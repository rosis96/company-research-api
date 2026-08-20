"""The renderer adapter.

Architecture reference: SS1 ("the one architectural decision that matters for
later scale") and SS5.

The orchestrator calls ``renderer.render(url)`` and knows nothing else.  In V1
the implementation is Playwright in-process; moving it to its own container,
swapping in a stealth renderer, or delegating to a paid rendering API are all
changes behind this interface.

Playwright cannot use the pinned httpx transport (Chromium does its own DNS), so
per SS12 the browser is constrained by route interception -- every request is
re-validated against the guard, and everything except the main document is
blocked outright -- backed by the network-level egress rules documented in the
README.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.config.settings import RendererSettings
from app.security.reasons import ReasonCode
from app.security.url_guard import CrawlScope, GuardError, UrlGuard

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RenderResult:
    ok: bool
    html: str = ""
    final_url: str = ""
    status_code: int = 0
    reason: ReasonCode | None = None
    detail: str = ""


class Renderer(Protocol):
    """Renders a single URL and returns its HTML."""

    @property
    def available(self) -> bool: ...  # pragma: no cover - protocol

    async def render(
        self, url: str, *, scope: CrawlScope | None, timeout_ms: int
    ) -> RenderResult: ...  # pragma: no cover - protocol

    async def aclose(self) -> None: ...  # pragma: no cover - protocol


class NullRenderer:
    """A renderer that always declines.

    Used when Playwright is not installed or the browser fallback is disabled.
    Pages that would have been rendered are reported as failures rather than
    silently returning empty content.
    """

    @property
    def available(self) -> bool:
        return False

    async def render(
        self, url: str, *, scope: CrawlScope | None, timeout_ms: int
    ) -> RenderResult:
        return RenderResult(
            ok=False, reason=ReasonCode.RENDER_FAILED, detail="renderer disabled"
        )

    async def aclose(self) -> None:
        return None


class PlaywrightRenderer:
    """Chromium-backed renderer with a fresh context per page.

    A shared browser process amortises startup; a new *context* per page gives
    clean cookie and storage isolation between companies at negligible cost.
    That isolation is the mechanical half of the no-cross-contamination
    requirement -- the other half lives in the GPT's instructions.
    """

    def __init__(
        self,
        guard: UrlGuard,
        settings: RendererSettings,
        *,
        user_agent: str,
        concurrency: int = 2,
    ) -> None:
        self._guard = guard
        self._settings = settings
        self._user_agent = user_agent
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._renders_since_restart = 0
        self._available: bool | None = None

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import playwright.async_api  # noqa: F401

            self._available = self._settings.enabled
        except ImportError:  # pragma: no cover - depends on environment
            self._available = False
        return self._available

    async def _ensure_browser(self):
        from playwright.async_api import async_playwright

        async with self._lock:
            if self._browser is not None and self._renders_since_restart >= (
                self._settings.recycle_after_renders
            ):
                await self._shutdown_locked()

            if self._browser is None:
                self._playwright = await async_playwright().start()
                args = list(self._settings.launch_args)
                if self._settings.host_resolver_rules:
                    args.append(
                        f"--host-resolver-rules={self._settings.host_resolver_rules}"
                    )
                launch_kwargs: dict = {"headless": True, "args": args}
                if self._settings.executable_path:
                    launch_kwargs["executable_path"] = self._settings.executable_path
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                self._renders_since_restart = 0
            return self._browser

    async def _shutdown_locked(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        self._browser = None
        self._playwright = None
        self._renders_since_restart = 0

    async def aclose(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    # ------------------------------------------------------------------

    async def render(
        self, url: str, *, scope: CrawlScope | None, timeout_ms: int
    ) -> RenderResult:
        if not self.available:
            return RenderResult(
                ok=False, reason=ReasonCode.RENDER_FAILED, detail="playwright unavailable"
            )

        try:
            await self._guard.validate(url, scope=scope)
        except GuardError as exc:
            return RenderResult(ok=False, reason=exc.reason, detail=exc.detail)

        async with self._semaphore:
            try:
                browser = await self._ensure_browser()
            except Exception as exc:  # pragma: no cover - launch failure
                log.warning("renderer.launch_failed", error=str(exc))
                return RenderResult(
                    ok=False, reason=ReasonCode.RENDER_FAILED, detail=str(exc)
                )

            context = None
            try:
                context = await browser.new_context(
                    user_agent=self._user_agent,
                    accept_downloads=False,
                    java_script_enabled=True,
                    bypass_csp=False,
                    viewport={"width": 1280, "height": 900},
                )
                context.set_default_timeout(timeout_ms)
                page = await context.new_page()
                await page.route("**/*", self._make_router(scope))

                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(3000, timeout_ms)
                    )
                except Exception:
                    pass  # network-idle is best effort; the DOM is what matters

                html = await page.content()
                self._renders_since_restart += 1
                return RenderResult(
                    ok=True,
                    html=html or "",
                    final_url=page.url or url,
                    status_code=response.status if response is not None else 200,
                )
            except Exception as exc:
                detail = str(exc)
                reason = (
                    ReasonCode.TIMEOUT
                    if "Timeout" in detail or "timeout" in detail
                    else ReasonCode.RENDER_FAILED
                )
                log.debug("renderer.failed", url=url, error=detail[:200])
                return RenderResult(ok=False, reason=reason, detail=detail[:200])
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:  # pragma: no cover
                        pass

    def _make_router(self, scope: CrawlScope | None):
        """Block every request that is not the main document for an allowed URL."""
        blocked_types = set(self._settings.block_resource_types)

        async def route_handler(route, request) -> None:
            try:
                if request.resource_type in blocked_types:
                    await route.abort()
                    return
                if request.resource_type != "document":
                    await route.abort()
                    return

                try:
                    _scheme, host, _port, _path = self._guard.check_syntax(request.url)
                except GuardError:
                    await route.abort()
                    return

                if scope is not None and not scope.contains(host):
                    await route.abort()
                    return

                await route.continue_()
            except Exception:  # pragma: no cover - route already handled
                try:
                    await route.abort()
                except Exception:
                    pass

        return route_handler


def build_renderer(
    guard: UrlGuard,
    settings: RendererSettings,
    *,
    user_agent: str,
    concurrency: int,
) -> Renderer:
    """Factory used by the API layer; keeps Playwright out of the orchestrator."""
    if not settings.enabled:
        return NullRenderer()
    renderer = PlaywrightRenderer(
        guard, settings, user_agent=user_agent, concurrency=concurrency
    )
    return renderer if renderer.available else NullRenderer()
