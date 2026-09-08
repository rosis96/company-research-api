from __future__ import annotations

import asyncio
import logging

from ad_research.detector import AdvertisingDetector
from ad_research.store import CrawlStore

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, detector: AdvertisingDetector, store: CrawlStore, concurrency: int) -> None:
        self.detector = detector
        self.store = store
        self.concurrency = max(1, concurrency)
        self.semaphore = asyncio.Semaphore(self.concurrency)
        self._tasks: set[asyncio.Task] = set()

    def start(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id), name=f"ad-research-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            log.error("batch runner escaped", exc_info=error)

    async def analyze_one(self, domain: str, *, landing_urls: list[str] | None = None):
        stored_landings = self.store.get_landing_urls(domain)
        combined_landings = list(dict.fromkeys([*(landing_urls or []), *stored_landings]))
        async with self.semaphore:
            return await self.detector.analyze(domain, landing_page_urls=combined_landings)

    async def _run(self, job_id: str) -> None:
        try:
            self.store.set_job_status(job_id, "RUNNING")
            rows = self.store.get_job_rows_for_run(job_id)
            queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
            for row in rows:
                queue.put_nowait(row)

            async def worker() -> None:
                while True:
                    try:
                        row_id, domain = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    self.store.mark_job_row_running(job_id, row_id)
                    try:
                        result = await self.analyze_one(domain)
                        self.store.save_result(result, job_id, row_id)
                    except Exception as exc:
                        log.exception(
                            "batch domain task failed",
                            extra={"job_id": job_id, "domain": domain},
                        )
                        self.store.fail_job_row(job_id, row_id, f"{type(exc).__name__}: {exc}")
                    finally:
                        queue.task_done()

            worker_count = min(self.concurrency, len(rows))
            if worker_count:
                await asyncio.gather(*(worker() for _ in range(worker_count)))
            current = self.store.get_job(job_id)
            if current and current.completed_domains < current.total_domains:
                self.store.fail_unfinished_job(job_id, "batch ended before every row completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("batch runner failed", extra={"job_id": job_id})
            self.store.fail_unfinished_job(job_id, f"{type(exc).__name__}: {exc}")

    async def drain(self, timeout: float = 5.0) -> None:
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=timeout)
        except TimeoutError:
            for task in self._tasks:
                task.cancel()
