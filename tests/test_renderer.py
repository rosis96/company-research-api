"""Renderer adapter tests (SS1, SS5, SS12).

The adapter contract is tested unconditionally.  The tests that drive a real
Chromium are marked ``renderer`` and skip when Playwright is unavailable, so the
suite still runs in environments without a browser.
"""

from __future__ import annotations

import asyncio
import functools
import os

import pytest

from app.config.settings import RendererSettings
from app.fetch.renderer import (
    NullRenderer,
    PlaywrightRenderer,
    RenderResult,
    build_renderer,
)
from app.security.reasons import ReasonCode
from app.security.url_guard import CrawlScope


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401

        return True
    except ImportError:  # pragma: no cover - environment dependent
        return False


def browser_settings() -> RendererSettings:
    """Renderer settings, honouring CRA_RENDERER__EXECUTABLE_PATH when set.

    Lets the suite drive a pre-installed Chromium whose build number does not
    match the installed Playwright package.
    """
    return RendererSettings(
        executable_path=os.environ.get("CRA_RENDERER__EXECUTABLE_PATH"),
        # Chromium resolves hostnames itself, so fixture hosts need a mapping.
        host_resolver_rules="MAP * 127.0.0.1",
    )


@functools.lru_cache(maxsize=1)
def browser_launchable() -> bool:
    """Can a browser actually start here?

    Checking only that the package imports is not enough: the image may ship a
    Chromium whose build number does not match the installed Playwright package,
    in which case launching fails at runtime.  Set
    ``CRA_RENDERER__EXECUTABLE_PATH`` to point at the real binary.
    """
    if not playwright_available():
        return False

    async def _probe() -> bool:
        from playwright.async_api import async_playwright

        settings = browser_settings()
        try:
            async with async_playwright() as pw:
                kwargs: dict = {"headless": True, "args": ["--no-sandbox"]}
                if settings.executable_path:
                    kwargs["executable_path"] = settings.executable_path
                browser = await pw.chromium.launch(**kwargs)
                await browser.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:  # pragma: no cover - environment dependent
        return False


requires_browser = pytest.mark.skipif(
    not browser_launchable(),
    reason=(
        "no launchable Chromium (run `playwright install chromium`, or set "
        "CRA_RENDERER__EXECUTABLE_PATH)"
    ),
)


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


async def test_null_renderer_declines_honestly() -> None:
    renderer = NullRenderer()
    assert renderer.available is False
    result = await renderer.render("https://acme.com/", scope=None, timeout_ms=1000)
    assert result.ok is False
    assert result.reason is ReasonCode.RENDER_FAILED
    await renderer.aclose()


def test_disabled_settings_produce_a_null_renderer(guard) -> None:
    renderer = build_renderer(
        guard, RendererSettings(enabled=False), user_agent="Test/1.0", concurrency=1
    )
    assert isinstance(renderer, NullRenderer)


def test_factory_returns_a_playwright_renderer_when_enabled(guard) -> None:
    renderer = build_renderer(
        guard, RendererSettings(enabled=True), user_agent="Test/1.0", concurrency=2
    )
    expected = PlaywrightRenderer if playwright_available() else NullRenderer
    assert isinstance(renderer, expected)


async def test_renderer_validates_urls_through_the_guard(guard) -> None:
    """A renderer must not be a way around the security boundary."""
    renderer = PlaywrightRenderer(
        guard, RendererSettings(), user_agent="Test/1.0", concurrency=1
    )
    for url in ("http://127.0.0.1/", "file:///etc/passwd", "http://localhost/"):
        result = await renderer.render(url, scope=None, timeout_ms=1000)
        assert result.ok is False
        assert result.reason in (
            ReasonCode.INVALID_URL,
            ReasonCode.BLOCKED_SCHEME,
            ReasonCode.PRIVATE_ADDRESS,
        )
    await renderer.aclose()


