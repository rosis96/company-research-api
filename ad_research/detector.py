from __future__ import annotations

import asyncio
import logging
import re
import resource
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from ad_research.classifier import classify_overall
from ad_research.models import AnalysisResult, Observation, PageDiagnostic
from ad_research.qualification import enrich_ad_infrastructure_result
from ad_research.settings import AdResearchSettings
from ad_research.signature_engine import SignatureEngine
from app.security.domains import registrable_domain
from app.security.url_guard import CrawlScope, GuardError, UrlGuard, coerce_input_url

log = logging.getLogger(__name__)

_PRIORITY_PATHS = {
    "pricing": 100,
    "demo": 95,
    "book": 94,
    "get-started": 93,
    "sign-up": 92,
    "signup": 92,
    "plans": 91,
    "quote": 90,
    "product": 90,
    "products": 89,
    "shop": 88,
    "solutions": 87,
    "collections": 86,
    "features": 85,
    "services": 84,
    "how-it-works": 82,
    "contact": 80,
}
_FALLBACK_PATHS = ("/pricing", "/products", "/services", "/contact")
_AVOID_SECONDARY_PATHS = (
    "/blog",
    "/article",
    "/careers",
    "/jobs",
    "/legal",
    "/login",
    "/privacy",
    "/signin",
    "/support",
    "/terms",
)
_BOT_MARKERS = (
    "checking your browser",
    "just a moment...",
    "verify you are human",
    "attention required! | cloudflare",
    "cf-chl-",
    "challenge-platform",
)
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button#didomi-notice-agree-button",
    "button[data-testid='uc-accept-all-button']",
    "button[aria-label*='Accept all cookies' i]",
    "button[aria-label*='Accept cookies' i]",
)
_CONSENT_TEXT = re.compile(
    r"^(?:accept all|accept all cookies|accept cookies|allow all|allow cookies|agree|i agree|got it)$",
    re.IGNORECASE,
)


