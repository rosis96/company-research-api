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
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self._tasks: set[asyncio.Task] = set()

    def start(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id), name=f"ad-research-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def analyze_one(self, domain: str, *, landing_urls: list[str] | None = None):
        stored_landings = self.store.get_landing_urls(domain)
        combined_landings = list(dict.fromkeys([*(landing_urls or []), *stored_landings]))
        async with self.semaphore:
            return await self.detector.analyze(domain, landing_page_urls=combined_landings)

    async def _run(self, job_id: str) -> None:
        self.store.set_job_status(job_id, "RUNNING")
        rows = self.store.get_job_rows_for_run(job_id)

        async def run_domain(row_id: str, domain: str) -> None:
            self.store.mark_job_row_running(job_id, row_id)
            try:
                result = await self.analyze_one(domain)
                self.store.save_result(result, job_id, row_id)
            except Exception as exc:
                # Detector is designed to return structured failures. This is a
                # final containment boundary for unexpected persistence/runtime errors.
                log.exception(
                    "batch domain task failed", extra={"job_id": job_id, "domain": domain}
                )
                self.store.fail_job_row(job_id, row_id, f"{type(exc).__name__}: {exc}")

        await asyncio.gather(*(run_domain(row_id, domain) for row_id, domain in rows))
        current = self.store.get_job(job_id)
        if current and current.completed_domains < current.total_domains:
            self.store.set_job_status(job_id, "INCONCLUSIVE")

    async def drain(self, timeout: float = 5.0) -> None:
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=timeout)
        except TimeoutError:
            for task in self._tasks:
                task.cancel()
