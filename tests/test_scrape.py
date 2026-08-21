"""Single-URL scrape, output formats and the fast path.

`/v1/research` answers "what does this company do?" by crawling a domain.
`/v1/scrape` answers "what is on this page?" and skips discovery and ranking
entirely. What it must *not* skip is the security boundary, so a good half of
this file is proving the guard, robots and pinning still apply.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.fetch.renderer import NullRenderer, RenderResult
from app.main import create_app
from app.models.request import ScrapeRequest
from app.scrape_service import ScrapeService
from app.security.reasons import ReasonCode

KEY = "scrape-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class StubRenderer(NullRenderer):
    """A browser that always succeeds, so render decisions are observable."""

    def __init__(self, html: str = "") -> None:
        self.calls: list[str] = []
        self.html = html or (
            "<html lang='en'><head><title>Rendered Ltd</title></head><body><main>"
            "<h1>Rendered Ltd</h1><p>" + ("word " * 300) + "</p></main></body></html>"
        )

    @property
    def available(self) -> bool:
        return True

    async def render(self, url, *, scope, timeout_ms):
        self.calls.append(url)
        return RenderResult(ok=True, html=self.html, final_url=url, status_code=200)


@pytest.fixture
def service(guard, renderer_settings):
    def build(renderer=None, **overrides):
        settings = Settings(
            api_keys=f"{KEY}:standard",
            environment="test",
            log_json=False,
            log_level="ERROR",
            cache={"enabled": False},
            renderer={"enabled": False},
            **overrides,
        )
        return ScrapeService(
            guard=guard, renderer=renderer or NullRenderer(), settings=settings
        )

    return build


@pytest.fixture
def scrape_client(guard, fixture_servers):
    settings = Settings(
        api_keys=f"{KEY}:standard",
        environment="test",
        log_json=False,
        log_level="ERROR",
        cache={"enabled": False},
        renderer={"enabled": False},
        rate_limit_per_minute=1000,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.guard = guard
        app.state.scrape_service.guard = guard
        yield client


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_one_url_returns_that_page(service, fixture_servers) -> None:
    result = await service().run(ScrapeRequest(url=fixture_servers["static"].url("/about")))

    assert result.status in ("success", "partial")
    assert result.word_count > 0
    assert result.content
    assert result.url.endswith("/about")
    assert result.fetch_method == "http"


async def test_the_domain_is_not_crawled(service, fixture_servers) -> None:
    """The whole point: one request, one page.

    /v1/research on this fixture fetches robots, the homepage and several
    internal pages. A scrape must touch robots and the one page, nothing else.
    """
    server = fixture_servers["static"]
    server.clear_requests()
    await service().run(ScrapeRequest(url=server.url("/services")))

    fetched = [path for path in server.requests if path != "/robots.txt"]
    assert fetched == ["/services"], server.requests


async def test_a_scrape_reports_its_own_request_id(service, fixture_servers) -> None:
    result = await service().run(ScrapeRequest(url=fixture_servers["static"].url("/")))
    assert result.request_id.startswith("scr_")


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


async def test_text_is_the_default(service, fixture_servers) -> None:
    result = await service().run(ScrapeRequest(url=fixture_servers["static"].url("/about")))
    assert result.output_format == "text"


async def test_markdown_keeps_links_that_text_drops(service, fixture_servers) -> None:
    """Writer models use markdown better, and a link records where a claim went.

    The case-study index is the page to test it on: its links are in the body,
    not the nav, so they survive chrome removal.
    """
    url = fixture_servers["depth2"].url("/case-studies")
    text = await service().run(ScrapeRequest(url=url, output_format="text"))
    markdown = await service().run(ScrapeRequest(url=url, output_format="markdown"))

    assert markdown.output_format == "markdown"
    assert markdown.word_count > 0
    # Markdown link syntax appears in one and not the other.
    assert "](" in markdown.content
    assert "](" not in text.content


async def test_html_returns_markup(service, fixture_servers) -> None:
    result = await service().run(
        ScrapeRequest(url=fixture_servers["static"].url("/about"), output_format="html")
    )
    assert result.output_format == "html"
    assert "<" in result.content and ">" in result.content


async def test_html_is_capped(service, fixture_servers) -> None:
    built = service(scrape={"max_html_chars": 200})
    result = await built.run(
        ScrapeRequest(url=fixture_servers["static"].url("/about"), output_format="html")
    )
    assert len(result.content) <= 200
    assert result.truncated is True


async def test_text_is_capped_at_a_paragraph_boundary(service, fixture_servers) -> None:
    built = service(scrape={"max_content_chars": 300})
    result = await built.run(ScrapeRequest(url=fixture_servers["static"].url("/about")))
    assert len(result.content) <= 300
    assert result.truncated is True


# ---------------------------------------------------------------------------
# The fast path: render is the caller's choice
# ---------------------------------------------------------------------------


async def test_render_never_does_not_touch_the_browser(service, fixture_servers) -> None:
    """The real-time path. A render costs 1.5-3s, most of an inbound budget.

    A JS-only page comes back thin and says so, rather than spending the budget
    discovering that.
    """
    renderer = StubRenderer()
    built = service(renderer=renderer)
    result = await built.run(
        ScrapeRequest(url=fixture_servers["spa"].url("/"), render="never")
    )

    assert renderer.calls == []
    assert result.fetch_method == "http"
    assert result.status == "partial"
    assert result.status_reason == str(ReasonCode.TOO_THIN)


async def test_render_auto_rescues_a_js_only_page(service, fixture_servers) -> None:
    renderer = StubRenderer()
    built = service(renderer=renderer)
    result = await built.run(
        ScrapeRequest(url=fixture_servers["spa"].url("/"), render="auto")
    )

    assert renderer.calls, "a thin page should have been rendered"
    assert result.fetch_method == "browser"
    assert result.status == "success"
    assert "rendered_because_http_content_was_thin" in result.notes


async def test_render_always_skips_the_http_attempt(service, fixture_servers) -> None:
    renderer = StubRenderer()
    built = service(renderer=renderer)
    result = await built.run(
        ScrapeRequest(url=fixture_servers["static"].url("/"), render="always")
    )

    assert renderer.calls
    assert result.fetch_method == "browser"


async def test_a_blocked_page_is_retried_through_the_browser(
    service, fixture_servers
) -> None:
    renderer = StubRenderer()
    built = service(renderer=renderer)
    result = await built.run(ScrapeRequest(url=fixture_servers["forbidden"].url("/")))

    assert renderer.calls
    assert result.status == "success"
    assert "blocked_over_http_read_with_browser" in result.notes


async def test_a_blocked_page_fails_honestly_without_a_browser(
    service, fixture_servers
) -> None:
    result = await service().run(ScrapeRequest(url=fixture_servers["forbidden"].url("/")))
    assert result.status == "failed"
    assert result.status_reason == str(ReasonCode.BOT_BLOCKED)
    assert result.content == ""


async def test_render_never_does_not_escalate_a_block(service, fixture_servers) -> None:
    """The fast path stays fast even when the page refuses us."""
    renderer = StubRenderer()
    built = service(renderer=renderer)
    result = await built.run(
        ScrapeRequest(url=fixture_servers["forbidden"].url("/"), render="never")
    )

    assert renderer.calls == []
    assert result.status == "failed"


async def test_the_deadline_is_capped_by_the_server(service, fixture_servers) -> None:
    """A caller cannot buy an unbounded crawl by asking for one."""
    built = service(scrape={"max_deadline_ms": 2_000})
    result = await built.run(
        ScrapeRequest(url=fixture_servers["static"].url("/"), deadline_ms=60_000)
    )
    assert result.elapsed_ms < 10_000
    assert result.status in ("success", "partial")


# ---------------------------------------------------------------------------
# The security boundary is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:pass@example.com/",
        "file:///etc/passwd",
        "http://[::1]/",
        "http://0177.0.0.1/",
    ],
)
async def test_the_guard_still_refuses_what_it_always_refused(service, url) -> None:
    result = await service().run(ScrapeRequest(url=url))
    assert result.status == "failed"
    assert result.content == ""


async def test_robots_disallow_is_obeyed(service, fixture_servers) -> None:
    result = await service().run(ScrapeRequest(url=fixture_servers["blocked"].url("/")))
    assert result.status == "failed"
    assert result.status_reason == str(ReasonCode.ROBOTS_DISALLOWED)


async def test_a_failure_never_carries_content(service) -> None:
    """Invariant 1, restated for the new endpoint."""
    result = await service().run(ScrapeRequest(url="https://no-such-host.public.test/"))
    assert result.status == "failed"
    assert result.content == ""
    assert result.word_count == 0
    assert result.title is None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_scrape_requires_a_key(scrape_client) -> None:
    assert scrape_client.post("/v1/scrape", json={"url": "https://a.com"}).status_code == 401


def test_scrape_returns_200_even_when_the_page_fails(scrape_client) -> None:
    """SS9's status-code policy applies here too: branch on `status`, not on HTTP."""
    response = scrape_client.post(
        "/v1/scrape", json={"url": "http://127.0.0.1/"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_scrape_rejects_unknown_fields(scrape_client) -> None:
    response = scrape_client.post(
        "/v1/scrape", json={"url": "https://a.com", "depth": 3}, headers=AUTH
    )
    assert response.status_code == 422


def test_scrape_can_be_disabled(scrape_client) -> None:
    scrape_client.app.state.settings.scrape.enabled = False
    try:
        response = scrape_client.post(
            "/v1/scrape", json={"url": "https://a.com"}, headers=AUTH
        )
        assert response.status_code == 503
    finally:
        scrape_client.app.state.settings.scrape.enabled = True


def test_scrape_serves_a_real_page(scrape_client, fixture_servers) -> None:
    response = scrape_client.post(
        "/v1/scrape",
        json={"url": fixture_servers["static"].url("/about"), "output_format": "markdown"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("success", "partial")
    assert body["output_format"] == "markdown"
    assert body["word_count"] > 0
