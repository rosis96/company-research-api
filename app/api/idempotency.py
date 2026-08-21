"""Idempotency keys, so a safe retry is actually safe.

The result cache already means a repeated research request costs a lookup
rather than a crawl. Idempotency exists for the case the cache cannot cover: a
client whose request timed out, or whose connection dropped, retrying something
it is not certain the server completed -- especially with ``force_refresh``,
where the cache is deliberately bypassed and a blind retry crawls the site
twice.

The contract is the usual one:

* first request with a given key runs, and its response is stored;
* a repeat with the same key **and the same body** replays the stored response;
* a repeat with the same key and a *different* body is a client bug and gets
  409, because silently serving the first answer to a different question is
  worse than an error;
* a repeat while the first is still running gets 409 rather than a second crawl.

Failures are not stored. A request that raised leaves no marker, so the caller
can retry it -- which is the entire point of retrying.
"""

from __future__ import annotations

import hashlib
import json

import structlog

log = structlog.get_logger(__name__)

#: Header clients send. Chosen to match the widely-implemented convention
#: rather than inventing an X- name.
HEADER = "Idempotency-Key"

_IN_PROGRESS = "in_progress"
_DONE = "done"


class IdempotencyConflict(Exception):
    """Same key, different request -- or the first one is still running."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def body_fingerprint(payload: object) -> str:
    """A stable hash of the request, so "same key, same body" is decidable."""
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Redis-backed. Fails **open**: without Redis, requests simply run.

    Refusing traffic because idempotency bookkeeping is unavailable would turn a
    convenience into an outage. The cost of failing open is a duplicated crawl,
    which is exactly what the caller had before this existed.
    """

    def __init__(self, client, *, prefix: str = "cra", ttl_seconds: int = 86_400) -> None:
        self._client = client
        self._prefix = prefix
        self._ttl = ttl_seconds

    @property
    def available(self) -> bool:
        return self._client is not None

    def _key(self, key_id: str, idempotency_key: str) -> str:
        # Scoped per API key: two workspaces choosing the same key string must
        # never see each other's responses.
        scope = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        return f"{self._prefix}:idem:{key_id}:{scope}"

    async def begin(
        self, key_id: str, idempotency_key: str, fingerprint: str
    ) -> dict | None:
        """Claim the key, or return the stored response for a replay.

        Raises :class:`IdempotencyConflict` when the key is reused for a
        different request, or while the first is still in flight.
        """
        if not self.available:
            return None

        redis_key = self._key(key_id, idempotency_key)
        marker = json.dumps({"state": _IN_PROGRESS, "fingerprint": fingerprint})
        try:
            claimed = await self._client.set(redis_key, marker, ex=self._ttl, nx=True)
            if claimed:
                return None
            raw = await self._client.get(redis_key)
        except Exception as exc:  # pragma: no cover - redis hiccup
            log.warning("idempotency.unavailable", error=str(exc))
            return None

        if not raw:
            # It expired between the SET and the GET. Treat as a fresh request.
            return None
        try:
            record = json.loads(raw)
        except ValueError:  # pragma: no cover - defensive
            return None

        if record.get("fingerprint") != fingerprint:
            raise IdempotencyConflict(
                "this Idempotency-Key was already used for a different request"
            )
        if record.get("state") == _IN_PROGRESS:
            raise IdempotencyConflict(
                "a request with this Idempotency-Key is still in progress"
            )
        return record.get("response")

    async def complete(
        self, key_id: str, idempotency_key: str, fingerprint: str, response: dict
    ) -> None:
        if not self.available:
            return
        record = json.dumps(
            {"state": _DONE, "fingerprint": fingerprint, "response": response}
        )
        try:
            await self._client.set(self._key(key_id, idempotency_key), record, ex=self._ttl)
        except Exception as exc:  # pragma: no cover - redis hiccup
            log.warning("idempotency.store_failed", error=str(exc))

    async def release(self, key_id: str, idempotency_key: str) -> None:
        """Drop the marker for a request that failed, so a retry can proceed."""
        if not self.available:
            return
        try:
            await self._client.delete(self._key(key_id, idempotency_key))
        except Exception:  # pragma: no cover - best effort
            pass
