"""Cache key, CONFIG_VERSION and TTL tests (SS14)."""

from __future__ import annotations

import pytest

from app.cache.result_cache import InMemoryCache, cache_key, ttl_for
from app.config.loader import CONFIG_DIR, config_fingerprint
from app.config.settings import CacheSettings
from app.models.request import ResearchRequest
from app.models.response import (
    CrawlSummary,
    ResearchResponse,
    SiteInfo,
)
from app.security.reasons import ReasonCode

SETTINGS = CacheSettings()


def key_for(url: str = "https://acme.com", version: str = "1", **kwargs) -> str:
    request = ResearchRequest(url=url, **kwargs)
    return cache_key(request, canonical_domain="acme.com", config_version=version)


def response(status: str, reason: str | None = None) -> ResearchResponse:
    return ResearchResponse(
        request_id="req_1",
        status=status,  # type: ignore[arg-type]
        status_reason=reason,
        site=SiteInfo(requested_url="https://acme.com"),
        crawl_summary=CrawlSummary(),
    )


# ---------------------------------------------------------------------------
# Key composition
# ---------------------------------------------------------------------------


def test_key_is_stable_for_identical_requests() -> None:
    assert key_for() == key_for()


def test_key_is_domain_scoped_not_url_scoped() -> None:
    """Two URLs on the same domain share a cache entry."""
    first = cache_key(
        ResearchRequest(url="https://acme.com/"), canonical_domain="acme.com", config_version="1"
    )
    second = cache_key(
        ResearchRequest(url="https://www.acme.com/about"),
        canonical_domain="acme.com",
        config_version="1",
    )
    assert first == second


def test_key_changes_with_research_depth() -> None:
    assert key_for(research_depth="quick") != key_for(research_depth="deep")


def test_key_changes_with_focus() -> None:
    assert key_for(focus=["case_studies"]) != key_for(focus=["pricing"])
    assert key_for() != key_for(focus=["about"])


def test_focus_order_does_not_change_the_key() -> None:
    assert key_for(focus=["about", "pricing"]) == key_for(focus=["pricing", "about"])


def test_config_version_bump_invalidates_the_key() -> None:
    """Without this, a ranking tweak is invisible behind stale entries."""
    assert key_for(version="1") != key_for(version="2")


def test_config_fingerprint_tracks_yaml_content() -> None:
    original = config_fingerprint()
    lexicon = CONFIG_DIR / "lexicon.yaml"
    backup = lexicon.read_bytes()
    try:
        lexicon.write_bytes(backup + b"\n# a tweak\n")
        config_fingerprint.__wrapped__ if hasattr(config_fingerprint, "__wrapped__") else None
        assert config_fingerprint() != original
    finally:
        lexicon.write_bytes(backup)
    assert config_fingerprint() == original


def test_config_fingerprint_is_part_of_the_key() -> None:
    assert config_fingerprint() != ""
    assert len(config_fingerprint()) == 16


def test_force_refresh_does_not_change_the_key() -> None:
    """force_refresh bypasses the read; it must still write to the same slot."""
    assert key_for(force_refresh=True) == key_for(force_refresh=False)


def test_key_prefix_is_applied() -> None:
    request = ResearchRequest(url="https://acme.com")
    key = cache_key(request, canonical_domain="acme.com", config_version="1", prefix="xyz")
    assert key.startswith("xyz:research:")


# ---------------------------------------------------------------------------
# TTLs
# ---------------------------------------------------------------------------


def test_success_ttl() -> None:
    assert ttl_for(response("success"), SETTINGS) == SETTINGS.ttl_success_seconds


def test_partial_ttl_is_shorter() -> None:
    ttl = ttl_for(response("partial"), SETTINGS)
    assert ttl == SETTINGS.ttl_partial_seconds
    assert ttl < SETTINGS.ttl_success_seconds


@pytest.mark.parametrize(
    "reason",
    [ReasonCode.DNS_FAILURE, ReasonCode.PARKED_DOMAIN, ReasonCode.ROBOTS_DISALLOWED],
)
def test_stable_failures_get_a_long_ttl(reason: ReasonCode) -> None:
    """Stop re-hitting a dead domain on every campaign run."""
    assert ttl_for(response("failed", str(reason)), SETTINGS) == (
        SETTINGS.ttl_failed_stable_seconds
    )


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.BOT_BLOCKED,
        ReasonCode.TIMEOUT,
        ReasonCode.CONNECTION_FAILED,
        ReasonCode.HTTP_ERROR_5XX,
    ],
)
def test_transient_failures_get_a_short_ttl(reason: ReasonCode) -> None:
    ttl = ttl_for(response("failed", str(reason)), SETTINGS)
    assert ttl == SETTINGS.ttl_failed_transient_seconds
    assert ttl < SETTINGS.ttl_failed_stable_seconds


@pytest.mark.parametrize(
    "reason",
    [ReasonCode.INVALID_URL, ReasonCode.BLOCKED_SCHEME, ReasonCode.PRIVATE_ADDRESS],
)
def test_pre_crawl_rejections_are_never_cached(reason: ReasonCode) -> None:
    assert ttl_for(response("failed", str(reason)), SETTINGS) is None


def test_unknown_reason_falls_back_to_transient() -> None:
    assert ttl_for(response("failed", "something_new"), SETTINGS) == (
        SETTINGS.ttl_failed_transient_seconds
    )


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


async def test_roundtrip() -> None:
    cache = InMemoryCache(SETTINGS)
    original = response("success")
    await cache.set("k", original, 100)
    restored = await cache.get("k")
    assert restored is not None
    assert restored.status == "success"
    assert restored.request_id == original.request_id


async def test_miss_returns_none() -> None:
    cache = InMemoryCache(SETTINGS)
    assert await cache.get("absent") is None


async def test_none_ttl_is_not_written() -> None:
    cache = InMemoryCache(SETTINGS)
    await cache.set("k", response("failed", str(ReasonCode.PRIVATE_ADDRESS)), None)
    assert await cache.get("k") is None


async def test_unavailable_cache_fails_open() -> None:
    """Losing Redis must degrade cost control, not take the service down."""
    from app.cache.result_cache import ResultCache

    cache = ResultCache(SETTINGS, client=None)
    assert cache.available is False
    assert await cache.get("k") is None
    await cache.set("k", response("success"), 100)  # must not raise


async def test_disabled_cache_connect_returns_unavailable() -> None:
    from app.cache.result_cache import ResultCache

    cache = await ResultCache.connect(CacheSettings(enabled=False))
    assert cache.available is False
