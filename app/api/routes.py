"""HTTP routes.

Architecture reference: SS9, SS11.

The status-code policy is the important part: **every crawl outcome returns HTTP
200**, including total failure.  Non-2xx is surfaced to ChatGPT as a tool error,
and the model's most likely recovery is to fall back on its own knowledge of the
company -- precisely the failure this system exists to prevent.  Real HTTP errors
are reserved for auth, quota, malformed requests and server faults.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from functools import lru_cache
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from app.api.auth import ApiKey, require_api_key
from app.cache.result_cache import cache_key, ttl_for
from app.crawl.orchestrator import ResearchOrchestrator
from app.crawl.robots import RobotsPolicy
from app.fetch.http import HttpFetcher
from app.models.request import ResearchRequest
from app.models.response import SCHEMA_VERSION, HealthResponse, ResearchResponse
from app.security.domains import registrable_domain
from app.security.reasons import ReasonCode
from app.security.url_guard import GuardError, coerce_input_url

log = structlog.get_logger(__name__)

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


@lru_cache(maxsize=4)
def _static_page(name: str) -> str:
    """Read a static page once and keep it in memory.

    Only ever called with literal names from the routes below, so no request
    input reaches the filesystem path.
    """
    return (STATIC_DIR / name).read_text(encoding="utf-8")


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> HTMLResponse:
    """Required to publish a Custom GPT with Actions publicly."""
    return HTMLResponse(_static_page("privacy.html"))


@router.get("/bot", response_class=HTMLResponse, include_in_schema=False)
async def bot_info() -> HTMLResponse:
    """The page site owners look for when they see the User-Agent in their logs."""
    return HTMLResponse(_static_page("bot.html"))


@router.get(
    "/v1/health",
    response_model=HealthResponse,
    summary="Health check",
    include_in_schema=False,
)
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    return HealthResponse(
        status="ok",
        version=state.app_version,
        schema_version=SCHEMA_VERSION,
        config_version=state.settings.config_version,
        renderer_available=state.renderer.available,
        cache_available=state.cache.available,
    )


@router.post(
    "/v1/research",
    response_model=ResearchResponse,
    response_model_exclude_none=True,
    summary="Research one company website",
    description=(
        "Researches one company website. Crawls the company's own domain only "
        "(homepage plus its most relevant internal pages such as services, about "
        "and case studies) and returns cleaned page text with source URLs. "
        'Returns status "failed" if the site cannot be read - never invents content.'
    ),
    responses={
        401: {"description": "Missing or invalid API key"},
        422: {"description": "Malformed request body"},
        429: {"description": "Rate limit or monthly quota exceeded"},
    },
)
async def research(
    payload: ResearchRequest,
    request: Request,
    response: Response,
    api_key: ApiKey = Depends(require_api_key),
) -> ResearchResponse:
    state = request.app.state
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    started = time.monotonic()

    verdict = await state.rate_limiter.check(api_key.short_id)
    if not verdict.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=verdict.reason,
            headers={"Retry-After": str(verdict.retry_after)},
        )

    if payload.advanced is not None and not api_key.is_advanced:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="advanced options require an advanced-scoped API key",
        )

    budget = state.limits.budget(payload.research_depth)
    robots_settings = state.settings.robots
    if payload.advanced is not None:
        budget = budget.with_overrides(
            max_pages=payload.advanced.max_pages,
            max_depth=payload.advanced.max_depth,
            max_renders=payload.advanced.max_renders,
            deadline_ms=payload.advanced.deadline_ms,
            global_content_budget=payload.advanced.global_content_budget,
        )
        overrides = {}
        if payload.advanced.robots_enabled is not None:
            overrides["enabled"] = payload.advanced.robots_enabled
        if payload.advanced.robots_apply_to_seed_url is not None:
            overrides["apply_to_seed_url"] = payload.advanced.robots_apply_to_seed_url
        if overrides:
            robots_settings = robots_settings.model_copy(update=overrides)

    # Cache lookup keys on the *requested* domain; a rebrand redirect is
    # resolved during the crawl and cached under what the caller asked for.
    try:
        seed = coerce_input_url(payload.url)
        canonical = registrable_domain(_host_of(seed))
    except GuardError:
        return _immediate_failure(payload, request_id, ReasonCode.INVALID_URL)

    key = cache_key(
        payload,
        canonical_domain=canonical,
        config_version=state.settings.config_version,
        prefix=state.settings.cache.key_prefix,
    )

    # Debug requests bypass the cache in both directions.  Reading would serve a
    # cached response that has no debug block; writing would pollute the shared
    # entry with a developer-only payload that normal callers would then receive.
    debug_requested = payload.advanced is not None and payload.advanced.debug

    if not payload.force_refresh and not debug_requested:
        cached = await state.cache.get(key)
        if cached is not None:
            cached.request_id = request_id
            cached.crawl_summary.cache_hit = True
            response.headers["X-Cache"] = "HIT"
            log.info(
                "research.cache_hit",
                request_id=request_id,
                domain=canonical,
                key_id=api_key.short_id,
            )
            return cached

    response.headers["X-Cache"] = "MISS"

    async with state.crawl_semaphore:
        fetcher = HttpFetcher(
            state.guard,
            user_agent=state.settings.user_agent,
            budget=budget,
            max_redirect_hops=state.settings.security.max_redirect_hops,
        )
        try:
            orchestrator = ResearchOrchestrator(
                state.guard,
                fetcher,
                state.renderer,
                RobotsPolicy(robots_settings, fetcher),
                state.limits,
            )
            result = await orchestrator.run(payload, request_id=request_id, budget=budget)
        finally:
            await fetcher.aclose()

    if not debug_requested:
        await state.cache.set(key, result, ttl_for(result, state.settings.cache))

    log.info(
        "research.completed",
        request_id=request_id,
        domain=canonical,
        key_id=api_key.short_id,
        status=result.status,
        status_reason=result.status_reason,
        pages=result.crawl_summary.pages_scraped,
        failed=result.crawl_summary.pages_failed,
        renders=result.crawl_summary.browser_renders,
        words=result.crawl_summary.total_words,
        chars=result.crawl_summary.content_chars,
        stopped_reason=result.crawl_summary.stopped_reason,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return result


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def _immediate_failure(
    payload: ResearchRequest, request_id: str, reason: ReasonCode
) -> ResearchResponse:
    from app.crawl.orchestrator import IMPORTANT_PAGE_TYPES
    from app.models.response import Coverage, CrawlSummary, FailedPage, SiteInfo

    return ResearchResponse(
        request_id=request_id,
        status="failed",
        status_reason=str(reason),
        evidence_sufficient=False,
        site=SiteInfo(requested_url=payload.url),
        failed_pages=[FailedPage(url=payload.url, reason_code=str(reason))],
        coverage=Coverage(found=[], missing=list(IMPORTANT_PAGE_TYPES)),
        crawl_summary=CrawlSummary(),
    )


# Kept for symmetry with the semaphore usage above.
def _noop_semaphore() -> asyncio.Semaphore:  # pragma: no cover - helper
    return asyncio.Semaphore(1)
