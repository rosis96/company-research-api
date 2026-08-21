"""Bearer-token authentication.

Architecture reference: SS12, "API-surface security".

GPT Actions do not support custom headers, so ``Authorization: Bearer`` is the
correct choice regardless of preference.  Keys are compared as SHA-256 digests
using a constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status

from app.config.settings import KeyRecord, Settings


@dataclass(frozen=True, slots=True)
class ApiKey:
    """The caller, as resolved from the bearer token.

    Carries the key's own limits so the rate limiter can apply them without a
    second lookup, and a ``label`` naming the workspace for logs.
    """

    digest: str
    scope: str
    label: str = ""
    rate_limit_per_minute: int | None = None
    monthly_quota: int | None = None

    @property
    def is_advanced(self) -> bool:
        return self.scope == "advanced"

    @property
    def short_id(self) -> str:
        return self.digest[:12]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ApiKey:
    """FastAPI dependency: resolve and validate the caller's API key."""
    settings: Settings = request.app.state.settings
    table: dict[str, KeyRecord] = request.app.state.api_key_table

    if not table:
        # Refusing every request is the right behaviour for a misconfigured
        # deployment: an open crawler endpoint is worse than a broken one.
        raise _unauthorized("no API keys are configured on this server")

    if not authorization:
        raise _unauthorized("missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("expected 'Authorization: Bearer <key>'")

    digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    for known_digest, record in table.items():
        if hmac.compare_digest(digest, known_digest):
            return ApiKey(
                digest=digest,
                scope=record.scope,
                label=record.label,
                rate_limit_per_minute=record.rate_limit_per_minute,
                monthly_quota=record.monthly_quota,
            )

    del settings  # only needed to make the dependency's contract explicit
    raise _unauthorized("invalid API key")
