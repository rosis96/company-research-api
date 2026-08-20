"""A minimal in-process stand-in for the Redis commands the job store uses.

Only the handful of operations JobStore and RateLimiter actually call, with real
TTL semantics driven by an injectable clock so expiry can be tested without
sleeping.
"""

from __future__ import annotations

import time


class FakeRedis:
    """Supports get/set/ttl/expire, hash ops and set ops, with expiry."""

    def __init__(self, clock=None) -> None:
        self._values: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._expiry: dict[str, float] = {}
        self._clock = clock or time.monotonic
        self.calls: list[str] = []

    # -- expiry --------------------------------------------------------

    def _expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is None:
            return False
        if self._clock() >= deadline:
            self._values.pop(key, None)
            self._hashes.pop(key, None)
            self._sets.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    def advance(self, seconds: float) -> None:
        """Only meaningful with a controllable clock."""
        raise NotImplementedError("inject a controllable clock instead")

    # -- strings -------------------------------------------------------

    async def get(self, key: str):
        self.calls.append(f"get {key}")
        if self._expired(key):
            return None
        return self._values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        self.calls.append(f"set {key}")
        if nx and not self._expired(key) and key in self._values:
            return None
        self._values[key] = value
        if ex is not None:
            self._expiry[key] = self._clock() + ex
        return True

    async def ttl(self, key: str) -> int:
        """Redis semantics: -2 when the key does not exist, -1 when it never expires."""
        self._expired(key)
        exists = key in self._values or key in self._hashes or key in self._sets
        if not exists:
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(0, int(deadline - self._clock()))

    async def expire(self, key: str, seconds: int):
        self._expiry[key] = self._clock() + seconds
        return True

    async def incr(self, key: str) -> int:
        self._expired(key)
        value = int(self._values.get(key, "0")) + 1
        self._values[key] = str(value)
        return value

    # -- hashes --------------------------------------------------------

    async def hset(self, key: str, field: str, value: str):
        self._expired(key)
        self._hashes.setdefault(key, {})[field] = value
        return 1

    async def hkeys(self, key: str) -> list[str]:
        if self._expired(key):
            return []
        return list(self._hashes.get(key, {}))

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        if self._expired(key):
            return [None] * len(fields)
        table = self._hashes.get(key, {})
        return [table.get(f) for f in fields]

    # -- sets ----------------------------------------------------------

    async def sadd(self, key: str, member: str):
        self._expired(key)
        self._sets.setdefault(key, set()).add(member)
        return 1

    async def srem(self, key: str, member: str):
        self._sets.get(key, set()).discard(member)
        return 1

    async def smembers(self, key: str) -> set[str]:
        if self._expired(key):
            return set()
        return set(self._sets.get(key, set()))

    # -- misc ----------------------------------------------------------

    def pipeline(self):
        return _FakePipeline(self)

    async def ping(self):
        return True

    async def aclose(self):
        return None

    def key_count(self) -> int:
        return len(self._values) + len(self._hashes) + len(self._sets)


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list = []

    def incr(self, key: str):
        self._ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", key, seconds))
        return self

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "incr":
                out.append(await self._redis.incr(op[1]))
            else:
                out.append(await self._redis.expire(op[1], op[2]))
        self._ops.clear()
        return out


class Clock:
    """Manually advanced clock for TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
