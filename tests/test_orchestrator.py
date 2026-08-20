"""End-to-end orchestrator tests against the eight fixture scenarios (SS17).

These exercise the whole pipeline over real sockets: guard, fetch, discovery,
robots, ranking, extraction, budgeting and assembly.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config.loader import get_limits
from app.config.settings import RobotsSettings
from app.crawl.orchestrator import ResearchOrchestrator
from app.crawl.robots import RobotsPolicy
from app.fetch.renderer import NullRenderer, RenderResult
from app.models.request import AdvancedOptions, ResearchRequest
from app.security.reasons import ReasonCode

DEBUG = AdvancedOptions(debug=True)


async def run(orchestrator, url: str, **kwargs) -> object:
    request = ResearchRequest(url=url, advanced=DEBUG, **kwargs)
    return await orchestrator.run(request, request_id="req_test")


def urls_of(result) -> list[str]:
    return [page.url for page in result.pages]


def types_of(result) -> list[str]:
    return [page.page_type for page in result.pages]


# ---------------------------------------------------------------------------
# 1. Static site
# ---------------------------------------------------------------------------


async def test_static_site_succeeds_with_diverse_evidence(orchestrator, fixture_servers) -> None:
    server = fixture_servers["static"]
    result = await run(orchestrator, server.url("/"))

    assert result.status == "success"
    assert result.evidence_sufficient
    assert result.crawl_summary.browser_renders == 0  # HTTP only
    assert "homepage" in types_of(result)

    categories = set(types_of(result))
    assert len(categories) >= 4, categories
    assert result.site.canonical_domain == "staticsite.com"
    assert result.site.language == "en"


async def test_static_site_extracts_jsonld_organization(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    assert result.site.site_name == "Acme Precision Ltd"
    assert result.site.tagline == "Precision components, delivered on time"
    organization = result.site.organization
    assert organization is not None
    assert organization.founding_date == "1998"
    assert organization.employees == "120"
    assert organization.locations == ["Austin, TX, US"]
    # Third-party review data and social links must not survive.
    dumped = result.model_dump_json()
    assert "aggregateRating" not in dumped
    assert "linkedin.com" not in dumped


async def test_static_site_removes_cross_page_boilerplate(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    # The <footer> is already removed per page by extraction; what cross-page
    # removal has to catch is the CTA repeated inside <main>.
    assert result.site.site_wide_content is not None
    assert "Talk to an engineer" in result.site.site_wide_content
    assert result.site.site_wide_content.count("Talk to an engineer") == 1
    for page in result.pages:
        assert "Talk to an engineer" not in page.content
        assert "Copyright Acme Precision Ltd" not in page.content


async def test_excluded_pages_are_never_fetched(orchestrator, fixture_servers) -> None:
    """Login, privacy and terms are linked from every page and must be skipped."""
    server = fixture_servers["static"]
    server.clear_requests()
    await run(orchestrator, server.url("/"))
    for path in ("/login", "/privacy", "/terms"):
        assert path not in server.requests


async def test_every_page_is_attributable_to_a_url(orchestrator, fixture_servers) -> None:
    """Invariant 2: content only exists inside a page object carrying its URL."""
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    for page in result.pages:
        assert page.url.startswith("http")
        assert page.content.strip()


# ---------------------------------------------------------------------------
# 2. Careers: overview included, listings never expanded
# ---------------------------------------------------------------------------


async def test_careers_overview_included_without_job_expansion(
    orchestrator, fixture_servers
) -> None:
    server = fixture_servers["static"]
    server.clear_requests()
    result = await run(orchestrator, server.url("/"))

    assert "careers_overview" in types_of(result)
    # The careers page links to four job listings; none may be fetched.
    for path in server.requests:
        assert not path.startswith("/careers/"), path
    assert not any("/careers/" in url for url in urls_of(result))


async def test_careers_page_is_never_treated_as_a_hub(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    careers = [p for p in result.debug["pages"] if p["page_type"] == "careers_overview"]
    assert careers and all(not p["is_hub"] for p in careers)


# ---------------------------------------------------------------------------
# 3. Depth-2 case studies -- the reason the two-wave crawl exists
# ---------------------------------------------------------------------------


async def test_depth_two_reaches_individual_case_studies(
    orchestrator, fixture_servers
) -> None:
    result = await run(orchestrator, fixture_servers["depth2"].url("/"))

    case_studies = [p for p in result.pages if p.page_type == "case_study"]
    assert len(case_studies) >= 2, types_of(result)
    assert all("/case-studies/" in p.url for p in case_studies)
    assert result.crawl_summary.max_depth_reached == 2


async def test_hub_children_are_discovered_via_the_index(
    orchestrator, fixture_servers
) -> None:
    server = fixture_servers["depth2"]
    result = await run(orchestrator, server.url("/"))
    details = [
        p for p in result.debug["pages"] if p["page_type"] == "case_study" and p["depth"] == 2
    ]
    assert details
    assert all(p["discovered_via"].endswith("/case-studies") for p in details)


async def test_thin_index_page_still_enables_hub_expansion(
    orchestrator, fixture_servers
) -> None:
    """A grid of thumbnails carries no prose but is the only route to the detail pages."""
    result = await run(orchestrator, fixture_servers["depth2"].url("/"))
    returned = urls_of(result)
    assert not any(url.endswith("/case-studies") for url in returned)
    assert any("/case-studies/" in url for url in returned)


async def test_depth_is_capped_at_two(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["depth2"].url("/"))
    assert max(p["depth"] for p in result.debug["pages"]) <= 2


async def test_max_depth_one_disables_wave_two(guard, fetcher, fixture_servers) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(max_depth=1)
    request = ResearchRequest(url=fixture_servers["depth2"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test", budget=budget)
    assert result.crawl_summary.max_depth_reached <= 1


# ---------------------------------------------------------------------------
# 4. Cross-domain redirect: rebase on the seed, reject on internal pages
# ---------------------------------------------------------------------------


async def test_homepage_redirect_rebases_the_crawl_scope(
    orchestrator, fixture_servers
) -> None:
    result = await run(orchestrator, fixture_servers["oldbrand"].url("/"))

    assert result.status in ("success", "partial")
    assert result.site.domain_rebased is True
    assert result.site.canonical_domain == "newbrand.com"
    assert result.site.final_url.startswith("http://newbrand.com")
    assert all("newbrand.com" in url for url in urls_of(result))


async def test_internal_cross_domain_redirect_is_rejected(
    orchestrator, fixture_servers
) -> None:
    """Invariant 4: /partners redirects off-domain and must be dropped, not followed."""
    server = fixture_servers["leaky"]
    newbrand = fixture_servers["newbrand"]
    newbrand.clear_requests()

    result = await run(orchestrator, server.url("/"))

    assert all("leakysite.com" in url for url in urls_of(result)), urls_of(result)
    assert newbrand.requests == []
    reasons = {failed.reason_code for failed in result.failed_pages}
    assert str(ReasonCode.OUT_OF_SCOPE) in reasons


async def test_scope_is_frozen_after_the_homepage(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["leaky"].url("/"))
    assert result.debug["scope"] == "leakysite.com"


# ---------------------------------------------------------------------------
# 5. Slow site: the deadline always wins
# ---------------------------------------------------------------------------


async def test_slow_site_returns_partial_results_within_the_deadline(
    guard, fetcher, fixture_servers
) -> None:
    """Invariant 3: in-flight work is cancelled and a valid response is assembled."""
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(deadline_ms=6000)
    request = ResearchRequest(url=fixture_servers["slow"].url("/"), advanced=DEBUG)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await orchestrator.run(request, request_id="req_test", budget=budget)
    elapsed_ms = (loop.time() - started) * 1000

    assert elapsed_ms < 12000, elapsed_ms  # bounded, despite 5s-per-page routes
    assert result.status in ("partial", "success")
    assert result.pages  # the homepage was fast and survives
    assert result.crawl_summary.elapsed_ms <= budget.deadline_ms + 3000


async def test_deadline_cancellation_is_reported_not_hidden(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(deadline_ms=5000)
    request = ResearchRequest(url=fixture_servers["slow"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    assert result.crawl_summary.stopped_reason == "deadline"
    assert any(
        failed.reason_code == str(ReasonCode.DEADLINE_EXCEEDED)
        for failed in result.failed_pages
    )


async def test_expired_deadline_still_returns_a_valid_response(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(deadline_ms=1000)
    request = ResearchRequest(url=fixture_servers["slow"].url("/"))
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    assert result.status in ("failed", "partial", "success")
    assert result.request_id == "req_test"
    assert result.schema_version == "1.1"


# ---------------------------------------------------------------------------
# 6. robots Disallow: /
# ---------------------------------------------------------------------------


async def test_blanket_robots_disallow_fails_the_site(orchestrator, fixture_servers) -> None:
    server = fixture_servers["blocked"]
    server.clear_requests()
    result = await run(orchestrator, server.url("/"))

    assert result.status == "failed"
    assert result.status_reason == str(ReasonCode.ROBOTS_DISALLOWED)
    assert result.pages == []
    assert result.evidence_sufficient is False
    # Only robots.txt itself may have been fetched.
    assert all(path == "/robots.txt" for path in server.requests)


async def test_robots_can_be_configured_off_per_request(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(enabled=False), fetcher)
    )
    request = ResearchRequest(url=fixture_servers["blocked"].url("/"))
    result = await orchestrator.run(request, request_id="req_test")
    assert result.status in ("success", "partial")
    assert result.pages


async def test_partially_disallowed_path_is_skipped(orchestrator, fixture_servers) -> None:
    server = fixture_servers["partial_robots"]
    server.clear_requests()
    result = await run(orchestrator, server.url("/"))

    assert "/pricing" not in server.requests
    assert not any(url.endswith("/pricing") for url in urls_of(result))
    assert any(url.endswith("/services") for url in urls_of(result))


# ---------------------------------------------------------------------------
# 7. Thin one-page site: blog fallback
# ---------------------------------------------------------------------------


async def test_thin_site_triggers_the_blog_fallback(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["thin"].url("/"))

    assert "thin_site_blog_fallback" in result.notes
    blog_pages = [p for p in result.pages if p.page_type == "blog_post"]
    assert len(blog_pages) == 1
    assert blog_pages[0].url.endswith("/blog/new-facility")


async def test_blog_fallback_never_spends_the_render_budget(
    guard, fetcher, fixture_servers
) -> None:
    calls: list[str] = []

    class CountingRenderer(NullRenderer):
        @property
        def available(self) -> bool:
            return True

        async def render(self, url, *, scope, timeout_ms):
            calls.append(url)
            return RenderResult(ok=False, reason=ReasonCode.RENDER_FAILED)

    orchestrator = ResearchOrchestrator(
        guard, fetcher, CountingRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    request = ResearchRequest(url=fixture_servers["thin"].url("/"))
    await orchestrator.run(request, request_id="req_test")
    assert not any("/blog/new-facility" in url for url in calls)


async def test_healthy_site_does_not_trigger_the_blog_fallback(
    orchestrator, fixture_servers
) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    assert "thin_site_blog_fallback" not in result.notes
    assert not any(p.page_type == "blog_post" for p in result.pages)


async def test_blog_index_is_never_selected(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["thin"].url("/"))
    assert not any(url.rstrip("/").endswith("/blog") for url in urls_of(result))


# ---------------------------------------------------------------------------
# 8. Locale-duplicated site
# ---------------------------------------------------------------------------


async def test_locale_duplicates_do_not_consume_the_budget(
    orchestrator, fixture_servers
) -> None:
    result = await run(orchestrator, fixture_servers["locale"].url("/"))
    locales = ("de", "fr", "es", "it", "pt", "nl")
    localized = [url for url in urls_of(result) if any(f"/{c}/" in url for c in locales)]
    assert len(localized) <= 1, urls_of(result)
    assert any(url.endswith("/services") and "/de/" not in url for url in urls_of(result))


# ---------------------------------------------------------------------------
# Budgeting and assembly
# ---------------------------------------------------------------------------


async def test_global_content_budget_is_never_silently_exceeded(
    guard, fetcher, fixture_servers
) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(global_content_budget=4000)
    request = ResearchRequest(url=fixture_servers["static"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    total = sum(len(page.content) for page in result.pages)
    assert total <= 4000
    assert result.crawl_summary.content_chars == total
    assert any(page.truncated for page in result.pages)
    assert result.crawl_summary.stopped_reason == "char_budget_reached"


async def test_truncation_starts_from_the_lowest_ranked_page(
    guard, fetcher, fixture_servers
) -> None:
    """The homepage and top-ranked evidence must never be what gets cut."""
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(global_content_budget=8000)
    request = ResearchRequest(url=fixture_servers["static"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    assert result.pages[0].page_type == "homepage"
    assert not result.pages[0].truncated
    assert result.pages[-1].truncated


async def test_per_page_caps_are_tiered(guard, fetcher, fixture_servers) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = (
        get_limits()
        .budget("standard")
        .with_overrides(page_content_cap_top=600, page_content_cap_rest=200)
    )
    request = ResearchRequest(url=fixture_servers["static"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test", budget=budget)

    for index, page in enumerate(result.pages):
        limit = 600 if index < budget.page_content_cap_top_ranks else 200
        assert len(page.content) <= limit


async def test_max_pages_is_respected(guard, fetcher, fixture_servers) -> None:
    orchestrator = ResearchOrchestrator(
        guard, fetcher, NullRenderer(), RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(max_pages=3)
    request = ResearchRequest(url=fixture_servers["static"].url("/"))
    result = await orchestrator.run(request, request_id="req_test", budget=budget)
    assert len(result.pages) <= 3


async def test_homepage_is_always_first(orchestrator, fixture_servers) -> None:
    for name in ("static", "depth2", "locale", "thin"):
        result = await run(orchestrator, fixture_servers[name].url("/"))
        if result.pages:
            assert result.pages[0].page_type == "homepage", name


async def test_pages_are_sorted_by_relevance_after_the_homepage(
    orchestrator, fixture_servers
) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    scores = [p["relevance_score"] for p in result.debug["pages"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Coverage and evidence sufficiency
# ---------------------------------------------------------------------------


async def test_coverage_reports_found_and_missing(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["static"].url("/"))
    assert "services" in result.coverage.found
    assert "about" in result.coverage.found
    assert "case_study" in result.coverage.missing  # the fixture has no case studies
    assert set(result.coverage.found) & set(result.coverage.missing) == set()


async def test_evidence_sufficient_gate(orchestrator, fixture_servers) -> None:
    rich = await run(orchestrator, fixture_servers["static"].url("/"))
    assert rich.evidence_sufficient is True
    assert rich.crawl_summary.total_words >= 300

    blocked = await run(orchestrator, fixture_servers["blocked"].url("/"))
    assert blocked.evidence_sufficient is False


async def test_failed_sites_carry_a_reason_and_no_pages(orchestrator, fixture_servers) -> None:
    result = await run(orchestrator, fixture_servers["blocked"].url("/"))
    assert result.status == "failed"
    assert result.status_reason
    assert result.pages == []
    assert result.failed_pages


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1/", ReasonCode.INVALID_URL),
        ("file:///etc/passwd", ReasonCode.BLOCKED_SCHEME),
        ("http://localhost/", ReasonCode.PRIVATE_ADDRESS),
    ],
)
async def test_guard_failures_surface_as_site_failures(orchestrator, url, reason) -> None:
    request = ResearchRequest(url=url)
    result = await orchestrator.run(request, request_id="req_test")
    assert result.status == "failed"
    assert result.status_reason == str(reason)


async def test_spa_without_a_renderer_fails_honestly(orchestrator, fixture_servers) -> None:
    """No renderer means an honest failure, never silently empty content."""
    result = await run(orchestrator, fixture_servers["spa"].url("/"))
    assert result.status == "failed"
    assert result.status_reason == str(ReasonCode.TOO_THIN)
    assert result.pages == []


async def test_spa_with_a_working_renderer_succeeds(guard, fetcher, fixture_servers) -> None:
    """The renderer adapter is the only thing that changes between these cases."""
    rendered_html = (
        "<html lang='en'><head><title>SPA Ltd</title></head><body><main><h1>SPA Ltd</h1>"
        "<p>" + ("word " * 400) + "</p></main></body></html>"
    )

    class StubRenderer(NullRenderer):
        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def available(self) -> bool:
            return True

        async def render(self, url, *, scope, timeout_ms):
            self.calls.append(url)
            return RenderResult(ok=True, html=rendered_html, final_url=url, status_code=200)

    renderer = StubRenderer()
    orchestrator = ResearchOrchestrator(
        guard, fetcher, renderer, RobotsPolicy(RobotsSettings(), fetcher)
    )
    request = ResearchRequest(url=fixture_servers["spa"].url("/"), advanced=DEBUG)
    result = await orchestrator.run(request, request_id="req_test")

    assert result.pages
    assert result.crawl_summary.browser_renders >= 1
    assert result.debug["js_mode"] is True
    assert "js_site_mode" in result.notes


async def test_render_budget_is_capped(guard, fetcher, fixture_servers) -> None:
    class CountingRenderer(NullRenderer):
        def __init__(self) -> None:
            self.calls = 0

        @property
        def available(self) -> bool:
            return True

        async def render(self, url, *, scope, timeout_ms):
            self.calls += 1
            return RenderResult(ok=False, reason=ReasonCode.RENDER_FAILED)

    renderer = CountingRenderer()
    orchestrator = ResearchOrchestrator(
        guard, fetcher, renderer, RobotsPolicy(RobotsSettings(), fetcher)
    )
    budget = get_limits().budget("standard").with_overrides(max_renders=1)
    request = ResearchRequest(url=fixture_servers["spa"].url("/"))
    await orchestrator.run(request, request_id="req_test", budget=budget)
    assert renderer.calls <= 1


async def test_no_llm_or_external_calls_happen(orchestrator, fixture_servers) -> None:
    """Invariant 1: the only hosts contacted are the site under research."""
    server = fixture_servers["static"]
    others = {
        name: srv for name, srv in fixture_servers.items() if name != "static"
    }
    for srv in others.values():
        srv.clear_requests()

    await run(orchestrator, server.url("/"))

    for name, srv in others.items():
        assert srv.requests == [], f"{name} was contacted"
