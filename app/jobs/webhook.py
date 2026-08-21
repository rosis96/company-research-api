"""Signed outbound callbacks for batch jobs.

A caller-supplied URL that this server then POSTs to is a classic SSRF pivot:
point it at an internal address and the server becomes the attacker's HTTP
client. So a callback target is validated by exactly the same
:class:`UrlGuard` as a page we crawl -- scheme and port allowlists, IP literal
and shorthand rejection, private/link-local/CGNAT blocking, DNS pinning. There
is no separate, weaker path for "our own" outbound requests.

Two further rules:

* **Every delivery is signed.** An unsigned webhook is one anyone can forge, and
  a forged "this company finished, here are the facts" is worse than no webhook
  at all. Callbacks are refused outright when no signing secret is configured.
* **The event is the payload, not the evidence.** A callback says *which*
  company finished and how it went; the receiver fetches the content from the
  results endpoint. Thirty companies of full evidence is thirty large POSTs with
  size limits and partial-delivery problems that nobody needs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import anyio
import httpx
import structlog

from app.config.settings import WebhookSettings
from app.security.transport import GuardedTransport, PinRegistry
from app.security.url_guard import GuardError, UrlGuard

log = structlog.get_logger(__name__)

#: Retried: the receiver is up but unhappy, or unreachable for a moment.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class WebhookError(ValueError):
    """The callback URL is unusable. Raised at job creation, not at delivery."""


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>`` over ``timestamp.body``.

    The timestamp is inside the signed material so a captured delivery cannot be
    replayed later against a receiver that checks freshness.
    """
    material = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookDispatcher:
    """Validates callback URLs and delivers signed events to them."""

    def __init__(
        self, *, guard: UrlGuard, settings: WebhookSettings, user_agent: str
    ) -> None:
        self.guard = guard
        self.settings = settings
        self.user_agent = user_agent

    @property
    def available(self) -> bool:
        return self.settings.enabled and bool(self.settings.signing_secret)

    async def validate_target(self, url: str) -> str:
        """Check a callback URL *before* accepting the job.

        Rejecting at creation gives the caller a 422 they can act on, instead of
        a job that runs to completion and then fails silently to deliver.
        """
        if not self.settings.enabled:
            raise WebhookError("callbacks are disabled on this server")
        if not self.settings.signing_secret:
            raise WebhookError(
                "callbacks require a signing secret; unsigned callbacks are refused"
            )
        try:
            target = await self.guard.validate(url, None)
        except GuardError as exc:
            raise WebhookError(f"callback url rejected: {exc.reason}") from exc

        allowed = self.settings.allowed_hosts
        if allowed and target.host not in allowed:
            raise WebhookError(f"callback host {target.host} is not allowlisted")
        return target.url

    async def deliver(self, url: str, event: str, data: dict) -> bool:
        """POST one event. Returns whether it was accepted.

        Delivery failure never fails the job: the results are already durable in
        Redis and the receiver can always poll. A webhook is an optimisation, not
        a source of truth.
        """
        if not self.available:
            return False

        body = json.dumps(
            {"event": event, "delivery_id": f"whd_{uuid.uuid4().hex[:16]}", **data},
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "X-CRA-Event": event,
            "X-CRA-Timestamp": timestamp,
            "X-CRA-Signature": sign(self.settings.signing_secret, timestamp, body),
        }

        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                # Re-validated every attempt: DNS can change between them, and a
                # redirect is never followed, so the host we checked is the host
                # we talk to.
                target = await self.guard.validate(url, None)
                # The connection is pinned to the address the guard just
                # cleared. Without this the client re-resolves the host itself
                # and a DNS answer that changes between validation and connect
                # walks straight past every IP rule -- the rebinding hole the
                # whole transport layer exists to close. Redirects are never
                # followed, for the same reason the crawler never follows them.
                registry = PinRegistry()
                registry.pin(target.host, target.port, target.pinned_address)
                transport = GuardedTransport(registry)
                try:
                    async with httpx.AsyncClient(
                        transport=transport,
                        timeout=self.settings.timeout_seconds,
                        follow_redirects=False,
                    ) as client:
                        response = await client.post(
                            target.url, content=body, headers=headers
                        )
                finally:
                    closer = getattr(transport, "aclose", None)
                    if closer is not None:
                        await closer()
                if response.status_code < 300:
                    log.info(
                        "webhook.delivered",
                        hook_event=event,
                        status=response.status_code,
                        attempt=attempt,
                        host=target.host,
                    )
                    return True
                if response.status_code not in _RETRY_STATUSES:
                    log.warning(
                        "webhook.rejected",
                        hook_event=event,
                        status=response.status_code,
                        attempt=attempt,
                    )
                    return False
                reason = f"status {response.status_code}"
            except GuardError as exc:
                log.warning(
                    "webhook.url_rejected", hook_event=event, reason=str(exc.reason)
                )
                return False
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

            log.warning(
                "webhook.attempt_failed", hook_event=event, attempt=attempt, error=reason
            )
            if attempt < self.settings.max_attempts:
                await anyio.sleep(self.settings.retry_backoff_seconds * attempt)

        log.warning(
            "webhook.gave_up", hook_event=event, attempts=self.settings.max_attempts
        )
        return False
