"""The crawl orchestrator.

Architecture reference: SS2 (request lifecycle), SS5 (render rationing),
SS6 (two-wave depth-2 crawl), SS8 (budgeting), SS11 (status model).

This module imports no web framework.  It takes a
:class:`~app.models.request.ResearchRequest` and returns a
:class:`~app.models.response.ResearchResponse`, which is what makes adding the
async job API later a wiring change rather than a rewrite (SS14).

Invariant 3 is enforced here: every stage consults the deadline before starting
new work, in-flight work is cancelled when it expires, and the result is
assembled from whatever completed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import structlog

from app.config.loader import CrawlBudget, Limits, get_lexicon, get_limits
from app.crawl import discover, rank
from app.crawl.normalize import deduplicate, normalize_url
from app.crawl.robots import RobotsPolicy
from app.extract.boilerplate import remove_boilerplate
from app.extract.content import extract_content, split_blocks, truncate_at_paragraph
from app.extract.jsonld import extract_organization, iter_jsonld
from app.extract.metadata import extract_metadata
from app.fetch.host_limiter import HostLimiter
from app.fetch.http import HTML_TYPES, TEXT_TYPES, HttpFetcher
from app.fetch.renderer import Renderer
from app.fetch.sufficiency import assess, count_words, looks_parked
from app.models.internal import (
    Candidate,
    Deadline,
    ExtractedPage,
    FetchFailure,
    FetchMethod,
    FetchResult,
)
from app.models.request import ResearchRequest
from app.models.response import (
    Coverage,
    CrawlSummary,
    FailedPage,
    PageResult,
    ResearchResponse,
    SiteInfo,
)
from app.security.reasons import ReasonCode
from app.security.url_guard import CrawlScope, GuardError, UrlGuard, coerce_input_url

log = structlog.get_logger(__name__)

#: Reserved for boilerplate removal, budgeting and serialisation (SS2).
ASSEMBLY_RESERVE_MS = 2000

#: Page types the coverage block reports on.
IMPORTANT_PAGE_TYPES = (
    "about",
    "services",
    "solutions",
    "products",
    "case_study",
    "industries",
    "customers",
    "testimonials",
    "pricing",
)


@dataclass(slots=True)
class CrawlState:
    """Mutable per-request state.  Never shared between requests."""

    deadline: Deadline
    budget: CrawlBudget
    scope: CrawlScope | None = None
    js_mode: bool = False
    renders_used: int = 0
    pages: list[ExtractedPage] = field(default_factory=list)
    failures: list[FetchFailure] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    urls_discovered: int = 0
    urls_after_filtering: int = 0
    pages_selected: int = 0
    seen_urls: set[str] = field(default_factory=set)
    content_hashes: set[int] = field(default_factory=set)
    robots_notes: list[str] = field(default_factory=list)
    #: Every scored candidate, kept for the debug block only.  This is what
    #: makes lexicon gaps visible during calibration: a page that keeps being
    #: discovered, never selected and classified "other" is vocabulary the
    #: lexicon does not yet know.
    scored_candidates: list[Candidate] = field(default_factory=list)

    @property
    def max_renders(self) -> int:
        return (
            self.budget.max_renders_js_mode if self.js_mode else self.budget.max_renders
        )

    @property
    def max_pages(self) -> int:
        return self.budget.js_mode_max_pages if self.js_mode else self.budget.max_pages

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)


class ResearchOrchestrator:
    """Runs one company-website research request, start to finish."""

    def __init__(
        self,
        guard: UrlGuard,
        fetcher: HttpFetcher,
        renderer: Renderer,
        robots: RobotsPolicy,
        limits: Limits | None = None,
    ) -> None:
        self.guard = guard
        self.fetcher = fetcher
        self.renderer = renderer
        self.robots = robots
        self.limits = limits or get_limits()

    # ------------------------------------------------------------------

    async def run(
        self,
        request: ResearchRequest,
        *,
        request_id: str,
        budget: CrawlBudget | None = None,
    ) -> ResearchResponse:
        budget = budget or self.limits.budget(request.research_depth)
        state = CrawlState(deadline=Deadline(total_ms=budget.deadline_ms), budget=budget)

        try:
            seed_raw = coerce_input_url(request.url)
        except GuardError as exc:
            return self._failed(request, request_id, exc.reason, state)

        try:
            await self.guard.validate(seed_raw, scope=None)
        except GuardError as exc:
            return self._failed(request, request_id, exc.reason, state)

        seed_origin = _origin_of(seed_raw)
        await self.robots.load(seed_origin, state.deadline)
        allowed, reason = self.robots.is_allowed(seed_raw, is_seed=True)
        if not allowed:
            return self._failed(request, request_id, reason or ReasonCode.ROBOTS_DISALLOWED, state)

        seed_result = await self._fetch_seed(seed_raw, state)
        if seed_result is None:
            failure = state.failures[0] if state.failures else None
            return self._failed(
                request,
                request_id,
                failure.reason if failure else ReasonCode.CONNECTION_FAILED,
                state,
            )

        # Scope is established once, from the homepage's final URL, then frozen.
        # This must happen before the homepage is extracted, because link
        # discovery filters outlinks against the scope.
        final_host = urlsplit(seed_result.final_url).hostname or ""
        state.scope = CrawlScope(
            registrable_domain=_registrable(final_host),
            seed_host=final_host,
            seed_url=seed_result.final_url,
        )

        homepage = await self._extract(
            seed_result,
            state,
            page_type="homepage",
            category="homepage",
            score=1000.0,
            depth=0,
            discovered_via="seed",
        )
        if homepage is None:
            state.failures.append(
                FetchFailure(url=seed_result.final_url, reason=ReasonCode.TOO_THIN)
            )
            return self._failed(request, request_id, ReasonCode.TOO_THIN, state)

        state.pages.append(homepage)
        state.seen_urls.add(_key_url(homepage.final_url))
        state.seen_urls.add(_key_url(homepage.url))

        final_origin = _origin_of(homepage.final_url)
        if final_origin != seed_origin:
            await self.robots.load(final_origin, state.deadline)

        context = rank.RankContext.build(
            state.scope.registrable_domain, homepage.final_url, request.focus
        )

        limiter = HostLimiter(
            budget.http_concurrency_per_host, crawl_delay_s=self.robots.crawl_delay_s
        )

        pool = await self._discover(homepage, state, context)
        await self._run_wave_one(pool, state, context, limiter)
        await self._run_wave_two(state, context, limiter)
        await self._thin_site_fallback(pool, state, context, limiter)

        return self._assemble(request, request_id, state, homepage)

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------

    async def _fetch_seed(self, seed_url: str, state: CrawlState) -> FetchResult | None:
        """Fetch the homepage and apply the sufficiency test / render fallback.

        Returns the raw fetch result; the caller freezes the crawl scope from
        its final URL before extraction, because link discovery needs the scope.
        """
        outcome = await self.fetcher.fetch(
            seed_url,
            deadline=state.deadline,
            scope=None,
            allow_domain_rebase=True,
            accept_types=HTML_TYPES,
            max_bytes=state.budget.max_response_bytes,
        )
        if not outcome.ok:
            state.failures.append(outcome.failure)
            return None

        result = outcome.result
        verdict = assess(
            result.html,
            is_homepage=True,
            min_words_homepage=self.limits.sufficiency["min_words_homepage"],
            min_words_internal=self.limits.sufficiency["min_words_internal"],
        )

        if verdict.reason == "parked":
            state.failures.append(
                FetchFailure(url=result.final_url, reason=ReasonCode.PARKED_DOMAIN)
            )
            return None

        if not verdict.sufficient:
            rendered = await self._render(result.final_url, state, scope=None)
            if rendered is None:
                reason = (
                    ReasonCode.BOT_BLOCKED
                    if verdict.reason == "challenge"
                    else ReasonCode.TOO_THIN
                )
                # Record how far short the page fell.  Tuning the sufficiency
                # thresholds is impossible from a bare verdict: the operator
                # needs to know whether a site missed by 5 words or by 200.
                state.failures.append(
                    FetchFailure(
                        url=result.final_url,
                        reason=reason,
                        detail=(
                            f"sufficiency={verdict.reason} words={verdict.word_count} "
                            f"threshold={self.limits.sufficiency['min_words_homepage']}"
                        ),
                    )
                )
                return None
            result = rendered
            # A homepage that needed rendering means the whole site is a JS app.
            state.js_mode = True
            state.note("js_site_mode")

        return result

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover(
        self,
        homepage: ExtractedPage,
        state: CrawlState,
        context: rank.RankContext,
    ) -> dict[str, Candidate]:
        assert state.scope is not None
        anchors = homepage.outlinks

        sitemap_candidates: list[Candidate] = []
        if state.deadline.has(4000):
            sitemap_candidates = await self._discover_sitemaps(homepage, state)

        pool = discover.merge_candidates(anchors, sitemap_candidates)
        state.urls_discovered = len(pool)

        filtered = {
            url: candidate
            for url, candidate in pool.items()
            if _key_url(url) not in state.seen_urls
            and self.robots.is_allowed(url)[0]
        }
        state.urls_after_filtering = len(filtered)

        for candidate in filtered.values():
            rank.score_candidate(candidate, context)
        state.scored_candidates.extend(filtered.values())
        return filtered

    async def _discover_sitemaps(
        self, homepage: ExtractedPage, state: CrawlState
    ) -> list[Candidate]:
        assert state.scope is not None
        origin = _origin_of(homepage.final_url)
        urls: list[str] = []
        for declared in self.robots.sitemaps():
            normalized = normalize_url(declared, base=homepage.final_url)
            if normalized:
                urls.append(normalized)
        urls.append(f"{origin}/sitemap.xml")
        urls.append(f"{origin}/sitemap_index.xml")

        entries: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        # Two sitemap files maximum, as specified.
        for url in deduplicate(urls)[:2]:
            if not state.deadline.has(3000) or len(entries) >= state.budget.sitemap_url_cap:
                break
            outcome = await self.fetcher.fetch(
                url,
                deadline=state.deadline,
                scope=state.scope,
                accept_types=TEXT_TYPES,
                max_bytes=min(state.budget.max_response_bytes, 4_000_000),
                total_timeout_s=min(4.0, max(0.5, state.deadline.remaining_s - 2)),
            )
            if not outcome.ok:
                continue
            pages, nested = discover.parse_sitemap(
                outcome.result.html, state.budget.sitemap_url_cap
            )
            entries.extend(pages)
            for nested_url in nested[:1]:
                if nested_url in seen or not state.deadline.has(3000):
                    continue
                seen.add(nested_url)
                nested_outcome = await self.fetcher.fetch(
                    nested_url,
                    deadline=state.deadline,
                    scope=state.scope,
                    accept_types=TEXT_TYPES,
                    max_bytes=min(state.budget.max_response_bytes, 4_000_000),
                    total_timeout_s=min(3.0, max(0.5, state.deadline.remaining_s - 2)),
                )
                if nested_outcome.ok:
                    more, _ = discover.parse_sitemap(
                        nested_outcome.result.html, state.budget.sitemap_url_cap
                    )
                    entries.extend(more)
            if entries:
                break

        return discover.candidates_from_sitemap(
            entries[: state.budget.sitemap_url_cap],
            scope_domain=state.scope.registrable_domain,
            base_url=homepage.final_url,
        )

    # ------------------------------------------------------------------
    # Waves
    # ------------------------------------------------------------------

    async def _run_wave_one(
        self,
        pool: dict[str, Candidate],
        state: CrawlState,
        context: rank.RankContext,
        limiter: HostLimiter,
    ) -> None:
        slots = state.max_pages - len(state.pages)
        if slots <= 0:
            state.stopped_reason = "budget_reached"
            return

        selected = rank.select(
            list(pool.values()),
            slots=slots,
            min_score=state.budget.min_score_select,
            context=context,
            already_selected=[],
        )
        if not selected:
            state.stopped_reason = state.stopped_reason or "candidates_exhausted"
            return

        state.pages_selected += len(selected)
        await self._fetch_wave(selected, state, limiter)

    async def _run_wave_two(
        self,
        state: CrawlState,
        context: rank.RankContext,
        limiter: HostLimiter,
    ) -> None:
        if state.budget.max_depth < 2 or state.deadline.expired:
            return
        slots = state.max_pages - len(state.pages)
        if slots <= 0:
            state.stopped_reason = "budget_reached"
            return

        lexicon = get_lexicon()
        children: list[Candidate] = []
        for page in state.pages:
            if page.depth != 1:
                continue
            if not rank.is_hub(page):
                continue
            page.is_hub = True
            children.extend(rank.hub_children(page, lexicon))

        if not children:
            return

        pool = discover.merge_candidates(children)
        fresh = [
            candidate
            for url, candidate in pool.items()
            if _key_url(url) not in state.seen_urls and self.robots.is_allowed(url)[0]
        ]
        if not fresh:
            return

        for candidate in fresh:
            rank.score_candidate(candidate, context)
        state.scored_candidates.extend(fresh)

        selected = rank.select(
            fresh,
            slots=slots,
            min_score=state.budget.min_score_select,
            context=context,
            already_selected=[
                Candidate(url=p.url, category=p.category) for p in state.pages
            ],
        )
        if not selected:
            return

        state.pages_selected += len(selected)
        await self._fetch_wave(selected, state, limiter)

    async def _fetch_wave(
        self, candidates: list[Candidate], state: CrawlState, limiter: HostLimiter
    ) -> None:
        """Fetch a wave concurrently, cancelling anything the deadline outlives."""
        remaining = state.deadline.remaining_ms - ASSEMBLY_RESERVE_MS
        if remaining <= 0:
            state.stopped_reason = "deadline"
            return

        tasks = [
            asyncio.create_task(self._fetch_and_extract(candidate, state, limiter))
            for candidate in candidates
        ]
        done, pending = await asyncio.wait(tasks, timeout=remaining / 1000.0)

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            state.stopped_reason = "deadline"
            for task, candidate in zip(tasks, candidates, strict=False):
                if task in pending:
                    state.failures.append(
                        FetchFailure(
                            url=candidate.url,
                            reason=ReasonCode.DEADLINE_EXCEEDED,
                            methods_tried=["http"],
                        )
                    )

        for task in tasks:
            if task in pending or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                log.warning("wave.task_failed", error=str(error))
                continue
            page = task.result()
            if page is None:
                continue
            if len(state.pages) >= state.max_pages:
                state.stopped_reason = "budget_reached"
                break
            state.pages.append(page)

        if state.stopped_reason is None and len(state.pages) >= state.max_pages:
            state.stopped_reason = "budget_reached"

    async def _fetch_and_extract(
        self, candidate: Candidate, state: CrawlState, limiter: HostLimiter
    ) -> ExtractedPage | None:
        key = _key_url(candidate.url)
        if key in state.seen_urls:
            return None
        state.seen_urls.add(key)

        async with limiter:
            if state.deadline.remaining_ms <= ASSEMBLY_RESERVE_MS:
                state.failures.append(
                    FetchFailure(url=candidate.url, reason=ReasonCode.DEADLINE_EXCEEDED)
                )
                return None

            result: FetchResult | None = None
            thin_but_usable: FetchResult | None = None
            if not state.js_mode:
                outcome = await self.fetcher.fetch(
                    candidate.url,
                    deadline=state.deadline,
                    scope=state.scope,
                    accept_types=HTML_TYPES,
                    max_bytes=state.budget.max_response_bytes,
                )
                if outcome.ok:
                    verdict = assess(
                        outcome.result.html,
                        is_homepage=False,
                        min_words_homepage=self.limits.sufficiency["min_words_homepage"],
                        min_words_internal=self.limits.sufficiency["min_words_internal"],
                    )
                    if verdict.sufficient:
                        result = outcome.result
                    elif verdict.reason == "parked":
                        state.failures.append(
                            FetchFailure(
                                url=candidate.url, reason=ReasonCode.PARKED_DOMAIN
                            )
                        )
                        return None
                    elif verdict.word_count > 0:
                        # Genuinely thin, but real HTML with real text.  Index
                        # pages routinely look like this -- a grid of case-study
                        # thumbnails carries almost no prose but is the only way
                        # to reach the case studies themselves.  Keep it as a
                        # fallback so wave 2 can harvest its links; the
                        # min_words_keep_page filter decides whether it is also
                        # returned as evidence.
                        thin_but_usable = outcome.result
                else:
                    if outcome.failure.reason in (
                        ReasonCode.OUT_OF_SCOPE,
                        ReasonCode.PRIVATE_ADDRESS,
                        ReasonCode.NON_HTML_CONTENT,
                        ReasonCode.HTTP_ERROR_4XX,
                        ReasonCode.ROBOTS_DISALLOWED,
                    ):
                        state.failures.append(outcome.failure)
                        return None
                    state.failures.append(outcome.failure)

            if result is None:
                rendered = await self._render(candidate.url, state, scope=state.scope)
                if rendered is not None:
                    result = rendered
                    state.failures = [f for f in state.failures if f.url != candidate.url]
                elif thin_but_usable is not None:
                    result = thin_but_usable
                else:
                    if not any(f.url == candidate.url for f in state.failures):
                        state.failures.append(
                            FetchFailure(
                                url=candidate.url,
                                reason=ReasonCode.TOO_THIN,
                                methods_tried=["http", "browser"],
                            )
                        )
                    return None

        return await self._extract(
            result,
            state,
            page_type=candidate.page_type,
            category=candidate.category,
            score=candidate.score,
            depth=candidate.depth,
            discovered_via=candidate.discovered_via,
        )

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    async def _render(
        self, url: str, state: CrawlState, *, scope: CrawlScope | None
    ) -> FetchResult | None:
        if not self.renderer.available:
            return None
        if state.renders_used >= state.max_renders:
            return None
        # The deadline always wins: skip rendering entirely when it is tight.
        if not state.deadline.has(state.budget.render_timeout_ms + ASSEMBLY_RESERVE_MS):
            return None

        state.renders_used += 1
        timeout = int(
            min(
                state.budget.render_timeout_ms,
                max(1000, state.deadline.remaining_ms - ASSEMBLY_RESERVE_MS),
            )
        )
        outcome = await self.renderer.render(url, scope=scope, timeout_ms=timeout)
        if not outcome.ok or not outcome.html:
            return None
        if looks_parked(outcome.html):
            return None
        return FetchResult(
            url=url,
            final_url=outcome.final_url or url,
            status_code=outcome.status_code or 200,
            html=outcome.html,
            method=FetchMethod.BROWSER,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    async def _extract(
        self,
        result: FetchResult,
        state: CrawlState,
        *,
        page_type: str,
        category: str,
        score: float,
        depth: int,
        discovered_via: str,
    ) -> ExtractedPage | None:
        response_cfg = self.limits.response
        metadata = extract_metadata(
            result.html,
            max_headings=int(response_cfg["max_headings"]),
            max_heading_chars=int(response_cfg["max_heading_chars"]),
        )
        text = await extract_content(result.html, result.final_url)
        if not text.strip():
            return None

        digest = hash(text[:4000])
        if digest in state.content_hashes:
            # Same content under a different URL (locale duplicate, /x vs
            # /x/index.html that survived normalization).
            return None
        state.content_hashes.add(digest)

        outlinks: list[Candidate] = []
        if state.scope is not None:
            outlinks = discover.extract_links(
                result.html,
                result.final_url,
                scope_domain=state.scope.registrable_domain,
                depth=depth + 1,
                discovered_via=result.final_url,
            )

        return ExtractedPage(
            url=result.url,
            final_url=result.final_url,
            page_type=page_type,
            category=category,
            score=score,
            depth=depth,
            discovered_via=discovered_via,
            method=result.method,
            status_code=result.status_code,
            title=metadata.title,
            meta_description=metadata.meta_description,
            h1=metadata.h1,
            headings=metadata.headings,
            language=metadata.language,
            text=text,
            blocks=split_blocks(text),
            jsonld=iter_jsonld(result.html)
            if page_type in ("homepage", "about")
            else [],
            outlinks=outlinks,
            word_count=count_words(text),
        )

    # ------------------------------------------------------------------
    # Thin-site blog fallback (SS7)
    # ------------------------------------------------------------------

    async def _thin_site_fallback(
        self,
        pool: dict[str, Candidate],
        state: CrawlState,
        context: rank.RankContext,
        limiter: HostLimiter,
    ) -> None:
        config = self.limits.thin_site_fallback
        if not config.get("enabled", True):
            return

        total_words = sum(page.word_count for page in state.pages)
        thin = (
            len(state.pages) < int(config["max_pages_scraped"])
            or total_words < int(config["min_total_words"])
        )
        if not thin:
            return
        if not state.deadline.has(int(config["min_remaining_ms"])):
            return

        blog_candidates = [
            candidate
            for url, candidate in pool.items()
            if candidate.category == "blog"
            and candidate.page_type == "blog_post"
            and _key_url(url) not in state.seen_urls
            and self.robots.is_allowed(url)[0]
        ]
        if not blog_candidates:
            return

        def freshness(candidate: Candidate) -> tuple[int, str]:
            parsed = rank.parse_lastmod(candidate.lastmod)
            return (0 if parsed is None else int(parsed.timestamp()), candidate.url)

        blog_candidates.sort(key=freshness, reverse=True)
        chosen = blog_candidates[0]

        # HTTP only: the render budget must not be spent on a fallback.
        async with limiter:
            outcome = await self.fetcher.fetch(
                chosen.url,
                deadline=state.deadline,
                scope=state.scope,
                accept_types=HTML_TYPES,
                max_bytes=state.budget.max_response_bytes,
                total_timeout_s=min(
                    state.budget.total_timeout_s,
                    max(1.0, state.deadline.remaining_s - ASSEMBLY_RESERVE_MS / 1000),
                ),
            )
        state.seen_urls.add(_key_url(chosen.url))
        if not outcome.ok:
            state.failures.append(outcome.failure)
            return

        page = await self._extract(
            outcome.result,
            state,
            page_type="blog_post",
            category="blog",
            score=chosen.score,
            depth=chosen.depth,
            discovered_via=chosen.discovered_via,
        )
        if page is None:
            return

        state.pages.append(page)
        state.note("thin_site_blog_fallback")

        parsed = rank.parse_lastmod(chosen.lastmod)
        if parsed is not None:
            from datetime import UTC, datetime

            age_days = (datetime.now(UTC) - parsed).days
            if age_days > int(config["stale_after_months"]) * 31:
                state.note("blog_fallback_stale")
        else:
            state.note("blog_fallback_stale")

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def _assemble(
        self,
        request: ResearchRequest,
        request_id: str,
        state: CrawlState,
        homepage: ExtractedPage,
    ) -> ResearchResponse:
        boilerplate_cfg = self.limits.boilerplate

        pages = sorted(
            state.pages,
            key=lambda p: (0 if p.is_homepage else 1, -p.score, p.url),
        )

        cleaned = remove_boilerplate(
            [page.blocks for page in pages],
            min_pages=int(boilerplate_cfg["min_pages"]),
            document_frequency=float(boilerplate_cfg["document_frequency"]),
            max_block_chars=int(boilerplate_cfg["max_block_chars"]),
            site_wide_cap=int(boilerplate_cfg["site_wide_content_cap"]),
        )
        for page, text in zip(pages, cleaned.pages, strict=False):
            page.text = text
            page.word_count = count_words(text)

        kept: list[ExtractedPage] = []
        dropped_thin = 0
        for page in pages:
            if page.word_count < state.budget.min_words_keep_page and not page.is_homepage:
                state.failures.append(
                    FetchFailure(
                        url=page.final_url,
                        reason=ReasonCode.TOO_THIN,
                        detail=(
                            f"words={page.word_count} "
                            f"threshold={state.budget.min_words_keep_page} "
                            f"page_type={page.page_type}"
                        ),
                    )
                )
                dropped_thin += 1
                continue
            kept.append(page)
        if dropped_thin:
            state.note(f"{dropped_thin}_pages_below_word_threshold_dropped")

        page_results, content_chars, char_budget_hit = self._apply_budgets(kept, state)
        if char_budget_hit and state.stopped_reason is None:
            state.stopped_reason = "char_budget_reached"
        if state.stopped_reason is None:
            state.stopped_reason = (
                "deadline" if state.deadline.expired else "candidates_exhausted"
            )

        site = self._build_site(request, state, homepage, cleaned.site_wide_content)
        total_words = sum(p.word_count for p in kept)
        evidence = self._evidence_sufficient(len(kept), total_words)

        if not kept:
            status = "failed"
            status_reason = (
                str(state.failures[0].reason) if state.failures else str(ReasonCode.TOO_THIN)
            )
        elif len(kept) >= 2 and evidence:
            status = "success"
            status_reason = None
        else:
            status = "partial"
            status_reason = (
                "deadline_exceeded"
                if state.stopped_reason == "deadline"
                else "insufficient_evidence"
            )

        found = sorted({p.page_type for p in kept})
        missing = sorted(t for t in IMPORTANT_PAGE_TYPES if t not in found)

        response = ResearchResponse(
            request_id=request_id,
            status=status,
            status_reason=status_reason,
            evidence_sufficient=evidence,
            site=site,
            pages=page_results,
            failed_pages=_dedupe_failures(state.failures),
            coverage=Coverage(found=found, missing=missing),
            crawl_summary=CrawlSummary(
                pages_scraped=len(page_results),
                pages_failed=len(_dedupe_failures(state.failures)),
                browser_renders=state.renders_used,
                max_depth_reached=max((p.depth for p in kept), default=0),
                total_words=total_words,
                content_chars=content_chars,
                elapsed_ms=state.deadline.elapsed_ms,
                stopped_reason=state.stopped_reason,
                cache_hit=False,
            ),
            notes=state.notes,
        )

        if request.advanced is not None and request.advanced.debug:
            response.debug = self._debug_block(state, kept)
        return response

    def _apply_budgets(
        self, pages: list[ExtractedPage], state: CrawlState
    ) -> tuple[list[PageResult], int, bool]:
        """Apply tiered per-page caps and the global content budget (SS8).

        Truncation walks from the lowest-ranked page upward, so the homepage and
        the top case study are never what gets cut.
        """
        budget = state.budget
        response_cfg = self.limits.response

        caps: list[int] = []
        for index in range(len(pages)):
            caps.append(
                budget.page_content_cap_top
                if index < budget.page_content_cap_top_ranks
                else budget.page_content_cap_rest
            )

        texts: list[str] = []
        truncated_flags: list[bool] = []
        for page, cap in zip(pages, caps, strict=False):
            text, was_truncated = truncate_at_paragraph(page.text, cap)
            texts.append(text)
            truncated_flags.append(was_truncated)

        total = sum(len(t) for t in texts)
        char_budget_hit = False
        index = len(texts) - 1
        while total > budget.global_content_budget and index >= 0:
            overflow = total - budget.global_content_budget
            current = len(texts[index])
            if current == 0:
                index -= 1
                continue
            keep = max(0, current - overflow)
            trimmed, was_truncated = truncate_at_paragraph(texts[index], keep)
            total -= current - len(trimmed)
            texts[index] = trimmed
            truncated_flags[index] = truncated_flags[index] or was_truncated or keep < current
            char_budget_hit = True
            index -= 1

        results: list[PageResult] = []
        content_chars = 0
        max_headings = int(response_cfg["max_headings"])
        for page, text, was_truncated in zip(pages, texts, truncated_flags, strict=False):
            if not text.strip():
                continue
            content_chars += len(text)
            results.append(
                PageResult(
                    url=page.final_url,
                    final_url=None if page.final_url == page.url else page.url,
                    page_type=page.page_type,  # type: ignore[arg-type]
                    title=page.title,
                    headings=page.headings[:max_headings],
                    word_count=count_words(text),
                    truncated=was_truncated,
                    content=text,
                )
            )
        return results, content_chars, char_budget_hit

    def _build_site(
        self,
        request: ResearchRequest,
        state: CrawlState,
        homepage: ExtractedPage,
        site_wide_content: str | None,
    ) -> SiteInfo:
        response_cfg = self.limits.response
        documents: list[dict] = []
        for page in state.pages:
            documents.extend(page.jsonld)

        organization, jsonld_name, jsonld_tagline = extract_organization(
            documents,
            description_cap=int(response_cfg["jsonld_description_cap"]),
            total_cap=int(response_cfg["organization_cap_chars"]),
        )

        site_name = jsonld_name or _title_company(homepage.title)
        tagline = jsonld_tagline or homepage.h1 or homepage.meta_description

        requested = request.url
        canonical = state.scope.registrable_domain if state.scope else None
        rebased = bool(
            state.scope
            and canonical
            and canonical != _registrable(urlsplit(coerce_input_url(requested)).hostname or "")
        )

        return SiteInfo(
            requested_url=requested,
            final_url=homepage.final_url,
            canonical_domain=canonical,
            domain_rebased=rebased,
            language=homepage.language,
            site_name=site_name,
            tagline=tagline,
            organization=organization if organization and not organization.is_empty() else None,
            site_wide_content=site_wide_content,
        )

    def _evidence_sufficient(self, page_count: int, total_words: int) -> bool:
        config = self.limits.evidence
        if page_count >= int(config["multi_page_min_pages"]):
            return total_words >= int(config["multi_page_min_words"])
        if page_count == 1:
            return total_words >= int(config["single_page_min_words"])
        return False

    def _debug_block(self, state: CrawlState, pages: list[ExtractedPage]) -> dict:
        returned_urls = {_key_url(page.final_url) for page in pages}
        return {
            "urls_discovered": state.urls_discovered,
            "urls_after_filtering": state.urls_after_filtering,
            "pages_selected": state.pages_selected,
            "js_mode": state.js_mode,
            "renders_used": state.renders_used,
            "robots_enabled": self.robots.enabled,
            "robots_crawl_delay_s": self.robots.crawl_delay_s,
            "scope": state.scope.registrable_domain if state.scope else None,
            "pages": [
                {
                    "url": page.final_url,
                    "relevance_score": page.score,
                    "discovered_via": page.discovered_via,
                    "depth": page.depth,
                    "fetch_method": str(page.method),
                    "http_status": page.status_code,
                    "is_hub": page.is_hub,
                    "page_type": page.page_type,
                    "category": page.category,
                }
                for page in pages
            ],
            "failures": [
                {
                    "url": failure.url,
                    "reason": str(failure.reason),
                    "detail": failure.detail[:200],
                    "methods_tried": failure.methods_tried,
                }
                for failure in state.failures
            ],
            "unselected_candidates": [
                {
                    "url": candidate.url,
                    "score": candidate.score,
                    "page_type": candidate.page_type,
                    "category": candidate.category,
                    "concept": candidate.concept_key,
                    "anchor": (candidate.anchor_texts or [""])[0][:60],
                }
                for candidate in sorted(
                    (
                        c
                        for c in state.scored_candidates
                        if _key_url(c.url) not in returned_urls
                    ),
                    key=rank.sort_key,
                )[:15]
            ],
        }

    # ------------------------------------------------------------------

    def _failed(
        self,
        request: ResearchRequest,
        request_id: str,
        reason: ReasonCode,
        state: CrawlState,
    ) -> ResearchResponse:
        """Build a site-level failure.  Always HTTP 200 at the API layer (SS11)."""
        return ResearchResponse(
            request_id=request_id,
            status="failed",
            status_reason=str(reason),
            evidence_sufficient=False,
            site=SiteInfo(requested_url=request.url),
            pages=[],
            failed_pages=_dedupe_failures(state.failures)
            or [FailedPage(url=request.url, reason_code=str(reason))],
            coverage=Coverage(found=[], missing=list(IMPORTANT_PAGE_TYPES)),
            crawl_summary=CrawlSummary(
                elapsed_ms=state.deadline.elapsed_ms,
                stopped_reason="deadline" if state.deadline.expired else None,
            ),
            notes=state.notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin_of(url: str) -> str:
    split = urlsplit(url)
    return f"{split.scheme}://{split.netloc}"


def _registrable(host: str) -> str:
    from app.security.domains import registrable_domain

    return registrable_domain(host)


def _key_url(url: str) -> str:
    normalized = normalize_url(url)
    return (normalized or url).rstrip("/").lower()


def _title_company(title: str | None) -> str | None:
    if not title:
        return None
    for separator in ("|", "–", "—", "::", " - "):
        if separator in title:
            parts = [p.strip() for p in title.split(separator) if p.strip()]
            if len(parts) >= 2:
                return parts[-1][:120]
    return title[:120]


def _dedupe_failures(failures: list[FetchFailure]) -> list[FailedPage]:
    seen: set[str] = set()
    out: list[FailedPage] = []
    for failure in failures:
        key = f"{failure.url}|{failure.reason}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            FailedPage(
                url=failure.url,
                reason_code=str(failure.reason),
                http_status=failure.status_code,
            )
        )
    return out
