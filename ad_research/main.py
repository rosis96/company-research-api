from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ad_research import __version__
from ad_research.api import router
from ad_research.detector import AdvertisingDetector, BrowserManager
from ad_research.jobs import JobRunner
from ad_research.settings import AdResearchSettings, get_settings
from ad_research.signature_engine import SignatureEngine
from ad_research.store import CrawlStore
from app.security.url_guard import GuardPolicy, UrlGuard


def create_app(settings: AdResearchSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = CrawlStore(settings.database_url)
        store.create_schema()
        browser = BrowserManager(settings)
        signatures = SignatureEngine(settings.signature_directory)
        guard = UrlGuard(GuardPolicy(max_redirect_hops=5))
        detector = AdvertisingDetector(settings, browser, guard, signatures)

        log = logging.getLogger(__name__)

        async def release_idle_browser() -> None:
            if settings.close_browser_when_idle:
                await browser.invalidate()
                log.info("released idle Chromium; no batch is running")

        jobs = JobRunner(
            detector,
            store,
            settings.concurrency,
            dedupe=settings.dedupe_job_domains,
            on_idle=release_idle_browser,
        )

        def purge_once() -> None:
            removed = store.purge_expired(settings.retention_days)
            if any(removed.values()):
                log.info(
                    "purged expired research older than %sd: %s job(s), %s result(s), "
                    "%s activity record(s)",
                    settings.retention_days,
                    removed["jobs"],
                    removed["results"],
                    removed["activity_evidence"],
                )

        async def resume_unfinished_jobs() -> None:
            resumable_jobs = await asyncio.to_thread(store.jobs_to_resume)
            for job_id in resumable_jobs:
                jobs.start(job_id)
            if resumable_jobs:
                log.warning("resumed %s unfinished ad-research job(s)", len(resumable_jobs))

        async def cleanup_loop() -> None:
            # The first pass purges before resuming, so an old stuck batch is not
            # re-crawled on every restart. It runs in the background because on
            # a large backlog the first purge can take minutes, and blocking
            # startup on it makes the platform healthcheck time out.
            interval = max(1, settings.cleanup_interval_hours) * 3600
            first_pass = True
            while True:
                try:
                    await asyncio.to_thread(purge_once)
                except Exception:
                    log.exception("scheduled retention cleanup failed")
                if first_pass:
                    first_pass = False
                    try:
                        await resume_unfinished_jobs()
                    except Exception:
                        log.exception("resuming unfinished ad-research jobs failed")
                await asyncio.sleep(interval)

        app.state.settings = settings
        app.state.store = store
        app.state.browser = browser
        app.state.detector = detector
        app.state.jobs = jobs
        app.state.version = __version__
        app.state.static_dir = Path(__file__).parent / "static"
        cleanup_task = asyncio.create_task(cleanup_loop(), name="ad-research-cleanup")
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await jobs.drain()
            await browser.close()
            store.dispose()

    app = FastAPI(
        title="Paid Advertising Infrastructure & Activity Detector",
        version=__version__,
        description=(
            "Browser-based, evidence-first detection of paid-advertising infrastructure. "
            "It does not estimate media spend."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        return JSONResponse(422, {"error": "invalid_request", "detail": exc.errors()[:4]})

    return app


app = create_app()
