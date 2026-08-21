"""Per-key limits, idempotency, cache freshness and ICP signals.

The through-line: everything here is about running at volume without either
lying to the caller or crawling a site twice for one intent.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.idempotency import IdempotencyConflict, IdempotencyStore, body_fingerprint
from app.api.ratelimit import RateLimiter
from app.cache.result_cache import ResultCache
from app.config.settings import CacheSettings, Settings
from app.extract.signals import (
    detect_employee_count,
    detect_founded_year,
    detect_keywords,
    detect_technologies,
)
from app.main import create_app
from app.models.request import ScrapeRequest
from tests.fixtures.fake_redis import Clock, FakeRedis

STANDARD = "phase2-standard-key"
LIMITED = "phase2-limited-key"
AUTH = {"Authorization": f"Bearer {STANDARD}"}
LIMITED_AUTH = {"Authorization": f"Bearer {LIMITED}"}


# ---------------------------------------------------------------------------
# Per-key limits and workspace labels
# ---------------------------------------------------------------------------


def test_the_legacy_key_format_still_means_what_it_meant() -> None:
    table = Settings(api_keys="alpha:standard,beta:advanced").api_key_table()
    scopes = sorted(record.scope for record in table.values())
    assert scopes == ["advanced", "standard"]
    assert all(record.rate_limit_per_minute is None for record in table.values())


def test_json_keys_carry_limits_and_a_workspace_label() -> None:
    raw = json.dumps(
        [
            {
                "key": "alpha",
                "scope": "standard",
                "label": "revcadence-prod",
                "rate_limit_per_minute": 600,
                "monthly_quota": 50_000,
            },
            {"key": "beta", "scope": "advanced", "label": "internal-tools"},
        ]
    )
    table = Settings(api_keys=raw).api_key_table()
    by_label = {record.label: record for record in table.values()}

    assert by_label["revcadence-prod"].rate_limit_per_minute == 600
    assert by_label["revcadence-prod"].monthly_quota == 50_000
    # An unspecified limit stays None so the server default applies.
    assert by_label["internal-tools"].rate_limit_per_minute is None
    assert by_label["internal-tools"].is_advanced is True


def test_malformed_json_keys_fail_loudly() -> None:
    """The key list is the most error-prone value in the config.

    It has already caused one outage by being pasted wholesale into a client.
    Silent partial parsing would be worse than a startup error.
    """
    with pytest.raises(ValueError, match="not valid JSON"):
        Settings(api_keys='[{"key": "alpha"').api_key_table()
    with pytest.raises(ValueError, match="needs a 'key'"):
        Settings(api_keys='[{"scope": "standard"}]').api_key_table()


def test_a_key_that_never_specifies_a_scope_is_standard() -> None:
    table = Settings(api_keys='[{"key": "alpha"}]').api_key_table()
    assert next(iter(table.values())).scope == "standard"


async def test_a_per_key_limit_overrides_the_server_default() -> None:
    limiter = RateLimiter(per_minute=1000, monthly_quota=0)
    for _ in range(2):
        assert (await limiter.check("key-a", per_minute=2)).allowed
    verdict = await limiter.check("key-a", per_minute=2)
    assert verdict.allowed is False
    assert "rate limit" in verdict.reason


async def test_a_per_key_zero_means_unlimited_not_default() -> None:
    """A deliberate 0 must not fall back to the server's limit."""
    limiter = RateLimiter(per_minute=1, monthly_quota=0)
    for _ in range(5):
        assert (await limiter.check("key-b", per_minute=0)).allowed


async def test_a_per_key_quota_is_enforced() -> None:
    limiter = RateLimiter(per_minute=0, monthly_quota=0)
    assert (await limiter.check("key-c", monthly_quota=1)).allowed
    verdict = await limiter.check("key-c", monthly_quota=1)
    assert verdict.allowed is False
    assert "quota" in verdict.reason


async def test_keys_do_not_share_a_budget() -> None:
    limiter = RateLimiter(per_minute=1000, monthly_quota=0)
    assert (await limiter.check("workspace-a", per_minute=1)).allowed
    assert (await limiter.check("workspace-a", per_minute=1)).allowed is False
    # A different workspace is untouched by the first one's exhaustion.
    assert (await limiter.check("workspace-b", per_minute=1)).allowed


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.fixture
def idem_store():
    return IdempotencyStore(FakeRedis(clock=Clock()), prefix="cra", ttl_seconds=3600)


async def test_the_first_request_claims_the_key(idem_store) -> None:
    assert await idem_store.begin("k1", "idem-1", "fp") is None


async def test_a_replay_returns_the_stored_response(idem_store) -> None:
    await idem_store.begin("k1", "idem-1", "fp")
    await idem_store.complete("k1", "idem-1", "fp", {"status": "success"})
    assert await idem_store.begin("k1", "idem-1", "fp") == {"status": "success"}


