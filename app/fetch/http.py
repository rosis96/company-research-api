"""HTTP fetching.

Architecture reference: SS4 (fetch strategy) and SS12 (redirects, response
handling).

Every request in this module goes through :class:`~app.security.url_guard.UrlGuard`
first and is then pinned to the validated address by
:class:`~app.security.transport.GuardedTransport`.  ``follow_redirects`` is never
enabled: redirects are walked manually so that each hop is re-validated in full.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
import structlog

from app.config.loader import CrawlBudget
from app.models.internal import Deadline, FetchFailure, FetchMethod, FetchResult
from app.security.reasons import ReasonCode
from app.security.transport import GuardedTransport, PinRegistry
from app.security.url_guard import CrawlScope, GuardError, UrlGuard, ValidatedTarget

log = structlog.get_logger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = (
    "text/plain",
    "text/xml",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "text/html",  # servers frequently mislabel robots.txt and sitemaps
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_STATUSES = frozenset({401, 403, 407, 429})
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I
)


@dataclass(slots=True)
class FetchOutcome:
    """Either a successful fetch or a typed failure -- never both."""

    result: FetchResult | None = None
    failure: FetchFailure | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


class HttpFetcher:
    """Guarded, streamed, manually-redirected HTTP client."""

    def __init__(
        self,
        guard: UrlGuard,
        *,
        user_agent: str,
        budget: CrawlBudget,
        max_redirect_hops: int = 5,
        registry: PinRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.guard = guard
        self.budget = budget
        self.max_redirect_hops = max_redirect_hops
        self.registry = registry or PinRegistry()
        self._transport = transport or GuardedTransport(self.registry)
        self._client = httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,  # invariant: redirects are walked manually
            timeout=httpx.Timeout(
                connect=budget.connect_timeout_s,
                read=budget.read_timeout_s,
                write=budget.connect_timeout_s,
                pool=budget.connect_timeout_s,
            ),
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
            },
            max_redirects=0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        closer = getattr(self._transport, "aclose", None)
        if closer is not None:
            await closer()

    # ------------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        deadline: Deadline,
        scope: CrawlScope | None,
        allow_domain_rebase: bool = False,
        accept_types: tuple[str, ...] = HTML_TYPES,
        max_bytes: int | None = None,
        total_timeout_s: float | None = None,
    ) -> FetchOutcome:
        """Fetch one URL, walking redirects manually.

        ``allow_domain_rebase`` is passed only for the seed URL.  It permits the
        chain to leave the requested registrable domain (a rebrand redirect is
        first-party evidence); the caller then freezes the crawl scope from the
        final URL.  For every other page, an off-scope redirect drops the page.
        """
        started = deadline.elapsed_ms
        limit = max_bytes if max_bytes is not None else self.budget.max_response_bytes
        chain: list[str] = []
        current = url

        allowed = total_timeout_s if total_timeout_s is not None else self.budget.total_timeout_s
        allowed = deadline.budget_for(allowed)
        if allowed <= 0:
            return FetchOutcome(
                failure=FetchFailure(
                    url=url, reason=ReasonCode.DEADLINE_EXCEEDED, methods_tried=["http"]
                )
            )

        try:
            async with asyncio.timeout(allowed):
                for hop in range(self.max_redirect_hops + 1):
                    try:
                        target = await self.guard.validate(
                            current, scope=None if allow_domain_rebase else scope
                        )
                    except GuardError as exc:
                        # Report the page that was *asked* for, not the redirect
                        # target it tried to escape to -- the caller needs to
                        # know which of its candidates failed.
                        return FetchOutcome(
                            failure=FetchFailure(
                                url=url,
                                reason=exc.reason,
                                detail=(exc.detail or "") + (f" (at {current})" if current != url else ""),
                                methods_tried=["http"],
                            )
                        )

                    chain.append(target.url)
                    self.registry.pin(target.host, target.port, target.pinned_address)

                    outcome, redirect_to = await self._single_request(
                        target, accept_types=accept_types, max_bytes=limit
                    )
                    if redirect_to is None:
                        if outcome.result is not None:
                            outcome.result.redirect_chain = chain
                            outcome.result.elapsed_ms = deadline.elapsed_ms - started
                        return outcome

                    if hop >= self.max_redirect_hops:
                        return FetchOutcome(
                            failure=FetchFailure(
                                url=url,
                                reason=ReasonCode.TOO_MANY_REDIRECTS,
                                detail=f"exceeded {self.max_redirect_hops} hops",
                                methods_tried=["http"],
                            )
                        )

                    nxt = urljoin(target.url, redirect_to)
                    if nxt in chain:
                        return FetchOutcome(
                            failure=FetchFailure(
                                url=url,
                                reason=ReasonCode.TOO_MANY_REDIRECTS,
                                detail="redirect loop",
                                methods_tried=["http"],
                            )
                        )
                    current = nxt
        except TimeoutError:
            return FetchOutcome(
                failure=FetchFailure(
                    url=url, reason=ReasonCode.TIMEOUT, methods_tried=["http"]
                )
            )

        return FetchOutcome(  # pragma: no cover - loop always returns
            failure=FetchFailure(
                url=url, reason=ReasonCode.TOO_MANY_REDIRECTS, methods_tried=["http"]
            )
        )

    # ------------------------------------------------------------------

    async def _single_request(
        self,
        target: ValidatedTarget,
        *,
        accept_types: tuple[str, ...],
        max_bytes: int,
    ) -> tuple[FetchOutcome, str | None]:
        """Issue one request.  Returns ``(outcome, redirect_location_or_None)``."""
        request = self._client.build_request("GET", target.url)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException:
            return _fail(target.url, ReasonCode.TIMEOUT), None
        except httpx.ConnectError as exc:
            reason = (
                ReasonCode.TLS_ERROR
                if isinstance(exc.__cause__, ssl.SSLError) or "ssl" in str(exc).lower()
                else ReasonCode.CONNECTION_FAILED
            )
            return _fail(target.url, reason, str(exc)), None
        except ssl.SSLError as exc:  # pragma: no cover - normally wrapped
            return _fail(target.url, ReasonCode.TLS_ERROR, str(exc)), None
        except httpx.HTTPError as exc:
            return _fail(target.url, ReasonCode.CONNECTION_FAILED, str(exc)), None

        try:
            status = response.status_code

            if status in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if location:
                    return FetchOutcome(), location
                return _fail(target.url, ReasonCode.HTTP_ERROR_4XX, "redirect without location"), None

            if status in _BLOCKED_STATUSES:
                return _fail(target.url, ReasonCode.BOT_BLOCKED, f"status {status}", status), None
            if 400 <= status < 500:
                return _fail(target.url, ReasonCode.HTTP_ERROR_4XX, f"status {status}", status), None
            if status >= 500:
                return _fail(target.url, ReasonCode.HTTP_ERROR_5XX, f"status {status}", status), None
            if status < 200 or status >= 300:  # pragma: no cover - 1xx
                return _fail(target.url, ReasonCode.HTTP_ERROR_4XX, f"status {status}", status), None

            # Content-Type is checked on the headers, before any body is read.
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(accept_types):
                return (
                    _fail(
                        target.url,
                        ReasonCode.NON_HTML_CONTENT,
                        f"content-type {content_type}",
                        status,
                    ),
                    None,
                )

            # Declared length is only a hint; the stream is what is enforced.
            body, truncated = await self._read_capped(response, max_bytes)
            text = _decode(body, response)
            return (
                FetchOutcome(
                    result=FetchResult(
                        url=target.url,
                        final_url=str(response.url),
                        status_code=status,
                        html=text,
                        method=FetchMethod.HTTP,
                        truncated_bytes=truncated,
                    )
                ),
                None,
            )
        except httpx.TimeoutException:
            return _fail(target.url, ReasonCode.TIMEOUT), None
        except httpx.HTTPError as exc:
            return _fail(target.url, ReasonCode.CONNECTION_FAILED, str(exc)), None
        finally:
            await response.aclose()

    @staticmethod
    async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
        """Stream the body, aborting at ``max_bytes`` of *decompressed* data.

        httpx decodes content-encoding as it iterates, so the cap here is a
        decompression-bomb guard as well as a size guard.
        """
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                truncated = True
                break
        return b"".join(chunks)[:max_bytes], truncated


def _fail(
    url: str, reason: ReasonCode, detail: str = "", status: int | None = None
) -> FetchOutcome:
    return FetchOutcome(
        failure=FetchFailure(
            url=url, reason=reason, detail=detail, status_code=status, methods_tried=["http"]
        )
    )


def _decode(body: bytes, response: httpx.Response) -> str:
    """Decode a response body, preferring the declared charset."""
    if not body:
        return ""
    encoding = response.charset_encoding
    if not encoding:
        match = _META_CHARSET.search(body[:4096])
        if match:
            encoding = match.group(1).decode("ascii", "ignore")
    for candidate in (encoding, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")
