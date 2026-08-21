"""Single-URL scrape.

``/v1/research`` answers "what does this company do?" by crawling a domain.
This answers a different question -- "what is on this page?" -- and it exists
because the crawl is the wrong tool twice over:

* the caller already knows the exact URL, so discovery and ranking are waste;
* the caller needs an answer in seconds, and a multi-page crawl cannot promise
  that.

Everything security-relevant is unchanged. The URL passes through the same
:class:`UrlGuard`, the same pinned transport, the same robots policy and the
same redirect revalidation as any crawled page. What is skipped is discovery,
ranking, boilerplate removal and assembly -- none of which have a meaning for a
single page.

Failure is reported in the body, never by inventing content (invariant 1).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import structlog

from app.config.loader import Limits, get_limits
from app.config.settings import Settings
from app.crawl.robots import RobotsPolicy
from app.extract.content import count_words, extract_content, extract_html, truncate_at_paragraph
from app.extract.metadata import extract_metadata
from app.fetch.http import HttpFetcher
from app.fetch.renderer import Renderer
from app.fetch.sufficiency import assess, looks_parked
from app.models.internal import Deadline
from app.models.request import ScrapeRequest
from app.models.response import ScrapeResponse
from app.security.reasons import ReasonCode
from app.security.url_guard import GuardError, UrlGuard

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Fetched:
    html: str
    final_url: str
    method: str


class ScrapeService:
    """Fetch one URL and return its cleaned content.

    Dependencies are held explicitly rather than read from ``app.state``, for
    the same reason :class:`ResearchService` holds them: the crawl layer stays
    framework-free and directly testable.
    """

    def __init__(
        self,
        *,
        guard: UrlGuard,
        renderer: Renderer,
        settings: Settings,
        limits: Limits | None = None,
        semaphore=None,
    ) -> None:
        self.guard = guard
        self.renderer = renderer
        self.settings = settings
        self.limits = limits or get_limits()
        self.semaphore = semaphore

    async def run(
        self, payload: ScrapeRequest, *, request_id: str | None = None, key_id: str = "-"
    ) -> ScrapeResponse:
        request_id = request_id or f"scr_{uuid.uuid4().hex[:16]}"
        started = time.monotonic()

        cfg = self.settings.scrape
        deadline_ms = min(
            payload.deadline_ms or cfg.default_deadline_ms, cfg.max_deadline_ms
        )
        deadline = Deadline(deadline_ms)
        budget = self.limits.budget("quick")

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        def failed(reason: ReasonCode | str, notes: list[str] | None = None) -> ScrapeResponse:
            return ScrapeResponse(
                request_id=request_id,
                status="failed",
                status_reason=str(reason),
                url=payload.url,
                output_format=payload.output_format,
                elapsed_ms=elapsed(),
                notes=notes or [],
            )

        try:
            target = await self.guard.validate(payload.url, None)
        except GuardError as exc:
            log.info("scrape.rejected", request_id=request_id, reason=str(exc.reason))
            return failed(exc.reason)

        fetcher = HttpFetcher(
            self.guard,
            user_agent=self.settings.user_agent,
            budget=budget,
            max_redirect_hops=self.settings.security.max_redirect_hops,
        )
        notes: list[str] = []
        try:
            robots = RobotsPolicy(self.settings.robots, fetcher)
            # Build the origin exactly as RobotsPolicy.is_allowed does, so the
            # load populates the cache key that lookup will use.
            split = urlsplit(target.url)
            origin = f"{split.scheme}://{split.netloc}"
            await robots.load(origin, deadline)
            allowed, robots_reason = robots.is_allowed(target.url, is_seed=True)
            if not allowed:
                return failed(robots_reason or ReasonCode.ROBOTS_DISALLOWED)

            fetched = await self._acquire(payload, target.url, fetcher, deadline, budget, notes)
        finally:
            await fetcher.aclose()

        if fetched is None:
            return failed(self._last_reason, notes)

        if looks_parked(fetched.html):
            return failed(ReasonCode.PARKED_DOMAIN, notes)

        metadata = extract_metadata(
            fetched.html,
            max_headings=self.limits.response["max_headings"],
            max_heading_chars=self.limits.response["max_heading_chars"],
        )

        if payload.output_format == "html":
            content = extract_html(fetched.html, limit=cfg.max_html_chars)
            truncated = len(content) >= cfg.max_html_chars
        else:
            content = await extract_content(
                fetched.html, fetched.final_url, output_format=payload.output_format
            )
            content, truncated = truncate_at_paragraph(content, cfg.max_content_chars)

        words = count_words(content)
        status = "success" if words >= self.limits.sufficiency["min_words_internal"] else "partial"

        log.info(
            "scrape.completed",
            request_id=request_id,
            key_id=key_id,
            url=payload.url,
            status=status,
            words=words,
            method=fetched.method,
            format=payload.output_format,
            elapsed_ms=elapsed(),
        )
        return ScrapeResponse(
            request_id=request_id,
            status=status,
            status_reason=None if status == "success" else ReasonCode.TOO_THIN.value,
            url=payload.url,
            final_url=fetched.final_url if fetched.final_url != payload.url else None,
            title=metadata.title,
            headings=list(metadata.headings),
            word_count=words,
            output_format=payload.output_format,
            content=content,
            truncated=truncated,
            fetch_method=fetched.method,
            elapsed_ms=elapsed(),
            notes=notes,
        )

    # ------------------------------------------------------------------

    _last_reason: ReasonCode = ReasonCode.CONNECTION_FAILED

    async def _acquire(
        self,
        payload: ScrapeRequest,
        url: str,
        fetcher: HttpFetcher,
        deadline: Deadline,
        budget,
        notes: list[str],
    ) -> _Fetched | None:
        """Get the markup, honouring the caller's render mode.

        ``never`` is the real-time path: no browser, so a JS-only page comes back
        thin and says so rather than spending three seconds discovering that.
        """
        if payload.render == "always":
            rendered = await self._render(url, deadline, budget)
            if rendered is not None:
                return rendered
            notes.append("render_failed_fell_back_to_http")

        outcome = await fetcher.fetch(
            url, deadline=deadline, scope=None, allow_domain_rebase=True,
            max_bytes=budget.max_response_bytes,
        )
        if not outcome.ok:
            self._last_reason = outcome.failure.reason
            # A refused page is worth one browser attempt, for the same reason a
            # refused homepage is: the block is often a transport fingerprint
            # rather than a decision about us.
            if payload.render != "never" and outcome.failure.reason == ReasonCode.BOT_BLOCKED:
                rendered = await self._render(url, deadline, budget)
                if rendered is not None:
                    notes.append("blocked_over_http_read_with_browser")
                    return rendered
            return None

        result = outcome.result
        if payload.render == "never":
            return _Fetched(result.html, result.final_url, "http")

        verdict = assess(
            result.html,
            is_homepage=False,
            min_words_homepage=self.limits.sufficiency["min_words_homepage"],
            min_words_internal=self.limits.sufficiency["min_words_internal"],
        )
        if verdict.sufficient:
            return _Fetched(result.html, result.final_url, "http")

        rendered = await self._render(result.final_url, deadline, budget)
        if rendered is not None:
            notes.append("rendered_because_http_content_was_thin")
            return rendered
        self._last_reason = ReasonCode.TOO_THIN
        return _Fetched(result.html, result.final_url, "http")

    async def _render(self, url: str, deadline: Deadline, budget) -> _Fetched | None:
        if not self.renderer.available:
            return None
        remaining = deadline.remaining_ms
        if remaining <= 500:
            return None
        timeout = int(min(budget.render_timeout_ms, remaining))
        outcome = await self.renderer.render(url, scope=None, timeout_ms=timeout)
        if not outcome.ok or not outcome.html:
            return None
        return _Fetched(outcome.html, outcome.final_url or url, "browser")
