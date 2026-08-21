"""The single-company research execution path, shared by both entry points.

Architecture reference: SS1 (the orchestrator is framework-free), SS14.

``/v1/research`` and the batch job runner must behave *identically* for one
company -- same budgets, same cache semantics, same isolation.  Extracting this
here is what guarantees that: there is one implementation, and the batch layer
is orchestration around it rather than a second copy of it.

Nothing in this module touches crawler behaviour.  It is the wiring that already
lived inside the route handler, lifted out unchanged.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import structlog

from app.cache.result_cache import ResultCache, cache_key, ttl_for
from app.config.loader import Limits
from app.config.settings import Settings
from app.crawl.orchestrator import ResearchOrchestrator
from app.crawl.robots import RobotsPolicy
from app.fetch.http import HttpFetcher
from app.fetch.renderer import Renderer
from app.models.request import ResearchRequest
from app.models.response import (
    Coverage,
    CrawlSummary,
    FailedPage,
    ResearchResponse,
    SiteInfo,
)
from app.security.domains import registrable_domain
from app.security.reasons import ReasonCode
from app.security.url_guard import GuardError, UrlGuard, coerce_input_url

log = structlog.get_logger(__name__)

#: Page types the coverage block reports on.  Imported lazily to avoid a cycle.
def _important_page_types() -> tuple[str, ...]:
    from app.crawl.orchestrator import IMPORTANT_PAGE_TYPES

    return IMPORTANT_PAGE_TYPES


@dataclass(slots=True)
class ResearchOutcome:
    """A completed research run plus how it was served."""

    response: ResearchResponse
    cache_hit: bool
    canonical_domain: str | None


class ResearchService:
    """Runs one company research request end to end.

    A fresh :class:`HttpFetcher` and :class:`ResearchOrchestrator` are built per
    call, so no crawl state is ever shared between companies -- the mechanical
    half of the no-cross-contamination requirement (SS11).
    """

    def __init__(
        self,
        *,
        guard: UrlGuard,
        renderer: Renderer,
        cache: ResultCache,
        settings: Settings,
        limits: Limits,
        crawl_semaphore,
    ) -> None:
        self.guard = guard
        self.renderer = renderer
        self.cache = cache
        self.settings = settings
        self.limits = limits
        self.crawl_semaphore = crawl_semaphore

    # ------------------------------------------------------------------

    async def run(
        self,
        payload: ResearchRequest,
        *,
        request_id: str | None = None,
        key_id: str = "-",
    ) -> ResearchOutcome:
        request_id = request_id or f"req_{uuid.uuid4().hex[:16]}"
        started = time.monotonic()

        budget = self.limits.budget(payload.research_depth)
        robots_settings = self.settings.robots
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
            return ResearchOutcome(
                response=immediate_failure(payload, request_id, ReasonCode.INVALID_URL),
                cache_hit=False,
                canonical_domain=None,
            )

        key = cache_key(
            payload,
            canonical_domain=canonical,
            config_version=self.settings.config_version,
            prefix=self.settings.cache.key_prefix,
        )

        # Debug requests bypass the cache in both directions.  Reading would serve
        # a cached response that has no debug block; writing would pollute the
        # shared entry with a developer-only payload.
        debug_requested = payload.advanced is not None and payload.advanced.debug

        if not payload.force_refresh and not debug_requested:
            cached = await self.cache.get(
                key, max_age_seconds=payload.max_age_seconds
            )
            if cached is not None:
                cached.request_id = request_id
                cached.crawl_summary.cache_hit = True
                log.info(
                    "research.cache_hit",
                    request_id=request_id,
                    domain=canonical,
                    key_id=key_id,
                )
                return ResearchOutcome(
                    response=cached, cache_hit=True, canonical_domain=canonical
                )

        async with self.crawl_semaphore:
            fetcher = HttpFetcher(
                self.guard,
                user_agent=self.settings.user_agent,
                budget=budget,
                max_redirect_hops=self.settings.security.max_redirect_hops,
            )
            try:
                orchestrator = ResearchOrchestrator(
                    self.guard,
                    fetcher,
                    self.renderer,
                    RobotsPolicy(robots_settings, fetcher),
                    self.limits,
                    escalate_render_on_block=self.settings.renderer.escalate_on_block,
                )
                result = await orchestrator.run(
                    payload, request_id=request_id, budget=budget
                )
            finally:
                await fetcher.aclose()

        if not debug_requested:
            await self.cache.set(key, result, ttl_for(result, self.settings.cache))

        log.info(
            "research.completed",
            request_id=request_id,
            domain=canonical,
            key_id=key_id,
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
        return ResearchOutcome(
            response=result, cache_hit=False, canonical_domain=canonical
        )


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def immediate_failure(
    payload: ResearchRequest, request_id: str, reason: ReasonCode
) -> ResearchResponse:
    """A failure that happened before any crawl work -- still HTTP 200 (SS11)."""
    return ResearchResponse(
        request_id=request_id,
        status="failed",
        status_reason=str(reason),
        evidence_sufficient=False,
        site=SiteInfo(requested_url=payload.url),
        failed_pages=[FailedPage(url=payload.url, reason_code=str(reason))],
        coverage=Coverage(found=[], missing=list(_important_page_types())),
        crawl_summary=CrawlSummary(),
    )
