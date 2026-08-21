"""Redis-backed result cache.

Architecture reference: SS14, "Caching -- APPROVED for V1".

Two details matter more than they look:

* ``CONFIG_VERSION`` and a fingerprint of the YAML configuration are folded into
  the key.  Without them, every ranking or extraction tweak during development
  is invisible behind stale entries.
* Failures are cached too, with differentiated TTLs.  Without negative caching, a
  dead domain retried across a campaign burns a full deadline every time.

The cache always fails open: if Redis is unavailable the request still runs.
"""

from __future__ import annotations

import hashlib
import json
import time

import structlog

from app.config.loader import config_fingerprint
from app.config.settings import CacheSettings
from app.models.request import ResearchRequest
from app.models.response import ResearchResponse
from app.security.reasons import NEVER_CACHED, STABLE_FAILURES, ReasonCode

log = structlog.get_logger(__name__)


def cache_key(
    request: ResearchRequest,
    *,
    canonical_domain: str,
    config_version: str,
    prefix: str = "cra",
) -> str:
    """``sha256(domain + depth + sorted(focus) + CONFIG_VERSION + config fingerprint)``."""
    focus = ",".join(sorted(request.focus or []))
    material = "|".join(
        [
            canonical_domain.lower(),
            request.research_depth,
            focus,
            config_version,
            config_fingerprint(),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{prefix}:research:{digest}"


def ttl_for(response: ResearchResponse, settings: CacheSettings) -> int | None:
    """Differentiated TTLs by outcome (SS14).  ``None`` means do not cache."""
    if response.status == "success":
        return settings.ttl_success_seconds
    if response.status == "partial":
        return settings.ttl_partial_seconds

    reason = response.status_reason or ""
    try:
        code = ReasonCode(reason)
    except ValueError:
        return settings.ttl_failed_transient_seconds

    if code in NEVER_CACHED:
        return None
    if code in STABLE_FAILURES:
        return settings.ttl_failed_stable_seconds
    return settings.ttl_failed_transient_seconds


def _unwrap(raw: dict) -> tuple[dict, int | None]:
    """Accept both the envelope and the bare response written before it.

    Entries live for up to fourteen days, so a deploy that changed the shape
    would otherwise blank the cache for a fortnight.
    """
    if isinstance(raw, dict) and "response" in raw and "cached_at" in raw:
        return raw["response"], int(raw["cached_at"])
    return raw, None


class ResultCache:
    """Thin wrapper over Redis with fail-open semantics."""

    def __init__(self, settings: CacheSettings, client: object | None = None) -> None:
        self.settings = settings
        self._client = client
        self._healthy = client is not None

    @classmethod
    async def connect(cls, settings: CacheSettings) -> ResultCache:
        if not settings.enabled:
            return cls(settings, client=None)
        try:
            import redis.asyncio as redis

            client = redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.connect_timeout_seconds,
                socket_timeout=settings.connect_timeout_seconds,
                decode_responses=True,
            )
            await client.ping()
            log.info("cache.connected", url=_redact(settings.redis_url))
            return cls(settings, client=client)
        except Exception as exc:
            log.warning("cache.unavailable", error=str(exc))
            return cls(settings, client=None)

    @property
    def available(self) -> bool:
        return self._client is not None and self._healthy

    async def get(
        self, key: str, *, max_age_seconds: int | None = None
    ) -> ResearchResponse | None:
        """Read a cached result, optionally refusing one that is too old.

        ``max_age_seconds`` is a *read-side* freshness bound, deliberately, not a
        caller-supplied TTL. A caller-set TTL would let one integration decide
        how stale every other integration's reads may be; ``max_age`` only ever
        narrows what this caller will accept. It is what HTTP does, for the same
        reason.
        """
        if not self.available:
            return None
        try:
            raw = await self._client.get(key)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("cache.get_failed", error=str(exc))
            self._healthy = False
            return None
        if not raw:
            return None
        try:
            payload, cached_at = _unwrap(json.loads(raw))
        except Exception as exc:
            log.warning("cache.decode_failed", error=str(exc))
            return None

        if max_age_seconds is not None:
            # An entry written before envelopes existed has no age. Treating
            # unknown as "too old" is the conservative reading: the caller asked
            # for a freshness guarantee we cannot make.
            if cached_at is None or (time.time() - cached_at) > max_age_seconds:
                return None
        try:
            return ResearchResponse.model_validate(payload)
        except Exception as exc:
            log.warning("cache.decode_failed", error=str(exc))
            return None

    async def set(self, key: str, response: ResearchResponse, ttl: int | None) -> None:
        if not self.available or ttl is None or ttl <= 0:
            return
        try:
            payload = json.dumps(
                {
                    "cached_at": int(time.time()),
                    "response": response.model_dump(mode="json", exclude_none=True),
                },
                separators=(",", ":"),
            )
            await self._client.set(key, payload, ex=ttl)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("cache.set_failed", error=str(exc))
            self._healthy = False

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass


class InMemoryCache(ResultCache):
    """Deterministic cache used by the test suite."""

    def __init__(self, settings: CacheSettings) -> None:
        super().__init__(settings, client=None)
        self.store: dict[str, tuple[str, int, float]] = {}

    @property
    def available(self) -> bool:
        return self.settings.enabled

    async def get(
        self, key: str, *, max_age_seconds: int | None = None
    ) -> ResearchResponse | None:
        entry = self.store.get(key)
        if entry is None:
            return None
        if max_age_seconds is not None and (time.time() - entry[2]) > max_age_seconds:
            return None
        return ResearchResponse.model_validate(json.loads(entry[0]))

    async def set(self, key: str, response: ResearchResponse, ttl: int | None) -> None:
        if ttl is None or ttl <= 0:
            return
        self.store[key] = (response.model_dump_json(exclude_none=True), ttl, time.time())

    async def aclose(self) -> None:
        self.store.clear()


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"