class BrowserManager:
    """One browser process, isolated context per company, bounded by callers."""

    def __init__(self, settings: AdResearchSettings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._domains_since_restart = 0

    async def start(self) -> None:
        await self._ensure_browser()

    async def close(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def new_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        self._domains_since_restart += 1
        return await browser.new_context(
            user_agent=self.settings.user_agent,
            accept_downloads=False,
            java_script_enabled=True,
            ignore_https_errors=self.settings.ignore_https_errors,
            viewport={"width": 1365, "height": 900},
            service_workers="allow",
        )

    async def invalidate(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser and self._domains_since_restart >= self.settings.recycle_after_domains:
                log.info(
                    "recycling Chromium after %s domains (rss_mb=%.1f)",
                    self._domains_since_restart,
                    _rss_mb(),
                )
                await self._shutdown_locked()
            if self._browser is None:
                self._playwright = await async_playwright().start()
                kwargs = {
                    "headless": self.settings.browser_headless,
                    "args": [
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--mute-audio",
                    ],
                }
                if self.settings.browser_executable_path:
                    kwargs["executable_path"] = self.settings.browser_executable_path
                try:
                    self._browser = await self._playwright.chromium.launch(**kwargs)
                    log.info("Chromium started (rss_mb=%.1f)", _rss_mb())
                except Exception:
                    await self._shutdown_locked()
                    raise
                self._domains_since_restart = 0
            return self._browser

    async def _shutdown_locked(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        self._domains_since_restart = 0


class AdvertisingDetector:
    def __init__(
        self,
        settings: AdResearchSettings,
        browser: BrowserManager,
        guard: UrlGuard,
        signatures: SignatureEngine,
    ) -> None:
        self.settings = settings
        self.browser = browser
        self.guard = guard
        self.signatures = signatures

    async def analyze(
        self, raw_url: str, *, landing_page_urls: list[str] | None = None
    ) -> AnalysisResult:
        started = datetime.now(UTC)
        try:
            normalized = _normalize_url(raw_url)
            target = await self.guard.validate(normalized)
        except GuardError as exc:
            return self._failure(
                raw_url, started, "CRAWL_FAILED", f"invalid or unsafe URL: {exc.detail}"
            )
        except Exception as exc:
            return self._failure(raw_url, started, "CRAWL_FAILED", f"invalid URL: {exc}")

        try:
            async with asyncio.timeout(self.settings.total_timeout_seconds):
                return await self._analyze_validated(
                    target.url, raw_url, started, landing_page_urls or []
                )
        except TimeoutError:
            return self._failure(
                raw_url,
                started,
                "INCONCLUSIVE",
                f"domain crawl exceeded {self.settings.total_timeout_seconds}s timeout",
                domain=target.host,
            )
        except Exception as exc:
            if "Target page, context or browser has been closed" in str(exc):
                await self.browser.invalidate()
            log.exception("ad crawl failed", extra={"url": target.url})
            return self._failure(
                raw_url,
                started,
                "CRAWL_FAILED",
                f"browser crawl failed: {type(exc).__name__}: {str(exc)[:300]}",
                domain=target.host,
            )

    async def _analyze_validated(
        self,
        seed_url: str,
        requested_url: str,
        started: datetime,
        landing_page_urls: list[str],
    ) -> AnalysisResult:
        context = await self.browser.new_context()
        context.set_default_timeout(self.settings.navigation_timeout_ms)
        observations: list[Observation] = []
        pages: list[PageDiagnostic] = []
        notes: list[str] = []
        valid_hosts: dict[str, bool] = {}
        scope_holder: list[CrawlScope | None] = [None]
        phase_holder = ["before_consent"]
        current_page_url = [seed_url]

        async def allow_request(url: str, is_document: bool) -> bool:
            try:
                host = urlsplit(url).hostname or ""
                scope = scope_holder[0] if is_document else None
                if is_document and scope is not None and not scope.contains(host):
                    return False
                if host in valid_hosts:
                    return valid_hosts[host]
                await self.guard.validate(url, scope=scope)
                valid_hosts[host] = True
                return True
            except GuardError:
                valid_hosts[host] = False
                return False

        page = await context.new_page()
        await self._instrument_page(
            page, observations, notes, phase_holder, current_page_url, allow_request
        )
        try:
            home = await self._visit(
                page,
                seed_url,
                observations,
                phase_holder,
                current_page_url,
                attempt_consent=True,
                page_role="homepage",
            )
            pages.append(home)
            if home.error:
                return self._build_result(
                    requested_url=requested_url,
                    seed_url=seed_url,
                    final_url=home.final_url,
                    started=started,
                    pages=pages,
                    observations=observations,
                    notes=notes,
                    status="INCONCLUSIVE",
                    error=home.error,
                    landing_pages_requested=landing_page_urls,
                )

            final_url = home.final_url or seed_url
            final_host = urlsplit(final_url).hostname or urlsplit(seed_url).hostname or ""
            scope_holder[0] = CrawlScope(
                registrable_domain=registrable_domain(final_host),
                seed_host=final_host,
                seed_url=final_url,
            )
            links = await _discover_priority_links(page, final_url, scope_holder[0])
            # Keep two alternates available so a 404 or blocked secondary page
            # does not consume the entire successful-page budget.
            targets = _choose_targets(final_url, links, self.settings.max_pages + 2)

            for url in targets[1:]:
                if sum(not item.error for item in pages) >= self.settings.max_pages:
                    break
                phase_holder[0] = "after_consent" if home.consent_clicked else "before_consent"
                diagnostic = await self._visit(
                    page,
                    url,
                    observations,
                    phase_holder,
                    current_page_url,
                    attempt_consent=not home.consent_clicked,
                    page_role="internal",
                )
                pages.append(diagnostic)

            crawled_urls = {
                (item.final_url or item.url).rstrip("/") for item in pages if not item.error
            }
            for raw_landing_url in landing_page_urls[: self.settings.max_landing_pages]:
                try:
                    landing_url = _normalize_url(raw_landing_url)
                    target = await self.guard.validate(landing_url)
                except Exception as exc:
                    notes.append(
                        f"Skipped unsafe or invalid supplied ad landing page: {str(exc)[:180]}"
                    )
                    continue
                if target.url.rstrip("/") in crawled_urls:
                    continue
                landing_host = urlsplit(target.url).hostname or ""
                scope_holder[0] = CrawlScope(
                    registrable_domain=registrable_domain(landing_host),
                    seed_host=landing_host,
                    seed_url=target.url,
                )
                valid_hosts.pop(landing_host, None)
                phase_holder[0] = "before_consent"
                diagnostic = await self._visit(
                    page,
                    target.url,
                    observations,
                    phase_holder,
                    current_page_url,
                    attempt_consent=True,
                    page_role="ad_landing",
                )
                pages.append(diagnostic)
                if not diagnostic.error:
                    crawled_urls.add((diagnostic.final_url or diagnostic.url).rstrip("/"))

            successful = [item for item in pages if not item.error]
            status = "COMPLETED" if successful else "CRAWL_FAILED"
            if len(successful) == 1 and any(item.error for item in pages[1:]):
                notes.append("Only the homepage could be inspected; secondary pages failed.")
            return self._build_result(
                requested_url=requested_url,
                seed_url=seed_url,
                final_url=final_url,
                started=started,
                pages=pages,
                observations=observations,
                notes=notes,
                status=status,
                landing_pages_requested=landing_page_urls,
            )
        finally:
            await context.close()

    async def _instrument_page(
        self,
        page: Page,
        observations: list[Observation],
        notes: list[str],
        phase_holder: list[str],
        current_page_url: list[str],
        allow_request: Callable[[str, bool], Awaitable[bool]],
    ) -> None:
        def on_request(request) -> None:
            if len(observations) >= 10_000:
                return
            kind = "script" if request.resource_type == "script" else "network"
            observations.append(
                Observation(
                    kind=kind,
                    value=request.url,
                    url=request.url,
                    page_url=current_page_url[0],
                    phase=phase_holder[0],
                )
            )
            if kind == "script":
                observations.append(
                    Observation(
                        kind="network",
                        value=request.url,
                        url=request.url,
                        page_url=current_page_url[0],
                        phase=phase_holder[0],
                    )
                )

        page.context.on("request", on_request)
        page.on("pageerror", lambda exc: notes.append(f"JavaScript error: {str(exc)[:180]}"))

        async def route_handler(route, request) -> None:
            url = request.url
            if url.startswith(("data:", "blob:", "about:")):
                await route.continue_()
                return
            if not url.startswith(("http://", "https://")):
                await route.abort()
                return
            allowed = await allow_request(url, request.resource_type == "document")
            await (route.continue_() if allowed else route.abort())

        await page.route("**/*", route_handler)

    async def _visit(
        self,
        page: Page,
        url: str,
        observations: list[Observation],
        phase_holder: list[str],
        current_page_url: list[str],
        *,
        attempt_consent: bool,
        page_role: str,
    ) -> PageDiagnostic:
        current_page_url[0] = url
        before_count = len(observations)
        diagnostic = PageDiagnostic(url=url, page_role=page_role)
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.navigation_timeout_ms,
            )
            diagnostic.final_url = page.url
            diagnostic.status_code = response.status if response else None
            diagnostic.title = (await page.title())[:300]
            if response and response.status >= 400:
                diagnostic.error = f"HTTP {response.status}"
                return diagnostic
            await _settle(page, self.settings.settle_time_ms)
            await _scroll(page)
            await _settle(page, min(1500, self.settings.settle_time_ms))
            await self._snapshot(page, observations, phase_holder[0], url)

            html = await page.content()
            if _looks_like_bot_challenge(diagnostic.title or "", html):
                diagnostic.error = "bot-protection or challenge page prevented inspection"
                return diagnostic

            if attempt_consent:
                clicked = await _accept_cookie_consent(page)
                if clicked:
                    diagnostic.consent_clicked = True
                    diagnostic.consent_control = clicked
                    phase_holder[0] = "after_consent"
                    await _settle(page, self.settings.post_consent_wait_ms)
                    await _scroll(page)
                    await self._snapshot(page, observations, phase_holder[0], url)
            return diagnostic
        except Exception as exc:
            diagnostic.final_url = page.url if page.url != "about:blank" else None
            diagnostic.error = f"{type(exc).__name__}: {str(exc)[:240]}"
            return diagnostic
        finally:
            page_observations = observations[before_count:]
            diagnostic.requests_observed = sum(item.kind == "network" for item in page_observations)
            diagnostic.scripts_observed = sum(item.kind == "script" for item in page_observations)

    async def _snapshot(
        self,
        page: Page,
        observations: list[Observation],
        phase: str,
        page_url: str,
    ) -> None:
        html = (await page.content())[:1_000_000]
        observations.append(Observation(kind="dom", value=html, page_url=page_url, phase=phase))
        for cookie in await page.context.cookies():
            observations.append(
                Observation(
                    kind="cookie",
                    value=cookie.get("name", ""),
                    page_url=page_url,
                    phase=phase,
                )
            )
        names = self.signatures.exact_global_names()
        existing = await page.evaluate(
            "names => names.filter(name => typeof globalThis[name] !== 'undefined')", names
        )
        for name in existing:
            observations.append(
                Observation(kind="global", value=name, page_url=page_url, phase=phase)
            )

    def _build_result(
        self,
        *,
        requested_url: str,
        seed_url: str,
        final_url: str | None,
        started: datetime,
        pages: list[PageDiagnostic],
        observations: list[Observation],
        notes: list[str],
        status: str,
        error: str | None = None,
        landing_pages_requested: list[str] | None = None,
    ) -> AnalysisResult:
        platforms, tag_manager = self.signatures.detect(observations)
        successful = sum(not item.error for item in pages)
        crawl_conclusive = status == "COMPLETED" and successful > 0
        confidence, recommendation = classify_overall(platforms, crawl_conclusive=crawl_conclusive)
        domain = urlsplit(final_url or seed_url).hostname or urlsplit(seed_url).hostname or ""
        result = AnalysisResult(
            domain=domain,
            requested_url=requested_url,
            final_url=final_url,
            status=status,
            started_at=started,
            completed_at=datetime.now(UTC),
            pages_crawled=successful,
            pages=pages,
            platforms=platforms,
            google_tag_manager=tag_manager,
            advertising_infrastructure_confidence=confidence,
            recommendation=recommendation,
            landing_pages_requested=landing_pages_requested or [],
            landing_pages_crawled=sum(
                not item.error and item.page_role == "ad_landing" for item in pages
            ),
            technical_notes=_dedupe(notes)[:30],
            error=error,
        )
        log.info(
            "ad crawl complete domain=%s status=%s pages=%s confidence=%s rss_mb=%.1f",
            result.domain,
            result.status,
            result.pages_crawled,
            result.advertising_infrastructure_confidence,
            _rss_mb(),
        )
        return enrich_ad_infrastructure_result(result)

    def _failure(
        self,
        raw_url: str,
        started: datetime,
        status: str,
        error: str,
        *,
        domain: str = "",
    ) -> AnalysisResult:
        return enrich_ad_infrastructure_result(
            AnalysisResult(
                domain=domain,
                requested_url=raw_url,
                status=status,
                started_at=started,
                completed_at=datetime.now(UTC),
                advertising_infrastructure_confidence="INCONCLUSIVE",
                recommendation="ASK_SPEND",
                error=error,
                technical_notes=[error],
            )
        )


def _normalize_url(raw: str) -> str:
    candidate = coerce_input_url(raw)
    parts = urlsplit(candidate)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


async def _settle(page: Page, milliseconds: int) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=milliseconds)
    except Exception:
        pass  # the failed wait already consumed the requested settle interval


async def _scroll(page: Page) -> None:
    await page.evaluate(
        """
        async () => {
          const distance = Math.max(400, Math.floor(window.innerHeight * 0.8));
          const max = Math.min(document.body.scrollHeight, window.innerHeight * 5);
          for (let y = 0; y < max; y += distance) {
            window.scrollTo(0, y);
            await new Promise(resolve => setTimeout(resolve, 180));
          }
          window.scrollTo(0, 0);
        }
        """
    )


async def _accept_cookie_consent(page: Page) -> str | None:
    for root in [page, *page.frames]:
        clicked = await _accept_cookie_consent_in_root(root)
        if clicked:
            return clicked
    return None


async def _accept_cookie_consent_in_root(root) -> str | None:
    for selector in _CONSENT_SELECTORS:
        locator = root.locator(selector).first
        try:
            if await locator.is_visible(timeout=250):
                label = (await locator.inner_text()).strip() or selector
                await locator.click(timeout=1500)
                return label[:120]
        except Exception:
            continue

    buttons = root.get_by_role("button").filter(has_text=_CONSENT_TEXT)
    try:
        count = min(await buttons.count(), 8)
        for index in range(count):
            button = buttons.nth(index)
            text = (await button.inner_text()).strip()
            context_is_consent = await button.evaluate(
                """el => {
                  let node = el;
                  for (let i = 0; node && i < 5; i++, node = node.parentElement) {
                    const text = (node.innerText || '').toLowerCase();
                    if (text.length < 4000 && /cookie|consent|privacy/.test(text)) return true;
                  }
                  return false;
                }"""
            )
            if (
                context_is_consent
                and _CONSENT_TEXT.fullmatch(text)
                and await button.is_visible(timeout=200)
            ):
                await button.click(timeout=1500)
                return text[:120]
    except Exception:
        return None
    return None


async def _discover_priority_links(
    page: Page, base_url: str, scope: CrawlScope
) -> list[tuple[int, str]]:
    raw_links = await page.locator("a[href]").evaluate_all(
        "els => els.slice(0, 500).map(a => ({href: a.href, text: (a.innerText || '').trim()}))"
    )
    ranked: dict[str, int] = {}
    for item in raw_links:
        absolute = urljoin(base_url, item.get("href") or "")
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        if not scope.contains(parts.hostname):
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        haystack = f"{parts.path} {item.get('text', '')}".lower()
        score = max((value for key, value in _PRIORITY_PATHS.items() if key in haystack), default=0)
        path = parts.path.casefold()
        if not score and not any(marker in path for marker in _AVOID_SECONDARY_PATHS):
            # A short, visible internal navigation target is safer and usually
            # more useful than guessing that /pricing or /products exists.
            segments = [segment for segment in parts.path.split("/") if segment]
            if segments and len(segments) <= 2 and item.get("text", "").strip():
                score = 10
        if score:
            ranked[clean] = max(score, ranked.get(clean, 0))
    return sorted(((score, url) for url, score in ranked.items()), reverse=True)


def _choose_targets(homepage: str, discovered: list[tuple[int, str]], max_pages: int) -> list[str]:
    targets = [homepage]
    seen = {homepage.rstrip("/")}
    for _score, url in discovered:
        key = url.rstrip("/")
        if key not in seen:
            targets.append(url)
            seen.add(key)
        if len(targets) >= max_pages:
            return targets
    for path in _FALLBACK_PATHS:
        url = urljoin(homepage, path)
        key = url.rstrip("/")
        if key not in seen:
            targets.append(url)
            seen.add(key)
        if len(targets) >= max_pages:
            break
    return targets


def _looks_like_bot_challenge(title: str, html: str) -> bool:
    sample = f"{title}\n{html[:100_000]}".lower()
    return any(marker in sample for marker in _BOT_MARKERS)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _rss_mb() -> float:
    usage = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux (including Railway containers) reports KiB.
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
