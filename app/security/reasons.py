"""Canonical failure reason codes.

Defined once here so the guard, the fetchers, the orchestrator and the response
models all agree on the vocabulary.  Architecture reference: SS11.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    """Reason codes as specified in SS11 of the architecture document."""

    INVALID_URL = "invalid_url"
    BLOCKED_SCHEME = "blocked_scheme"
    PRIVATE_ADDRESS = "private_address"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILED = "connection_failed"
    TLS_ERROR = "tls_error"
    TIMEOUT = "timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    HTTP_ERROR_4XX = "http_error_4xx"
    HTTP_ERROR_5XX = "http_error_5xx"
    BOT_BLOCKED = "bot_blocked"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PARKED_DOMAIN = "parked_domain"
    NON_HTML_CONTENT = "non_html_content"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    RENDER_FAILED = "render_failed"
    TOO_THIN = "too_thin"

    # Internal-only: a candidate URL left the frozen crawl scope.  Never
    # surfaced as a site-level status, only ever as a dropped page.
    OUT_OF_SCOPE = "out_of_scope"
    TOO_MANY_REDIRECTS = "too_many_redirects"


#: Reason codes that mean "this condition is stable, do not retry soon".
STABLE_FAILURES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.DNS_FAILURE,
        ReasonCode.PARKED_DOMAIN,
        ReasonCode.ROBOTS_DISALLOWED,
    }
)

#: Reason codes that are usually transient and deserve a short negative TTL.
TRANSIENT_FAILURES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.BOT_BLOCKED,
        ReasonCode.TIMEOUT,
        ReasonCode.CONNECTION_FAILED,
        ReasonCode.TLS_ERROR,
        ReasonCode.HTTP_ERROR_5XX,
        ReasonCode.DEADLINE_EXCEEDED,
    }
)

#: Rejected before any crawl work happened; nothing worth caching.
NEVER_CACHED: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.INVALID_URL,
        ReasonCode.BLOCKED_SCHEME,
        ReasonCode.PRIVATE_ADDRESS,
    }
)
