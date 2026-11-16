from __future__ import annotations

import asyncio
import logging
import re
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
from app.security.reasons import ReasonCode
from app.security.url_guard import CrawlScope, GuardError, UrlGuard, coerce_input_url

try:
    import resource
except ImportError:  # Windows has no resource module; RSS logging is diagnostic only.
    resource = None

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
_HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})
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
    # CloudFront and Akamai block pages are often served with HTTP 200.
    "the request could not be satisfied",
    "errors.edgesuite.net",
)
_BLOCKED_PAGE_MARKERS = ("HTTP 403", "HTTP 429", "bot-protection")
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
        # A crashed Chromium or Playwright driver used to stay cached, so every
        # remaining row of a batch failed instantly. Relaunch once and retry.
        for attempt in range(2):
            browser = await self._ensure_browser()
            self._domains_since_restart += 1
            try:
                return await browser.new_context(
                    user_agent=self.settings.user_agent,
                    accept_downloads=False,
                    java_script_enabled=True,
                    ignore_https_errors=self.settings.ignore_https_errors,
                    viewport={"width": 1365, "height": 900},
                    service_workers="allow",
                )
            except Exception as exc:
                if attempt or not is_dead_browser_error(exc):
                    raise
                log.warning("Chromium connection lost; relaunching: %s", str(exc)[:180])
                await self.invalidate(browser)
        raise AssertionError("unreachable")

    async def invalidate(self, browser: Browser | None = None) -> None:
        """Drop the browser so the next context relaunches it.

        Passing the failed browser makes concurrent workers that hit the same
        crash relaunch once, instead of closing each other's fresh browser.
        """
        async with self._lock:
            if browser is not None and browser is not self._browser:
                return
            await self._shutdown_locked()

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and not self._browser.is_connected():
                log.warning("Chromium disconnected; relaunching (rss_mb=%.1f)", _rss_mb())
                await self._shutdown_locked()
            should_recycle = (
                self._browser is not None
                and self.settings.recycle_after_domains > 0
                and self._domains_since_restart >= self.settings.recycle_after_domains
                and not self._browser.contexts
            )
            if should_recycle:
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
            candidates = _candidate_seed_urls(raw_url)
        except GuardError as exc:
            if exc.reason == ReasonCode.INVALID_URL and exc.detail == "empty url":
                return self._failure(
                    raw_url,
                    started,
                    "NO_WEBSITE",
                    "No website was supplied.",
                )
            return self._failure(
                raw_url, started, "CRAWL_FAILED", f"invalid or unsafe URL: {exc.detail}"
            )
        except Exception as exc:
            return self._failure(raw_url, started, "CRAWL_FAILED", f"invalid URL: {exc}")

        domain_hint = urlsplit(candidates[0]).hostname or ""
        last_attempted_url = candidates[0]
        try:
            async with asyncio.timeout(self.settings.total_timeout_seconds):
                validated = []
                dns_failures: list[str] = []
                for candidate in candidates:
                    try:
                        target = await self.guard.validate(candidate)
                    except GuardError as exc:
                        if exc.reason == ReasonCode.DNS_FAILURE:
                            dns_failures.append(exc.detail)
                            continue
                        return self._failure(
                            raw_url,
                            started,
                            "CRAWL_FAILED",
                            f"invalid or unsafe URL: {exc.detail}",
                        )
                    if target.url not in {item.url for item in validated}:
                        validated.append(target)

                if not validated:
                    return self._failure(
                        raw_url,
                        started,
                        "NO_WEBSITE",
                        "No active website was found: the domain did not resolve in DNS.",
                        domain=urlsplit(candidates[0]).hostname or "",
                    )

                attempts: list[AnalysisResult] = []
                for target in validated:
                    domain_hint = target.host
                    last_attempted_url = target.url
                    result = await self._analyze_validated(
                        target.url, raw_url, started, landing_page_urls or []
                    )
                    if result.status == "COMPLETED":
                        return result
                    attempts.append(result)

                if attempts and all(_is_definitively_inactive(item) for item in attempts):
                    result = attempts[0]
                    result.status = "NO_WEBSITE"
                    result.advertising_infrastructure_confidence = "NONE"
                    result.error = (
                        "No active website was found after checking HTTPS/HTTP and www/apex alternatives."
                    )
                    result.technical_notes = _dedupe(
                        [*result.technical_notes, result.error, *dns_failures]
                    )[:30]
                    return enrich_ad_infrastructure_result(result)
                return _best_failed_attempt(attempts)
        except TimeoutError:
            return self._failure(
                raw_url,
                started,
                "INCONCLUSIVE",
                f"domain crawl exceeded {self.settings.total_timeout_seconds}s timeout",
                domain=domain_hint,
            )
        except Exception as exc:
            log.exception("ad crawl failed", extra={"url": last_attempted_url})
            return self._failure(
                raw_url,
                started,
                "CRAWL_FAILED",
                f"browser crawl failed: {type(exc).__name__}: {str(exc)[:300]}",
                domain=domain_hint,
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
            result = self._build_result(
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
            if (
                result.advertising_infrastructure_confidence == "NONE"
                and _blocked_after_homepage(pages)
            ):
                # The site let the first request through and then refused the
                # crawler. Finding nothing on one page is not a negative result.
                return self._build_result(
                    requested_url=requested_url,
                    seed_url=seed_url,
                    final_url=final_url,
                    started=started,
                    pages=pages,
                    observations=observations,
                    notes=notes,
                    status="INCONCLUSIVE",
                    error=(
                        "bot-protection blocked every page after the homepage "
                        "and no advertising evidence was found on the homepage"
                    ),
                    landing_pages_requested=landing_page_urls,
                )
            return result
        except Exception as exc:
            if is_dead_browser_error(exc):
                await self.browser.invalidate(context.browser)
            raise
        finally:
            try:
                await context.close()
            except Exception as exc:
                # A browser/context shutdown must not overwrite a result that was
                # already collected successfully for this domain.
                log.warning("browser context close failed: %s", str(exc)[:180])

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
            # Images, fonts and media only slow the crawl down. The request event
            # has already been recorded, and anything matching an advertising
            # rule (image pixels such as facebook.com/tr) is still allowed.
            if (
                self.settings.block_heavy_resources
                and request.resource_type in _HEAVY_RESOURCE_TYPES
                and not self.signatures.matches_network(url)
            ):
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
        no_website = status == "NO_WEBSITE"
        return enrich_ad_infrastructure_result(
            AnalysisResult(
                domain=domain,
                requested_url=raw_url,
                status=status,
                started_at=started,
                completed_at=datetime.now(UTC),
                advertising_infrastructure_confidence=("NONE" if no_website else "INCONCLUSIVE"),
                recommendation=("LOW_PRIORITY" if no_website else "ASK_SPEND"),
                error=error,
                technical_notes=[error],
            )
        )


_DEAD_BROWSER_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser has disconnected",
    "connection closed",
    "handler is closed",
    "transport closed",
    "target closed",
)


def is_dead_browser_error(exc: BaseException) -> bool:
    """True when the error means Chromium or the Playwright driver is gone."""
    message = str(exc).lower()
    return any(marker in message for marker in _DEAD_BROWSER_MARKERS)


def _normalize_url(raw: str) -> str:
    candidate = coerce_input_url(raw)
    parts = urlsplit(candidate)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _candidate_seed_urls(raw: str) -> list[str]:
    """Return safe homepage alternatives without leaving the company domain."""
    normalized = _normalize_url(raw)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    supplied = urlunsplit((parts.scheme, f"{host}{port}", parts.path or "/", parts.query, ""))
    candidates = [supplied]
    root_host = registrable_domain(host)
    hosts = [host]
    if host == root_host:
        hosts.append(f"www.{root_host}")
    elif host == f"www.{root_host}":
        hosts.append(root_host)
    for candidate_host in hosts:
        for scheme in (parts.scheme, "http" if parts.scheme == "https" else "https"):
            candidate = urlunsplit((scheme, candidate_host, "/", "", ""))
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _is_definitively_inactive(result: AnalysisResult) -> bool:
    reason = " ".join(
        [
            result.error or "",
            *(page.error or "" for page in result.pages),
        ]
    )
    return any(
        marker in reason
        for marker in (
            "HTTP 404",
            "HTTP 410",
            "ERR_NAME_NOT_RESOLVED",
            "ERR_CONNECTION_REFUSED",
            "ERR_ADDRESS_UNREACHABLE",
        )
    )


def _best_failed_attempt(attempts: list[AnalysisResult]) -> AnalysisResult:
    if not attempts:
        raise RuntimeError("no crawl attempt was made")
    # Prefer the original URL's diagnostic unless another attempt reached a
    # clear bot/rate-limit response, which is more useful than a generic error.
    for result in attempts:
        reason = " ".join([result.error or "", *(page.error or "" for page in result.pages)])
        if any(marker in reason for marker in ("HTTP 403", "HTTP 429", "bot-protection")):
            return result
    return attempts[0]


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


def _blocked_after_homepage(pages: list[PageDiagnostic]) -> bool:
    """Only the homepage loaded and at least two later pages were refused."""
    later = pages[1:]
    return (
        bool(pages)
        and not pages[0].error
        and len(later) >= 2
        and all(
            page.error and any(marker in page.error for marker in _BLOCKED_PAGE_MARKERS)
            for page in later
        )
    )


def _looks_like_bot_challenge(title: str, html: str) -> bool:
    sample = f"{title}\n{html[:100_000]}".lower()
    return any(marker in sample for marker in _BOT_MARKERS)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _rss_mb() -> float:
    if resource is None:
        return 0.0
    usage = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux (including Railway containers) reports KiB.
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