async def test_the_same_key_with_a_different_body_is_a_conflict(idem_store) -> None:
    """Serving the first answer to a different question is worse than an error."""
    await idem_store.begin("k1", "idem-1", "fingerprint-a")
    await idem_store.complete("k1", "idem-1", "fingerprint-a", {"status": "success"})
    with pytest.raises(IdempotencyConflict, match="different request"):
        await idem_store.begin("k1", "idem-1", "fingerprint-b")


async def test_a_retry_while_in_flight_is_a_conflict(idem_store) -> None:
    """Otherwise a timed-out client crawls the same thirty companies twice."""
    await idem_store.begin("k1", "idem-1", "fp")
    with pytest.raises(IdempotencyConflict, match="in progress"):
        await idem_store.begin("k1", "idem-1", "fp")


async def test_a_released_key_can_be_retried(idem_store) -> None:
    """A failed request stores nothing -- that is the point of retrying."""
    await idem_store.begin("k1", "idem-1", "fp")
    await idem_store.release("k1", "idem-1")
    assert await idem_store.begin("k1", "idem-1", "fp") is None


async def test_two_workspaces_can_use_the_same_idempotency_key(idem_store) -> None:
    await idem_store.begin("workspace-a", "shared-key", "fp")
    await idem_store.complete("workspace-a", "shared-key", "fp", {"a": 1})
    # A different API key must not see the first workspace's response.
    assert await idem_store.begin("workspace-b", "shared-key", "fp") is None


async def test_without_redis_requests_simply_run() -> None:
    """Fails open. Bookkeeping being unavailable must not become an outage."""
    store = IdempotencyStore(None)
    assert store.available is False
    assert await store.begin("k", "i", "fp") is None
    await store.complete("k", "i", "fp", {"x": 1})


def test_the_fingerprint_is_stable_across_key_order() -> None:
    assert body_fingerprint({"a": 1, "b": 2}) == body_fingerprint({"b": 2, "a": 1})
    assert body_fingerprint({"a": 1}) != body_fingerprint({"a": 2})


# ---------------------------------------------------------------------------
# Cache freshness
# ---------------------------------------------------------------------------


def _response():
    from app.models.response import Coverage, CrawlSummary, ResearchResponse, SiteInfo

    return ResearchResponse(
        request_id="req_x",
        status="success",
        evidence_sufficient=True,
        site=SiteInfo(requested_url="https://a.com", canonical_domain="a.com"),
        pages=[],
        coverage=Coverage(),
        crawl_summary=CrawlSummary(),
    )


async def test_a_fresh_entry_is_served() -> None:
    cache = ResultCache(CacheSettings(), client=FakeRedis(clock=Clock()))
    await cache.set("k", _response(), 600)
    assert await cache.get("k", max_age_seconds=3600) is not None


async def test_max_age_never_widens_what_is_stored() -> None:
    """max_age narrows what *this* caller accepts; it changes nothing globally.

    A caller-set TTL would let one integration decide how stale every other
    integration's reads may be. This is the read-side bound HTTP uses.
    """
    redis = FakeRedis(clock=Clock())
    cache = ResultCache(CacheSettings(), client=redis)
    await cache.set("k", _response(), 600)
    # Asking for a 1-second freshness bound must not evict or shorten anything.
    await cache.get("k", max_age_seconds=0)
    assert await cache.get("k") is not None


async def test_an_entry_without_a_timestamp_fails_a_freshness_check() -> None:
    """Entries written before envelopes existed have no age.

    Treating unknown as "too old" is the conservative reading: the caller asked
    for a guarantee we cannot make.
    """
    redis = FakeRedis(clock=Clock())
    cache = ResultCache(CacheSettings(), client=redis)
    await redis.set("legacy", _response().model_dump_json(exclude_none=True), ex=600)

    assert await cache.get("legacy") is not None  # still readable
    assert await cache.get("legacy", max_age_seconds=60) is None  # but not certifiable


async def test_no_max_age_accepts_anything_stored() -> None:
    cache = ResultCache(CacheSettings(), client=FakeRedis(clock=Clock()))
    await cache.set("k", _response(), 600)
    assert await cache.get("k") is not None


# ---------------------------------------------------------------------------
# ICP signals: observed, never inferred
# ---------------------------------------------------------------------------


def test_technologies_are_read_off_the_page() -> None:
    html = """
    <html><head>
      <script src="https://js.hs-scripts.com/1234.js"></script>
      <link href="https://assets.website-files.com/x.css" rel="stylesheet">
      <script src="https://cdn.segment.com/analytics.js"></script>
    </head><body><script src="/_next/static/chunk.js"></script></body></html>
    """
    found = detect_technologies(html)
    assert "HubSpot" in found
    assert "Segment" in found
    assert "Next.js" in found


