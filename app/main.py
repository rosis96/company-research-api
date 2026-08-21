"""FastAPI application factory.

Architecture reference: SS1, SS9, SS14.

The web layer owns process-wide resources (guard, renderer, cache, limiter) and
constructs a fresh orchestrator per request.  The orchestrator itself imports no
web framework, which is what makes the async job API a wiring change later.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.ratelimit import RateLimiter
from app.api.routes import router
from app.cache.result_cache import ResultCache
from app.config.loader import get_limits
from app.config.settings import Settings, get_settings
from app.fetch.renderer import build_renderer
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.jobs.webhook import WebhookDispatcher
from app.models.response import ErrorResponse
from app.observability.logging import configure_logging
from app.research_service import ResearchService
from app.scrape_service import ScrapeService
from app.security.transport import assert_pinning_supported
from app.security.url_guard import GuardPolicy, UrlGuard

log = structlog.get_logger(__name__)

APP_VERSION = "1.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail fast if httpcore's connect/SNI contract has changed: DNS pinning
        # silently degrading is exactly the kind of security regression that
        # must not be discovered in production.
        assert_pinning_supported()

        limits = get_limits()
        policy = GuardPolicy(
            allow_ip_literals=settings.security.allow_ip_literals,
            max_redirect_hops=settings.security.max_redirect_hops,
        )
        guard = UrlGuard(policy)

        cache = await ResultCache.connect(settings.cache)
        renderer = build_renderer(
            guard,
            settings.renderer,
            user_agent=settings.user_agent,
            concurrency=limits.budget("standard").render_concurrency,
        )

        app.state.settings = settings
        app.state.limits = limits
        app.state.guard = guard
        app.state.cache = cache
        app.state.renderer = renderer
        app.state.app_version = APP_VERSION
        app.state.api_key_table = settings.api_key_table()
        app.state.crawl_semaphore = asyncio.Semaphore(settings.max_concurrent_crawls)

        # One implementation of "research a company", shared by /v1/research and
        # the batch runner, so a batch cannot drift from a single call.
        app.state.research_service = ResearchService(
            guard=guard,
            renderer=renderer,
            cache=cache,
            settings=settings,
            limits=limits,
            crawl_semaphore=app.state.crawl_semaphore,
        )
        # One URL, no crawl. Shares the guard, renderer and limits, so the
        # security boundary is identical to the crawler's.
        app.state.scrape_service = ScrapeService(
            guard=guard,
            renderer=renderer,
            settings=settings,
            limits=limits,
            semaphore=app.state.crawl_semaphore,
        )
        app.state.webhooks = WebhookDispatcher(
            guard=guard,
            settings=settings.webhooks,
            user_agent=settings.user_agent,
        )
        app.state.job_store = JobStore(
            getattr(cache, "_client", None),
            prefix=settings.cache.key_prefix,
            ttl_seconds=settings.jobs.ttl_seconds,
        )
        app.state.job_runner = JobRunner(
            service=app.state.research_service,
            store=app.state.job_store,
            concurrency=settings.jobs.concurrency,
            webhooks=app.state.webhooks,
            include_results_in_callbacks=settings.webhooks.include_results,
        )
        app.state.rate_limiter = RateLimiter(
            per_minute=settings.rate_limit_per_minute,
            monthly_quota=settings.rate_limit_monthly_quota,
            redis_client=getattr(cache, "_client", None),
            key_prefix=settings.cache.key_prefix,
        )

        if not app.state.api_key_table:
            log.warning("startup.no_api_keys_configured")
        if not renderer.available:
            log.warning("startup.renderer_unavailable")
        if settings.jobs.enabled and not app.state.job_store.available:
            log.warning("startup.jobs_unavailable_no_redis")

        # A deploy or restart kills in-flight job tasks.  Results already written
        # to Redis survive, so resuming means crawling only the missing indices.
        if settings.jobs.enabled and settings.jobs.resume_on_startup:
            try:
                resumed = await app.state.job_runner.resume_interrupted()
                if resumed:
                    log.info("startup.jobs_resumed", count=resumed)
            except Exception as exc:  # pragma: no cover - never block startup
                log.warning("startup.job_resume_failed", error=str(exc))

        log.info(
            "startup.ready",
            version=APP_VERSION,
            environment=settings.environment,
            renderer=renderer.available,
            cache=cache.available,
            config_version=settings.config_version,
        )
        try:
            yield
        finally:
            # Give in-flight jobs a moment to persist their last results before
            # the process goes away; anything unfinished resumes on next boot.
            await app.state.job_runner.drain(timeout=5.0)
            await renderer.aclose()
            await cache.aclose()
            log.info("shutdown.complete")

    app = FastAPI(
        title="Company Website Research API",
        version=APP_VERSION,
        description=(
            "Fetches and cleans first-party content from a single company website "
            "so that a language model can write from evidence rather than "
            "assumption. Crawls only the company's own domain."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="invalid_request", detail=str(exc.errors()[:3])
            ).model_dump(exclude_none=True),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=_error_label(exc.status_code), detail=str(exc.detail)
            ).model_dump(exclude_none=True),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("request.unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error="internal_error").model_dump(exclude_none=True),
        )

    return app


def _error_label(status_code: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        422: "invalid_request",
        429: "rate_limited",
    }.get(status_code, "error")


app = create_app()
