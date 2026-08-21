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

    async def check(
        self,
        key_id: str,
        *,
        now: float | None = None,
        per_minute: int | None = None,
        monthly_quota: int | None = None,
    ) -> RateLimitVerdict:
        """Apply this key's limits, falling back to the server defaults.

        A per-key value of 0 means "no limit" and is honoured as such, which is
        why the override is ``None``-checked rather than truthiness-checked: a
        deliberate 0 must not silently fall back to the default.
        """
        now = now if now is not None else time.time()
        effective_minute = self.per_minute if per_minute is None else per_minute
        effective_quota = self.monthly_quota if monthly_quota is None else monthly_quota

        if effective_minute > 0:
            verdict = await self._check_window(key_id, now, effective_minute)
            if not verdict.allowed:
                return verdict
        if effective_quota > 0:
            return await self._check_quota(key_id, now, effective_quota)
        return RateLimitVerdict(allowed=True)

    # ------------------------------------------------------------------

    async def _check_window(
        self, key_id: str, now: float, limit: int
    ) -> RateLimitVerdict:
        if self._redis is not None:
            try:
                redis_key = f"{self._prefix}:rl:{key_id}:{int(now // 60)}"
                pipe = self._redis.pipeline()  # type: ignore[union-attr]
                pipe.incr(redis_key)
                pipe.expire(redis_key, 120)
                count, _ = await pipe.execute()
                if int(count) > limit:
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
        if len(window) >= limit:
            return RateLimitVerdict(
                allowed=False,
                retry_after=max(1, int(60 - (now - window[0]))),
                reason="rate limit exceeded",
            )
        window.append(now)
        return RateLimitVerdict(allowed=True)

    async def _check_quota(
        self, key_id: str, now: float, limit: int
    ) -> RateLimitVerdict:
        period = time.strftime("%Y%m", time.gmtime(now))
        if self._redis is not None:
            try:
                redis_key = f"{self._prefix}:quota:{key_id}:{period}"
                pipe = self._redis.pipeline()  # type: ignore[union-attr]
                pipe.incr(redis_key)
                pipe.expire(redis_key, 40 * 86400)
                count, _ = await pipe.execute()
                if int(count) > limit:
                    return RateLimitVerdict(
                        allowed=False, retry_after=3600, reason="monthly quota exceeded"
                    )
                return RateLimitVerdict(allowed=True)
            except Exception as exc:
                log.warning("ratelimit.quota_redis_failed", error=str(exc))

        local_key = f"{key_id}:{period}"
        used = self._local_monthly.get(local_key, 0) + 1
        self._local_monthly[local_key] = used
        if used > limit:
            return RateLimitVerdict(
                allowed=False, retry_after=3600, reason="monthly quota exceeded"
            )
        return RateLimitVerdict(allowed=True)