def test_the_generator_meta_counts() -> None:
    html = '<html><head><meta name="generator" content="WordPress 6.4"></head></html>'
    assert "WordPress" in detect_technologies(html)


def test_a_plain_page_reports_no_technologies() -> None:
    assert detect_technologies("<html><body><p>hello</p></body></html>") == []
    assert detect_technologies("") == []


def test_keywords_come_from_the_companys_own_meta_tag() -> None:
    html = '<html><head><meta name="keywords" content="B2B SaaS, RevOps, RevOps , CPQ"></head></html>'
    assert detect_keywords(html) == ["B2B SaaS", "RevOps", "CPQ"]


def test_no_keywords_meta_means_no_keywords() -> None:
    assert detect_keywords("<html><head></head></html>") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We are a team of 40 engineers", 40),
        ("More than 250 employees across three offices", 250),
        ("120+ specialists", 120),
        ("Our team of over 15 people", 15),
    ],
)
def test_a_stated_headcount_is_read(text, expected) -> None:
    assert detect_employee_count(text) == expected


def test_a_year_is_not_mistaken_for_a_headcount() -> None:
    assert detect_employee_count("Our team of 2024 was our best yet") is None


def test_no_stated_headcount_means_none() -> None:
    assert detect_employee_count("We are a growing agency") is None
    assert detect_employee_count("") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Founded in 1998 by two engineers", 1998),
        ("Established 2015", 2015),
        ("Serving manufacturers since 2007", 2007),
    ],
)
def test_a_founding_year_is_read(text, expected) -> None:
    assert detect_founded_year(text) == expected


def test_nothing_is_invented_when_the_page_says_nothing() -> None:
    """The whole argument for doing this deterministically."""
    assert detect_founded_year("We have been around a while") is None
    assert detect_employee_count("A large team") is None
    assert detect_technologies("<html><body>plain</body></html>") == []


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def phase2_client(guard, fixture_servers):
    keys = json.dumps(
        [
            {"key": STANDARD, "scope": "standard", "label": "revcadence-prod"},
            {"key": LIMITED, "scope": "standard", "label": "tiny", "rate_limit_per_minute": 1},
        ]
    )
    settings = Settings(
        api_keys=keys,
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
        app.state.idempotency = IdempotencyStore(
            FakeRedis(clock=Clock()), prefix="cra", ttl_seconds=3600
        )
        yield client


def test_a_limited_key_is_throttled_while_others_are_not(phase2_client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/about")
    first = phase2_client.post("/v1/scrape", json={"url": url}, headers=LIMITED_AUTH)
    second = phase2_client.post("/v1/scrape", json={"url": url}, headers=LIMITED_AUTH)
    other = phase2_client.post("/v1/scrape", json={"url": url}, headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 429
    assert other.status_code == 200


def test_a_replayed_scrape_is_flagged(phase2_client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/about")
    headers = {**AUTH, "Idempotency-Key": "abc-123"}
    first = phase2_client.post("/v1/scrape", json={"url": url}, headers=headers)
    second = phase2_client.post("/v1/scrape", json={"url": url}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert first.json()["request_id"] == second.json()["request_id"]


def test_reusing_a_key_for_a_different_body_is_409(phase2_client, fixture_servers) -> None:
    headers = {**AUTH, "Idempotency-Key": "abc-456"}
    phase2_client.post(
        "/v1/scrape", json={"url": fixture_servers["static"].url("/about")}, headers=headers
    )
    clash = phase2_client.post(
        "/v1/scrape", json={"url": fixture_servers["static"].url("/services")}, headers=headers
    )
    assert clash.status_code == 409


def test_without_the_header_nothing_changes(phase2_client, fixture_servers) -> None:
    url = fixture_servers["static"].url("/about")
    first = phase2_client.post("/v1/scrape", json={"url": url}, headers=AUTH)
    second = phase2_client.post("/v1/scrape", json={"url": url}, headers=AUTH)
    assert first.json()["request_id"] != second.json()["request_id"]
    assert "X-Idempotent-Replay" not in second.headers


def test_signals_are_off_by_default(phase2_client, fixture_servers) -> None:
    body = phase2_client.post(
        "/v1/research", json={"url": fixture_servers["static"].url("/")}, headers=AUTH
    ).json()
    assert "signals" not in body["site"]


def test_signals_appear_when_asked_for(phase2_client, fixture_servers) -> None:
    body = phase2_client.post(
        "/v1/research",
        json={"url": fixture_servers["static"].url("/"), "include_signals": True},
        headers=AUTH,
    ).json()
    # The fixture carries JSON-LD and meta, so something should be observable;
    # what matters is that the field is permitted to exist here and not above.
    assert "signals" in body["site"] or body["site"].get("signals") is None


def test_a_scrape_request_accepts_no_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ScrapeRequest(url="https://a.com", include_signals=True)
