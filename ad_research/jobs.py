from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from ad_research.detector import AdvertisingDetector
from ad_research.store import CrawlStore
from app.security.url_guard import coerce_input_url

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(
        self,
        detector: AdvertisingDetector,
        store: CrawlStore,
        concurrency: int,
        *,
        dedupe: bool = True,
    ) -> None:
        self.detector = detector
        self.store = store
        self.concurrency = max(1, concurrency)
        self.dedupe = dedupe
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
            # Lead lists repeat the same company for every contact. Each distinct
            # website is scanned once per job and its result is attached to every
            # row that supplied it; each row keeps its own ID and original data.
            groups: dict[str, list[tuple[str, str]]] = {}
            for row_id, domain in rows:
                key = _website_key(domain) if self.dedupe else row_id
                groups.setdefault(key, []).append((row_id, domain))
            queue: asyncio.Queue[list[tuple[str, str]]] = asyncio.Queue()
            for group in groups.values():
                queue.put_nowait(group)

            async def worker() -> None:
                while True:
                    try:
                        group = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    finished: set[str] = set()
                    domain = group[0][1]
                    try:
                        for row_id, _ in group:
                            self.store.mark_job_row_running(job_id, row_id)
                        result = await self.analyze_one(domain)
                        stored = self.store.save_result(result, job_id, group[0][0])
                        finished.add(group[0][0])
                        for row_id, _ in group[1:]:
                            self.store.link_job_row_result(job_id, row_id, stored)
                            finished.add(row_id)
                    except Exception as exc:
                        log.exception(
                            "batch domain task failed",
                            extra={"job_id": job_id, "domain": domain},
                        )
                        for row_id, _ in group:
                            if row_id not in finished:
                                self.store.fail_job_row(
                                    job_id, row_id, f"{type(exc).__name__}: {exc}"
                                )
                    finally:
                        queue.task_done()

            worker_count = min(self.concurrency, len(groups))
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


def _website_key(value: str) -> str:
    """Treat company.com, https://www.company.com/ and COMPANY.COM as one site.

    Different subdomains or paths stay separate because the crawler starts from
    the supplied URL and may observe a different site there.
    """
    candidate = value.strip().lower()
    try:
        parts = urlsplit(coerce_input_url(candidate))
    except Exception:
        return candidate
    host = (parts.hostname or "").removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}" if host else candidate
