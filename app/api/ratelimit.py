"""Per-key rate limiting and monthly quota.

Architecture reference: SS12.  Backed by Redis when available, with an in-process
fallback so a single-node deployment still enforces limits if Redis is down.

Fails *open* on Redis errors: losing the cache should degrade cost control, not
take the service offline.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    retry_after: int = 0
    reason: str = ""


class RateLimiter:
    """Sliding-window request limiter plus an optional monthly quota."""

    def __init__(
        self,
        *,
        per_minute: int,
        monthly_quota: int = 0,
        redis_client: object | None = None,
        key_prefix: str = "cra",
    ) -> None:
        self.per_minute = per_minute
        self.monthly_quota = monthly_quota
        self._redis = redis_client
        self._prefix = key_prefix
        self._local: dict[str, deque[float]] = {}
        self._local_monthly: dict[str, int] = {}

    async def check(self, key_id: str, *, now: float | None = None) -> RateLimitVerdict:
        now = now if now is not None else time.time()
        if self.per_minute > 0:
            verdict = await self._check_window(key_id, now)
            if not verdict.allowed:
                return verdict
        if self.monthly_quota > 0:
            return await self._check_quota(key_id, now)
        return RateLimitVerdict(allowed=True)

    # ------------------------------------------------------------------

    async def _check_window(self, key_id: str, now: float) -> RateLimitVerdict:
        if self._redis is not None:
            try:
                redis_key = f"{self._prefix}:rl:{key_id}:{int(now // 60)}"
                pipe = self._redis.pipeline()  # type: ignore[union-attr]
                pipe.incr(redis_key)
                pipe.expire(redis_key, 120)
                count, _ = await pipe.execute()
                if int(count) > self.per_minute:
                    return RateLimitVerdict(
                        allowed=False,
                        retry_after=60 - int(now % 60),
                        reason="rate limit exceeded",
                    )
                return RateLimitVerdict(allowed=True)
            except Exception as exc:
                log.warning("ratelimit.redis_failed", error=str(exc))

        window = self._local.setdefault(key_id, deque())
        cutoff = now - 60
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self.per_minute:
            return RateLimitVerdict(
                allowed=False,
                retry_after=max(1, int(60 - (now - window[0]))),
                reason="rate limit exceeded",
            )
        window.append(now)
        return RateLimitVerdict(allowed=True)

    async def _check_quota(self, key_id: str, now: float) -> RateLimitVerdict:
        period = time.strftime("%Y%m", time.gmtime(now))
        if self._redis is not None:
            try:
                redis_key = f"{self._prefix}:quota:{key_id}:{period}"
                pipe = self._redis.pipeline()  # type: ignore[union-attr]
                pipe.incr(redis_key)
                pipe.expire(redis_key, 40 * 86400)
                count, _ = await pipe.execute()
                if int(count) > self.monthly_quota:
                    return RateLimitVerdict(
                        allowed=False, retry_after=3600, reason="monthly quota exceeded"
                    )
                return RateLimitVerdict(allowed=True)
            except Exception as exc:
                log.warning("ratelimit.quota_redis_failed", error=str(exc))

        local_key = f"{key_id}:{period}"
        used = self._local_monthly.get(local_key, 0) + 1
        self._local_monthly[local_key] = used
        if used > self.monthly_quota:
            return RateLimitVerdict(
                allowed=False, retry_after=3600, reason="monthly quota exceeded"
            )
        return RateLimitVerdict(allowed=True)
