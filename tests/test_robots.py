"""robots.txt parsing and policy tests (SS6).

The policy must be entirely configuration-driven: every knob in
``RobotsSettings`` is exercised here.
"""

from __future__ import annotations

import pytest

from app.config.settings import RobotsSettings
from app.crawl.robots import RobotsPolicy, parse_robots
from app.models.internal import Deadline
from app.security.reasons import ReasonCode

AGENT = "CompanyResearchBot"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_allow_all() -> None:
    robots = parse_robots("User-agent: *\nAllow: /\n", AGENT)
    assert robots.allows("/anything")
    assert not robots.blanket_disallow


def test_parse_blanket_disallow() -> None:
    robots = parse_robots("User-agent: *\nDisallow: /\n", AGENT)
    assert robots.blanket_disallow
    assert not robots.allows("/anything")


def test_empty_disallow_means_allow_everything() -> None:
    robots = parse_robots("User-agent: *\nDisallow:\n", AGENT)
    assert robots.allows("/anything")
    assert not robots.blanket_disallow


def test_longest_match_wins() -> None:
    robots = parse_robots(
        "User-agent: *\nDisallow: /private\nAllow: /private/public\n", AGENT
    )
    assert not robots.allows("/private/secret")
    assert robots.allows("/private/public/page")


def test_allow_beats_disallow_at_equal_specificity() -> None:
    robots = parse_robots("User-agent: *\nDisallow: /x\nAllow: /x\n", AGENT)
    assert robots.allows("/x/y")


def test_wildcard_patterns() -> None:
    robots = parse_robots("User-agent: *\nDisallow: /*/private\n", AGENT)
    assert not robots.allows("/a/private")
    assert not robots.allows("/b/private/deep")
    assert robots.allows("/a/public")


def test_dollar_anchor() -> None:
    robots = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", AGENT)
    assert not robots.allows("/files/report.pdf")
    assert robots.allows("/files/report.pdf.html")


def test_agent_specific_group_preferred_over_wildcard() -> None:
    text = (
        "User-agent: *\nDisallow: /\n\n"
        f"User-agent: {AGENT}\nAllow: /\nDisallow: /secret\n"
    )
    robots = parse_robots(text, AGENT)
    assert robots.allows("/services")
    assert not robots.allows("/secret/x")


def test_crawl_delay_parsed() -> None:
    robots = parse_robots("User-agent: *\nCrawl-delay: 3.5\nAllow: /\n", AGENT)
    assert robots.crawl_delay_s == pytest.approx(3.5)


def test_sitemaps_collected() -> None:
    text = (
        "Sitemap: https://e.com/sitemap.xml\n"
        "User-agent: *\nAllow: /\n"
        "Sitemap: https://e.com/sitemap-2.xml\n"
    )
    robots = parse_robots(text, AGENT)
    assert robots.sitemaps == ["https://e.com/sitemap.xml", "https://e.com/sitemap-2.xml"]


def test_comments_and_blank_lines_ignored() -> None:
    text = "# comment\n\nUser-agent: *   # trailing\nDisallow: /x  # why\n"
    robots = parse_robots(text, AGENT)
    assert not robots.allows("/x")


def test_malformed_robots_does_not_raise() -> None:
    robots = parse_robots("<<<not robots>>>\n\x00\x01", AGENT)
    assert robots.allows("/anything")


def test_multiple_agents_share_one_group() -> None:
    text = "User-agent: Googlebot\nUser-agent: *\nDisallow: /x\n"
    robots = parse_robots(text, AGENT)
    assert not robots.allows("/x")


# ---------------------------------------------------------------------------
# Policy against live fixture sites
# ---------------------------------------------------------------------------


async def test_blanket_disallow_fails_the_site(fetcher, fixture_servers) -> None:
    server = fixture_servers["blocked"]
    policy = RobotsPolicy(RobotsSettings(on_blanket_disallow="fail"), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    allowed, reason = policy.is_allowed(server.url("/"), is_seed=True)
    assert not allowed
    assert reason is ReasonCode.ROBOTS_DISALLOWED


async def test_blanket_disallow_can_be_configured_to_proceed(
    fetcher, fixture_servers
) -> None:
    server = fixture_servers["blocked"]
    policy = RobotsPolicy(RobotsSettings(on_blanket_disallow="proceed"), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    allowed, _ = policy.is_allowed(server.url("/"), is_seed=True)
    assert allowed


async def test_robots_can_be_disabled_entirely(fetcher, fixture_servers) -> None:
    server = fixture_servers["blocked"]
    policy = RobotsPolicy(RobotsSettings(enabled=False), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    allowed, _ = policy.is_allowed(server.url("/services"))
    assert allowed
    assert not policy.enabled


async def test_apply_to_seed_url_false_exempts_the_seed(fetcher, fixture_servers) -> None:
    """The most likely future policy change gets its own flag."""
    server = fixture_servers["blocked"]
    policy = RobotsPolicy(RobotsSettings(apply_to_seed_url=False), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))

    assert policy.is_allowed(server.url("/"), is_seed=True)[0] is True
    assert policy.is_allowed(server.url("/services"), is_seed=False)[0] is False


async def test_partial_disallow_blocks_only_the_named_path(
    fetcher, fixture_servers
) -> None:
    server = fixture_servers["partial_robots"]
    policy = RobotsPolicy(RobotsSettings(), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))

    assert policy.is_allowed(server.url("/services"))[0]
    assert policy.is_allowed(server.url("/about"))[0]
    assert not policy.is_allowed(server.url("/pricing"))[0]


async def test_crawl_delay_is_capped_by_configuration(fetcher, fixture_servers) -> None:
    server = fixture_servers["partial_robots"]
    policy = RobotsPolicy(RobotsSettings(max_crawl_delay_ms=400), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    # The site declares Crawl-delay: 1, the cap is 0.4s.
    assert policy.crawl_delay_s == pytest.approx(0.4)


async def test_crawl_delay_can_be_ignored(fetcher, fixture_servers) -> None:
    server = fixture_servers["partial_robots"]
    policy = RobotsPolicy(RobotsSettings(respect_crawl_delay=False), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    assert policy.crawl_delay_s == 0.0


async def test_unreachable_robots_proceeds_by_default(fetcher) -> None:
    policy = RobotsPolicy(RobotsSettings(on_fetch_error="proceed"), fetcher)
    await policy.load("http://unresolvable.example", Deadline(total_ms=3000))
    assert policy.is_allowed("http://unresolvable.example/x")[0] is True


async def test_unreachable_robots_can_fail_closed(fetcher) -> None:
    policy = RobotsPolicy(RobotsSettings(on_fetch_error="fail"), fetcher)
    await policy.load("http://unresolvable.example", Deadline(total_ms=3000))
    allowed, reason = policy.is_allowed("http://unresolvable.example/x")
    assert not allowed
    assert reason is ReasonCode.ROBOTS_DISALLOWED


async def test_robots_is_cached_per_origin(fetcher, fixture_servers) -> None:
    server = fixture_servers["static"]
    policy = RobotsPolicy(RobotsSettings(), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    server.clear_requests()
    await policy.load(server.origin, Deadline(total_ms=10000))
    assert "/robots.txt" not in server.requests


async def test_sitemap_declarations_are_exposed(fetcher, fixture_servers) -> None:
    server = fixture_servers["static"]
    policy = RobotsPolicy(RobotsSettings(), fetcher)
    await policy.load(server.origin, Deadline(total_ms=10000))
    assert policy.sitemaps() == []  # this fixture declares none
