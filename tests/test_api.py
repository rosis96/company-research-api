"""API-layer tests: auth, status-code policy, caching, rate limiting, schema (SS9-SS12)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.cache.result_cache import InMemoryCache
from app.config.loader import get_limits
from app.config.settings import CacheSettings, Settings
from app.main import create_app
from app.models.response import SCHEMA_VERSION
from app.security.reasons import ReasonCode

STANDARD_KEY = "standard-test-key"
ADVANCED_KEY = "advanced-test-key"


@pytest.fixture
def client(guard, fixture_servers):
    settings = Settings(
        api_keys=f"{STANDARD_KEY}:standard,{ADVANCED_KEY}:advanced",
        environment="test",
        log_json=False,
        log_level="WARNING",
        cache={"enabled": False},
        renderer={"enabled": False},
        rate_limit_per_minute=1000,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        # Swap in the test guard so fixture hostnames on loopback are reachable.
        # Production wiring never uses a policy with extra_allowed_networks.
        #
        # ResearchService receives its dependencies explicitly rather than
        # reading app.state, which keeps it framework-free -- so the swap has to
        # be applied to the service as well, not only to app.state.
        app.state.guard = guard
        app.state.cache = InMemoryCache(CacheSettings(enabled=True))
        app.state.research_service.guard = guard
        app.state.research_service.cache = app.state.cache
        test_client.app_instance = app
        yield test_client


def post(client, url: str, key: str = STANDARD_KEY, **body):
    return client.post(
        "/v1/research",
        json={"url": url, **body},
        headers={"Authorization": f"Bearer {key}"},
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_component_state(client) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == SCHEMA_VERSION
    assert set(body) == {
        "status",
        "version",
        "schema_version",
        "config_version",
        "renderer_available",
        "cache_available",
    }


def test_health_needs_no_authentication(client) -> None:
    assert client.get("/v1/health").status_code == 200


# ---------------------------------------------------------------------------
# Authentication (SS12)
# ---------------------------------------------------------------------------


def test_missing_authorization_is_401(client) -> None:
    response = client.post("/v1/research", json={"url": "https://acme.com"})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong-key", "Basic abc123", "bearer", "Bearer ", "Token abc"],
)
def test_bad_authorization_is_401(client, header: str) -> None:
    response = client.post(
        "/v1/research", json={"url": "https://acme.com"}, headers={"Authorization": header}
    )
    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(client, fixture_servers) -> None:
    response = client.post(
        "/v1/research",
        json={"url": fixture_servers["static"].url("/")},
        headers={"Authorization": f"bEaReR {STANDARD_KEY}"},
    )
    assert response.status_code == 200


def test_advanced_options_require_an_advanced_key(client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/")
    assert post(client, url, key=STANDARD_KEY, advanced={"debug": True}).status_code == 403
    assert post(client, url, key=ADVANCED_KEY, advanced={"debug": True}).status_code == 200


def test_debug_block_only_appears_for_advanced_callers(client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/")
    plain = post(client, url).json()
    assert "debug" not in plain

    debugged = post(client, url, key=ADVANCED_KEY, advanced={"debug": True}).json()
    assert "debug" in debugged
    assert "relevance_score" in debugged["debug"]["pages"][0]


# ---------------------------------------------------------------------------
# Status-code policy (SS11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1/", ReasonCode.INVALID_URL),
        ("http://localhost/", ReasonCode.PRIVATE_ADDRESS),
        ("file:///etc/passwd", ReasonCode.BLOCKED_SCHEME),
        ("https://this-domain-does-not-resolve-9f8a.com/", ReasonCode.DNS_FAILURE),
    ],
)
def test_crawl_failures_return_http_200(client, url: str, reason: ReasonCode) -> None:
    """A blocked or dead site is a result, not an infrastructure error.

    Returning 4xx/5xx here makes ChatGPT treat it as a tool failure, whose most
    likely recovery is falling back on model knowledge of the company.
    """
    response = post(client, url)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["status_reason"] == str(reason)
    assert body["pages"] == []
    assert body["evidence_sufficient"] is False


def test_robots_blocked_site_returns_200_failed(client, fixture_servers) -> None:
    response = post(client, fixture_servers["blocked"].url("/"))
    assert response.status_code == 200
    assert response.json()["status_reason"] == str(ReasonCode.ROBOTS_DISALLOWED)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": ""},
        {"url": "x" * 3000},
        {"url": "https://acme.com", "research_depth": "exhaustive"},
        {"url": "https://acme.com", "unexpected_field": 1},
        {"url": "https://acme.com", "focus": ["nonsense"]},
    ],
)
def test_malformed_requests_are_422(client, body: dict) -> None:
    response = client.post(
        "/v1/research", json=body, headers={"Authorization": f"Bearer {STANDARD_KEY}"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_rate_limit_returns_429_with_retry_after(guard, fixture_servers) -> None:
    settings = Settings(
        api_keys=f"{STANDARD_KEY}:standard",
        environment="test",
        log_json=False,
        log_level="WARNING",
        cache={"enabled": False},
        renderer={"enabled": False},
        rate_limit_per_minute=1,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.guard = guard
        app.state.research_service.guard = guard
        url = fixture_servers["static"].url("/")
        first = post(test_client, url)
        second = post(test_client, url)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"
    assert int(second.headers["Retry-After"]) > 0


# ---------------------------------------------------------------------------
# Response schema (SS10)
# ---------------------------------------------------------------------------


def test_response_matches_the_v1_1_schema(client, fixture_servers) -> None:
    body = post(client, fixture_servers["static"].url("/")).json()

    assert body["schema_version"] == "1.2"
    assert set(body) <= {
        "schema_version",
        "request_id",
        "status",
        "status_reason",
        "evidence_sufficient",
        "site",
        "pages",
        "failed_pages",
        "coverage",
        "crawl_summary",
        "notes",
        "debug",
    }
    assert set(body["site"]) <= {
        "requested_url",
        "final_url",
        "canonical_domain",
        "domain_rebased",
        "language",
        "site_name",
        "tagline",
        "organization",
        "site_wide_content",
    }
    for page in body["pages"]:
        assert set(page) <= {
            "url",
            "final_url",
            "page_type",
            "title",
            "headings",
            "word_count",
            "truncated",
            "content",
        }
    assert set(body["coverage"]) == {"found", "missing"}
    assert set(body["crawl_summary"]) <= {
        "pages_scraped",
        "pages_failed",
        "browser_renders",
        "max_depth_reached",
        "total_words",
        "content_chars",
        "elapsed_ms",
        "stopped_reason",
        "cache_hit",
    }


def test_slimmed_fields_are_absent_from_the_default_payload(client, fixture_servers) -> None:
    """Operational metadata belongs in `debug`, not in every response."""
    body = post(client, fixture_servers["static"].url("/")).json()
    for page in body["pages"]:
        for removed in ("relevance_score", "discovered_via", "depth", "fetch_method",
                        "http_status", "meta_description"):
            assert removed not in page
    assert "redirect_chain" not in body["site"]
    assert "robots_policy" not in body["site"]
    for removed in ("urls_discovered", "urls_after_filtering", "pages_selected"):
        assert removed not in body["crawl_summary"]


def test_final_url_only_present_when_it_differs(client, fixture_servers) -> None:
    body = post(client, fixture_servers["static"].url("/")).json()
    for page in body["pages"]:
        assert "final_url" not in page or page["final_url"] != page["url"]


def test_metadata_stays_under_ten_percent_of_the_payload(client, fixture_servers) -> None:
    """SS8 asserts this in tests rather than trusting prose."""
    body = post(client, fixture_servers["static"].url("/")).json()
    payload = json.dumps(body, ensure_ascii=False)
    content_chars = sum(len(page["content"]) for page in body["pages"])
    site_wide = len(body["site"].get("site_wide_content") or "")

    if content_chars < get_limits().response["metadata_assert_min_content"]:
        pytest.skip("fixture content too small for a meaningful ratio")

    metadata_chars = len(payload) - content_chars - site_wide
    assert metadata_chars / len(payload) < 0.10, (
        f"metadata {metadata_chars} of {len(payload)} chars"
    )


def test_payload_stays_under_the_gpt_actions_limit(client, fixture_servers) -> None:
    """100,000 characters is a hard ceiling for GPT Actions."""
    for name in ("static", "depth2", "locale"):
        body = post(client, fixture_servers[name].url("/")).json()
        assert len(json.dumps(body, ensure_ascii=False)) < 100_000


def test_failed_pages_carry_typed_reasons(client, fixture_servers) -> None:
    body = post(client, fixture_servers["static"].url("/")).json()
    for failed in body["failed_pages"]:
        assert set(failed) <= {"url", "reason_code", "http_status"}
        assert ReasonCode(failed["reason_code"])


# ---------------------------------------------------------------------------
# Caching through the API
# ---------------------------------------------------------------------------


def test_second_request_is_served_from_cache(client, fixture_servers) -> None:
    server = fixture_servers["static"]
    first = post(client, server.url("/"))
    assert first.headers["X-Cache"] == "MISS"
    assert first.json()["crawl_summary"]["cache_hit"] is False

    server.clear_requests()
    second = post(client, server.url("/"))
    assert second.headers["X-Cache"] == "HIT"
    assert second.json()["crawl_summary"]["cache_hit"] is True
    assert server.requests == []  # no network work at all


def test_cache_hit_gets_a_fresh_request_id(client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/")
    first = post(client, url).json()
    second = post(client, url).json()
    assert first["request_id"] != second["request_id"]


def test_force_refresh_bypasses_the_cache(client, fixture_servers) -> None:
    server = fixture_servers["static"]
    post(client, server.url("/"))
    server.clear_requests()
    response = post(client, server.url("/"), force_refresh=True)
    assert response.headers["X-Cache"] == "MISS"
    assert server.requests


def test_different_depths_do_not_share_a_cache_entry(client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/")
    post(client, url, research_depth="standard")
    response = post(client, url, research_depth="quick")
    assert response.headers["X-Cache"] == "MISS"


# ---------------------------------------------------------------------------
# OpenAPI surface (SS9)
# ---------------------------------------------------------------------------


def test_openapi_describes_the_research_endpoint(client) -> None:
    spec = client.get("/openapi.json").json()
    assert "/v1/research" in spec["paths"]
    operation = spec["paths"]["/v1/research"]["post"]
    assert len(operation["description"]) <= 300
    assert "never invents content" in operation["description"]


def test_health_is_hidden_from_the_public_schema(client) -> None:
    spec = client.get("/openapi.json").json()
    assert "/v1/health" not in spec["paths"]


def test_request_schema_has_no_numeric_crawl_knobs(client) -> None:
    """A model given max_pages will supply a plausible number with no basis."""
    spec = client.get("/openapi.json").json()
    properties = spec["components"]["schemas"]["ResearchRequest"]["properties"]
    for knob in ("max_pages", "max_depth", "timeout", "max_content_length"):
        assert knob not in properties
    assert set(properties) == {
            "url",
            "research_depth",
            "focus",
            "force_refresh",
            "output_format",
            "include_signals",
            "max_age_seconds",
            "advanced",
        }


# ---------------------------------------------------------------------------
# Static pages required to publish a GPT
# ---------------------------------------------------------------------------


def test_privacy_policy_is_served(client) -> None:
    """A reachable privacy policy URL is required to publish a GPT publicly."""
    response = client.get("/privacy")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Privacy Policy" in response.text
    assert "robots.txt" in response.text  # the website-owner opt-out section


def test_bot_info_page_is_served(client) -> None:
    """The page site owners look for after seeing the User-Agent in their logs."""
    response = client.get("/bot")
    assert response.status_code == 200
    assert "CompanyResearchBot" in response.text
    assert "Disallow: /" in response.text


def test_static_pages_need_no_authentication(client) -> None:
    for path in ("/privacy", "/bot"):
        assert client.get(path).status_code == 200


def test_static_pages_are_absent_from_the_gpt_schema(client) -> None:
    spec = client.get("/openapi.json").json()
    assert "/privacy" not in spec["paths"]
    assert "/bot" not in spec["paths"]
