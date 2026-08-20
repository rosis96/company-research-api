"""Shared test fixtures.

Fixture sites run as real HTTP servers on loopback.  Hostnames resolve through a
:class:`~app.security.resolver.StaticResolver`, and loopback is permitted only
by passing ``extra_allowed_networks`` to the guard *policy* -- a constructor
argument the production wiring never sets, so the guard itself is not weakened.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import pytest

from app.config.loader import get_limits
from app.config.settings import RendererSettings, RobotsSettings
from app.crawl.orchestrator import ResearchOrchestrator
from app.crawl.robots import RobotsPolicy
from app.fetch.http import HttpFetcher
from app.fetch.renderer import NullRenderer
from app.security.resolver import StaticResolver
from app.security.url_guard import GuardPolicy, UrlGuard
from tests.fixtures.server import FixtureServer
from tests.fixtures.sites import ALL_SITES

LOOPBACK = (ipaddress.IPv4Network("127.0.0.0/8"),)
TEST_USER_AGENT = "CompanyResearchBot/1.0 (+https://example.test/bot)"


@pytest.fixture(scope="session")
def fixture_servers() -> Iterator[dict[str, FixtureServer]]:
    """Start every fixture site once for the whole session."""
    servers = {name: FixtureServer(spec).start() for name, spec in ALL_SITES.items()}

    # Cross-domain fixtures need the real port of the target site, which is only
    # known after binding.
    newbrand = servers["newbrand"]
    for name in ("oldbrand", "leaky"):
        for route in servers[name].spec.routes.values():
            if route.redirect_to and "__NEWBRAND__" in route.redirect_to:
                route.redirect_to = route.redirect_to.replace(
                    "__NEWBRAND__", f"http://{newbrand.host}:{newbrand.port}"
                )
    try:
        yield servers
    finally:
        for server in servers.values():
            server.stop()


@pytest.fixture(scope="session")
def resolver(fixture_servers: dict[str, FixtureServer]) -> StaticResolver:
    mapping = {server.host: ["127.0.0.1"] for server in fixture_servers.values()}
    mapping["public.test"] = ["93.184.216.34"]
    return StaticResolver(mapping)


@pytest.fixture(scope="session")
def allowed_ports(fixture_servers: dict[str, FixtureServer]) -> frozenset[int]:
    return frozenset({80, 443} | {server.port for server in fixture_servers.values()})


@pytest.fixture
def guard_policy(allowed_ports: frozenset[int]) -> GuardPolicy:
    return GuardPolicy(
        allowed_ports=allowed_ports,
        extra_allowed_networks=LOOPBACK,
        max_redirect_hops=5,
    )


@pytest.fixture
def guard(guard_policy: GuardPolicy, resolver: StaticResolver) -> UrlGuard:
    return UrlGuard(guard_policy, resolver)


@pytest.fixture
def budget():
    return get_limits().budget("standard")


@pytest.fixture
async def fetcher(guard: UrlGuard, budget):
    client = HttpFetcher(
        guard, user_agent=TEST_USER_AGENT, budget=budget, max_redirect_hops=5
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def robots_settings() -> RobotsSettings:
    return RobotsSettings()


@pytest.fixture
def robots(robots_settings: RobotsSettings, fetcher: HttpFetcher) -> RobotsPolicy:
    return RobotsPolicy(robots_settings, fetcher)


@pytest.fixture
def orchestrator(
    guard: UrlGuard, fetcher: HttpFetcher, robots: RobotsPolicy
) -> ResearchOrchestrator:
    return ResearchOrchestrator(guard, fetcher, NullRenderer(), robots)


@pytest.fixture
def renderer_settings() -> RendererSettings:
    return RendererSettings()


def make_orchestrator(
    guard: UrlGuard,
    fetcher: HttpFetcher,
    *,
    renderer=None,
    robots_settings: RobotsSettings | None = None,
) -> ResearchOrchestrator:
    """Build an orchestrator with a specific robots policy or renderer."""
    policy = RobotsPolicy(robots_settings or RobotsSettings(), fetcher)
    return ResearchOrchestrator(guard, fetcher, renderer or NullRenderer(), policy)
