from __future__ import annotations

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
        jobs = JobRunner(
            detector, store, settings.concurrency, dedupe=settings.dedupe_job_domains
        )

        app.state.settings = settings
        app.state.store = store
        app.state.browser = browser
        app.state.detector = detector
        app.state.jobs = jobs
        app.state.version = __version__
        app.state.static_dir = Path(__file__).parent / "static"
        resumable_jobs = store.jobs_to_resume()
        for job_id in resumable_jobs:
            jobs.start(job_id)
        if resumable_jobs:
            logging.getLogger(__name__).warning(
                "resumed %s unfinished ad-research job(s)", len(resumable_jobs)
            )
        try:
            yield
        finally:
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