async def test_renderer_rejects_out_of_scope_urls(guard) -> None:
    renderer = PlaywrightRenderer(
        guard, RendererSettings(), user_agent="Test/1.0", concurrency=1
    )
    scope = CrawlScope(
        registrable_domain="staticsite.com",
        seed_host="staticsite.com",
        seed_url="http://staticsite.com/",
    )
    result = await renderer.render("http://newbrand.com/", scope=scope, timeout_ms=1000)
    assert result.ok is False
    assert result.reason is ReasonCode.OUT_OF_SCOPE
    await renderer.aclose()


def test_render_result_shape() -> None:
    result = RenderResult(ok=True, html="<html></html>", final_url="u", status_code=200)
    assert result.ok and result.html and result.status_code == 200


def test_non_document_resource_types_are_blocked_by_default() -> None:
    """Images, fonts and stylesheets change nothing about extracted text."""
    settings = RendererSettings()
    for resource in ("image", "font", "media", "stylesheet"):
        assert resource in settings.block_resource_types


# ---------------------------------------------------------------------------
# Real browser
# ---------------------------------------------------------------------------


@requires_browser
@pytest.mark.renderer
async def test_renders_a_javascript_only_page(guard, fixture_servers) -> None:
    """The SPA fixture returns an empty shell over HTTP; only rendering finds text."""
    server = fixture_servers["spa"]
    renderer = PlaywrightRenderer(
        guard, browser_settings(), user_agent="Test/1.0", concurrency=1
    )
    try:
        result = await renderer.render(server.url("/"), scope=None, timeout_ms=15000)
        assert result.ok, result.detail
        assert "rendered" in result.html
        assert result.status_code == 200
    finally:
        await renderer.aclose()


@requires_browser
@pytest.mark.renderer
async def test_orchestrator_uses_the_renderer_for_a_spa(
    guard, fetcher, fixture_servers
) -> None:
    from app.config.settings import RobotsSettings
    from app.crawl.orchestrator import ResearchOrchestrator
    from app.crawl.robots import RobotsPolicy
    from app.models.request import AdvancedOptions, ResearchRequest

    renderer = PlaywrightRenderer(
        guard, browser_settings(), user_agent="Test/1.0", concurrency=1
    )
    try:
        orchestrator = ResearchOrchestrator(
            guard, fetcher, renderer, RobotsPolicy(RobotsSettings(), fetcher)
        )
        request = ResearchRequest(
            url=fixture_servers["spa"].url("/"), advanced=AdvancedOptions(debug=True)
        )
        result = await orchestrator.run(request, request_id="req_test")
        assert result.crawl_summary.browser_renders >= 1
        assert result.debug["js_mode"] is True
    finally:
        await renderer.aclose()


@requires_browser
@pytest.mark.renderer
async def test_browser_never_reaches_an_out_of_scope_host(
    guard, fixture_servers
) -> None:
    """Route interception is what keeps invariant 4 true inside Chromium."""
    spa = fixture_servers["spa"]
    other = fixture_servers["newbrand"]
    spa.spec.routes["/leaky"] = type(spa.spec.routes["/"])(
        body=(
            "<html><body><div id='root'></div>"
            f"<script>fetch('http://newbrand.com:{other.port}/').catch(()=>{{}});</script>"
            "</body></html>"
        )
    )
    other.clear_requests()

    renderer = PlaywrightRenderer(
        guard, browser_settings(), user_agent="Test/1.0", concurrency=1
    )
    try:
        scope = CrawlScope(
            registrable_domain="spasite.com",
            seed_host="spasite.com",
            seed_url=spa.url("/"),
        )
        await renderer.render(spa.url("/leaky"), scope=scope, timeout_ms=10000)
        assert other.requests == []
    finally:
        await renderer.aclose()


@requires_browser
@pytest.mark.renderer
async def test_renderer_times_out_cleanly(guard, fixture_servers) -> None:
    server = fixture_servers["slow"]
    renderer = PlaywrightRenderer(
        guard, browser_settings(), user_agent="Test/1.0", concurrency=1
    )
    try:
        result = await renderer.render(server.url("/services"), scope=None, timeout_ms=1200)
        assert result.ok is False
        assert result.reason in (ReasonCode.TIMEOUT, ReasonCode.RENDER_FAILED)
    finally:
        await renderer.aclose()
