"""Background execution of batch research jobs.

Architecture reference: SS14 (async job API), SS0 (the 45-second GPT Actions
window is why batches cannot be synchronous).

**Why in-process rather than a separate worker service.**

A 30-URL job at 5 concurrent companies takes roughly 45-90 seconds. Running it as
an ``asyncio`` task inside the API process is by far the simplest thing that
works on Railway, and it needs no queue library and no second service.

The honest downside is that a deploy or restart mid-job kills the task. Two
things make that acceptable rather than dangerous:

1. **Every company's result is written to Redis the moment it finishes**, so a
   restart loses only the companies actually in flight -- typically fewer than
   five -- never the whole job.
2. **On startup the API reconciles**: any job still marked running is resumed,
   and only its missing indices are crawled. A resume is recorded in the job's
   ``notes`` so the caller can see it happened.

That combination gives at-least-once completion without Celery, RQ, arq, or a
second Railway service. If job durations ever grow past a few minutes, the right
next step is a separate worker process consuming a Redis list -- the interfaces
here already allow it, because the runner only talks to :class:`JobStore`.

Concurrency is bounded and configurable. One company failing never fails the job:
every crawl is individually guarded, and a crash becomes a ``failed`` row.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from app.crawl.normalize import normalize_url
from app.jobs.store import JobMeta, JobStore
from app.models.jobs import JobResultRow
from app.models.request import ResearchRequest
from app.models.response import Coverage, ResearchResponse
from app.research_service import ResearchService
from app.security.reasons import ReasonCode

log = structlog.get_logger(__name__)


class JobRunner:
    """Runs batch jobs in the background with bounded concurrency."""

    def __init__(
        self,
        *,
        service: ResearchService,
        store: JobStore,
        concurrency: int = 5,
    ) -> None:
        self.service = service
        self.store = store
        self.concurrency = max(1, concurrency)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------

    def start(self, meta: JobMeta) -> None:
        """Schedule a job without waiting for it."""
        task = asyncio.create_task(self._run_guarded(meta))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_guarded(self, meta: JobMeta) -> None:
        try:
            await self._run(meta)
        except asyncio.CancelledError:  # pragma: no cover - deploy/restart
            log.warning("job.cancelled", job_id=meta.job_id, completed=meta.completed)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job.crashed", job_id=meta.job_id, error=str(exc))
            meta.status = "failed"
            meta.notes.append("job_crashed")
            try:
                await self.store.save(meta)
            except Exception:
                pass

    # ------------------------------------------------------------------

    async def _run(self, meta: JobMeta) -> None:
        started = time.monotonic()
        meta.status = "running"
        await self.store.save(meta)

        done = await self.store.completed_indices(meta.job_id)
        if done:
            log.info("job.resuming", job_id=meta.job_id, already_done=len(done))

        # Group identical URLs so a duplicate is crawled once and fanned out to
        # every input_index that asked for it.  Each index still gets its own row.
        groups: dict[str, list[int]] = {}
        for index, url in enumerate(meta.urls):
            if index in done:
                continue
            key = normalize_url(url) or url.strip().lower()
            groups.setdefault(key, []).append(index)

        semaphore = asyncio.Semaphore(self.concurrency)
        lock = asyncio.Lock()

        async def process(indices: list[int]) -> None:
            first = indices[0]
            url = meta.urls[first]
            async with semaphore:
                response = await self._research_one(url)

            rows = [_to_row(index, meta.urls[index], response) for index in indices]
            async with lock:
                for row in rows:
                    await self.store.put_result(meta.job_id, row)
                    meta.completed += 1
                    if row.status == "success":
                        meta.successful += 1
                    elif row.status == "partial":
                        meta.partial += 1
                    else:
                        meta.failed += 1
                await self.store.save(meta)

        await asyncio.gather(
            *(process(indices) for indices in groups.values()), return_exceptions=True
        )

        # Any index still missing means its task raised before writing a row.
        missing = set(range(meta.total)) - await self.store.completed_indices(meta.job_id)
        for index in sorted(missing):
            row = JobResultRow(
                input_index=index,
                requested_url=meta.urls[index],
                canonical_domain=_domain_of(meta.urls[index]),
                status="failed",
                status_reason=str(ReasonCode.CONNECTION_FAILED),
                coverage=Coverage(),
            )
            await self.store.put_result(meta.job_id, row)
            meta.completed += 1
            meta.failed += 1

        meta.status = "completed"
        meta.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self.store.save(meta)

        log.info(
            "job.completed",
            job_id=meta.job_id,
            total=meta.total,
            successful=meta.successful,
            partial=meta.partial,
            failed=meta.failed,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _research_one(self, url: str) -> ResearchResponse:
        """One company, fully isolated. Never raises."""
        request = ResearchRequest(url=url)
        try:
            outcome = await self.service.run(request, key_id="job")
            return outcome.response
        except Exception as exc:
            # str(exc) is empty for several httpx and ssl exceptions, which makes
            # the log line useless exactly when it matters, so record the type.
            log.warning(
                "job.company_failed",
                url=url,
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
            from app.research_service import immediate_failure

            return immediate_failure(
                request, "req_job_error", ReasonCode.CONNECTION_FAILED
            )

    # ------------------------------------------------------------------

    async def resume_interrupted(self) -> int:
        """Re-queue jobs left running by a restart. Returns how many resumed."""
        if not self.store.available:
            return 0
        try:
            job_ids = await self.store.active_job_ids()
        except Exception as exc:  # pragma: no cover - redis hiccup at boot
            log.warning("job.resume_scan_failed", error=str(exc))
            return 0

        resumed = 0
        for job_id in job_ids:
            meta = await self.store.load(job_id)
            if meta is None or meta.status in ("completed", "failed"):
                continue
            if not await self.store.claim_resume_lock(job_id):
                continue
            meta.notes.append("resumed_after_restart")
            # Recount from what actually persisted, so progress stays truthful.
            done = await self.store.completed_indices(job_id)
            rows = await self.store.get_results(job_id, sorted(done))
            meta.completed = len(rows)
            meta.successful = sum(1 for r in rows if r.status == "success")
            meta.partial = sum(1 for r in rows if r.status == "partial")
            meta.failed = sum(1 for r in rows if r.status == "failed")
            self.start(meta)
            resumed += 1
            log.info("job.resumed", job_id=job_id, already_done=meta.completed)
        return resumed

    async def drain(self, timeout: float = 5.0) -> None:
        """Give in-flight jobs a moment to persist on shutdown."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()


def _to_row(index: int, requested_url: str, response: ResearchResponse) -> JobResultRow:
    """Project a full research response into a batch row.

    Page content is carried through unchanged here; trimming to fit the response
    budget happens at read time in the results endpoint, so the stored evidence
    stays complete.
    """
    return JobResultRow(
        input_index=index,
        requested_url=requested_url,
        canonical_domain=response.site.canonical_domain or _domain_of(requested_url),
        status=response.status,
        evidence_sufficient=response.evidence_sufficient,
        status_reason=response.status_reason,
        site_name=response.site.site_name,
        tagline=response.site.tagline,
        pages=response.pages,
        coverage=response.coverage,
    )


def _domain_of(url: str) -> str | None:
    """Fallback domain for a row whose crawl never got far enough to set one.

    A failed row still needs ``canonical_domain``: null fields are stripped from
    the response, so without this the caller would see the field simply missing
    and could not tell which company failed.
    """
    from urllib.parse import urlsplit

    from app.security.domains import registrable_domain

    try:
        candidate = url if "://" in url else f"https://{url}"
        host = urlsplit(candidate).hostname
        return registrable_domain(host) if host else None
    except Exception:  # pragma: no cover - defensive
        return None
