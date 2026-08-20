"""HTTP routes.

Architecture reference: SS9, SS11.

The status-code policy is the important part: **every crawl outcome returns HTTP
200**, including total failure.  Non-2xx is surfaced to ChatGPT as a tool error,
and the model's most likely recovery is to fall back on its own knowledge of the
company -- precisely the failure this system exists to prevent.  Real HTTP errors
are reserved for auth, quota, malformed requests and server faults.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse

from app.api.auth import ApiKey, require_api_key
from app.extract.content import truncate_at_paragraph
from app.jobs.store import JobMeta, JobStoreUnavailable
from app.models.jobs import (
    JobCreated,
    JobCreateRequest,
    JobProgress,
    JobResultRow,
    JobResultsPage,
)
from app.models.request import ResearchRequest
from app.models.response import SCHEMA_VERSION, HealthResponse, ResearchResponse

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

    await _enforce_rate_limit(state, api_key)

    if payload.advanced is not None and not api_key.is_advanced:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="advanced options require an advanced-scoped API key",
        )

    outcome = await state.research_service.run(
        payload, request_id=request_id, key_id=api_key.short_id
    )
    response.headers["X-Cache"] = "HIT" if outcome.cache_hit else "MISS"
    return outcome.response


# ---------------------------------------------------------------------------
# Batch research jobs
# ---------------------------------------------------------------------------


@router.post(
    "/v1/research/jobs",
    response_model=JobCreated,
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a batch research job",
    operation_id="createResearchJob",
    description=(
        "Starts researching several company websites in the background and returns "
        "a job_id immediately. Input order and duplicates are preserved. Poll "
        "getResearchJob for progress, then read getResearchResults in pages."
    ),
)
async def create_research_job(
    payload: JobCreateRequest,
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> JobCreated:
    state = request.app.state
    settings = state.settings.jobs

    await _enforce_rate_limit(state, api_key)

    if not settings.enabled:
        raise HTTPException(status_code=503, detail="batch jobs are disabled")

    if len(payload.urls) > settings.max_urls:
        raise HTTPException(
            status_code=422,
            detail=f"at most {settings.max_urls} urls per job, got {len(payload.urls)}",
        )

    # Batch jobs require Redis. The result cache fails open because losing it only
    # costs a re-crawl; losing job state loses the job itself.
    if not state.job_store.available:
        raise HTTPException(
            status_code=503,
            detail="batch jobs require Redis, which is not currently available",
        )

    job_id = f"job_{uuid.uuid4().hex[:16]}"
    meta = JobMeta(
        job_id=job_id,
        urls=list(payload.urls),
        research_depth=payload.research_depth,
        focus=list(payload.focus) if payload.focus else None,
        force_refresh=payload.force_refresh,
        status="queued",
    )
    try:
        await state.job_store.create(meta)
    except JobStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    state.job_runner.start(meta)

    log.info(
        "job.created",
        job_id=job_id,
        total=meta.total,
        key_id=api_key.short_id,
        depth=payload.research_depth,
    )
    return JobCreated(job_id=job_id, status="queued", total=meta.total)


@router.get(
    "/v1/research/jobs/{job_id}",
    response_model=JobProgress,
    response_model_exclude_none=True,
    summary="Check batch job progress",
    operation_id="getResearchJob",
    description=(
        "Returns progress for a batch research job. Poll until status is "
        "'completed', then read the results with getResearchResults."
    ),
)
async def get_research_job(
    job_id: str,
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> JobProgress:
    state = request.app.state
    meta = await _load_job(state, job_id)
    return meta.to_progress()


@router.get(
    "/v1/research/jobs/{job_id}/results",
    response_model=JobResultsPage,
    response_model_exclude_none=True,
    summary="Read batch job results",
    operation_id="getResearchResults",
    description=(
        "Returns a page of results in the original input order. Use start and "
        "limit to page through them. Never returns every company at once, "
        "because responses are size limited."
    ),
)
async def get_research_results(
    job_id: str,
    request: Request,
    start: int = Query(default=0, ge=0, description="Index of the first result to return."),
    limit: int | None = Query(
        default=None, ge=1, description="How many results to return. Default 5, maximum 10."
    ),
    api_key: ApiKey = Depends(require_api_key),
) -> JobResultsPage:
    state = request.app.state
    settings = state.settings.jobs
    meta = await _load_job(state, job_id)

    effective_limit = min(limit or settings.default_limit, settings.max_limit)
    indices = list(range(start, min(start + effective_limit, meta.total)))
    rows = await state.job_store.get_results(job_id, indices)
    rows.sort(key=lambda r: r.input_index)

    notes: list[str] = []
    trimmed = _apply_results_budget(rows, settings.results_char_budget)
    if trimmed:
        notes.append("page_content_trimmed_to_fit_response_budget")
    if meta.status != "completed":
        notes.append("job_still_running_some_results_may_be_missing")

    next_start = start + effective_limit
    has_more = next_start < meta.total
    return JobResultsPage(
        job_id=job_id,
        status=meta.status,  # type: ignore[arg-type]
        total=meta.total,
        completed=meta.completed,
        start=start,
        limit=effective_limit,
        returned=len(rows),
        has_more=has_more,
        next_start=next_start if has_more else None,
        results=rows,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _enforce_rate_limit(state, api_key: ApiKey) -> None:
    verdict = await state.rate_limiter.check(api_key.short_id)
    if not verdict.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=verdict.reason,
            headers={"Retry-After": str(verdict.retry_after)},
        )


async def _load_job(state, job_id: str) -> JobMeta:
    if not state.job_store.available:
        raise HTTPException(
            status_code=503,
            detail="batch jobs require Redis, which is not currently available",
        )
    try:
        meta = await state.job_store.load(job_id)
    except JobStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if meta is None:
        raise HTTPException(status_code=404, detail="job not found or expired")
    return meta


def _apply_results_budget(rows: list[JobResultRow], budget: int) -> bool:
    """Fit a slice of companies inside one response.

    Thirty companies of full evidence is several hundred thousand characters and
    GPT Actions cap a response at 100,000, so a page of results is budgeted the
    same way a single research response is: an equal share per company, truncated
    at a paragraph boundary, and always flagged (invariant 5).
    """
    if not rows:
        return False

    total = sum(len(page.content) for row in rows for page in row.pages)
    if total <= budget:
        return False

    per_company = max(1000, budget // len(rows))
    trimmed_any = False
    for row in rows:
        spent = 0
        kept = []
        for page in row.pages:
            remaining = per_company - spent
            if remaining <= 0:
                row.content_truncated = True
                trimmed_any = True
                break
            if len(page.content) > remaining:
                page.content, _ = truncate_at_paragraph(page.content, remaining)
                page.truncated = True
                row.content_truncated = True
                trimmed_any = True
            spent += len(page.content)
            kept.append(page)
        row.pages = kept
    return trimmed_any


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


