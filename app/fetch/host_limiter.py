"""Per-host politeness limiting.

Architecture reference: SS4 (max 4 concurrent connections per host, small
jitter) and SS6 (``Crawl-delay`` is applied here and nowhere else).

When a site declares a crawl delay, concurrency for that host drops to 1 --
concurrent requests would otherwise defeat the delay entirely.
"""

from __future__ import annotations

import asyncio
import time


class HostLimiter:
    """Bounds concurrency and spacing for a single host."""

    __slots__ = ("_semaphore", "_delay_s", "_jitter_s", "_last_release", "_lock")

    def __init__(
        self,
        concurrency: int,
        *,
        crawl_delay_s: float = 0.0,
        jitter_s: float = 0.15,
    ) -> None:
        effective = 1 if crawl_delay_s > 0 else max(1, concurrency)
        self._semaphore = asyncio.Semaphore(effective)
        self._delay_s = max(0.0, crawl_delay_s)
        self._jitter_s = max(0.0, jitter_s)
        self._last_release = 0.0
        self._lock = asyncio.Lock()

    @property
    def crawl_delay_s(self) -> float:
        return self._delay_s

    async def __aenter__(self) -> HostLimiter:
        await self._semaphore.acquire()
        spacing = self._delay_s if self._delay_s > 0 else self._jitter_s
        if spacing > 0:
            async with self._lock:
                now = time.monotonic()
                wait = self._last_release + spacing - now
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_release = time.monotonic()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()
