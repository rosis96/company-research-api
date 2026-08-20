"""HTTP fetcher tests.

Covers SS4 and SS12: manual redirect walking with per-hop revalidation, the
cross-domain homepage rebase, internal-page cross-domain rejection, streamed
byte caps, content-type gating and typed failures.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.fetch.http import HTML_TYPES, TEXT_TYPES, HttpFetcher
from app.models.internal import Deadline
from app.security.reasons import ReasonCode
from app.security.resolver import StaticResolver
from app.security.transport import PinRegistry
from app.security.url_guard import CrawlScope, GuardPolicy, UrlGuard
from tests.fixtures.server import FixtureServer, Route, SiteSpec

LOOPBACK = (ipaddress.IPv4Network("127.0.0.0/8"),)
GOOD_HTML = "<html><head><title>T</title></head><body><main><p>" + ("word " * 300) + "</p></main></body></html>"


@pytest.fixture
def probe_site():
    spec = SiteSpec(
        host="probe.com",
        routes={
            "/": Route(body=GOOD_HTML),
            "/redirect-relative": Route(redirect_to="/", status=301),
            "/redirect-absolute": Route(redirect_to="http://probe.com/", status=302),
            "/redirect-loop": Route(redirect_to="/redirect-loop", status=302),
            "/redirect-chain-1": Route(redirect_to="/redirect-chain-2", status=302),
            "/redirect-chain-2": Route(redirect_to="/redirect-chain-3", status=302),
            "/redirect-chain-3": Route(redirect_to="/redirect-chain-4", status=302),
            "/redirect-chain-4": Route(redirect_to="/redirect-chain-5", status=302),
            "/redirect-chain-5": Route(redirect_to="/redirect-chain-6", status=302),
            "/redirect-chain-6": Route(redirect_to="/redirect-chain-7", status=302),
            "/redirect-chain-7": Route(body=GOOD_HTML),
            "/redirect-no-location": Route(body="", status=302, headers={}),
            "/huge": Route(body="x" * 5_000_000),
            "/gzip-bomb": Route(body="A" * 8_000_000, gzip_body=True),
            "/pdf": Route(body="%PDF-1.4", content_type="application/pdf"),
            "/image": Route(body="\x89PNG", content_type="image/png"),
            "/json": Route(body="{}", content_type="application/json"),
            "/plain": Route(body="User-agent: *", content_type="text/plain"),
            "/latin1": Route(
                body="<html><body><p>café</p></body></html>".encode("latin-1"),
                content_type="text/html; charset=iso-8859-1",
            ),
            "/meta-charset": Route(
                body=(
                    "<html><head><meta charset='iso-8859-1'></head>"
                    "<body><p>café</p></body></html>"
                ).encode("latin-1"),
                content_type="text/html",
            ),
            "/404": Route(body="nope", status=404),
            "/403": Route(body="denied", status=403),
            "/429": Route(body="slow down", status=429),
            "/500": Route(body="boom", status=500),
            "/slow": Route(body=GOOD_HTML, delay_s=3.0),
        },
    )
    server = FixtureServer(spec).start()
    # The absolute-redirect target needs the port, which is only known after bind.
    spec.routes["/redirect-absolute"].redirect_to = f"http://probe.com:{server.port}/"
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def offsite():
    spec = SiteSpec(host="offsite.com", routes={"/": Route(body=GOOD_HTML)})
    server = FixtureServer(spec).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def probe_fetcher(probe_site, offsite, budget):
    ports = frozenset({80, 443, probe_site.port, offsite.port})
    resolver = StaticResolver({"probe.com": ["127.0.0.1"], "offsite.com": ["127.0.0.1"]})
    guard = UrlGuard(
        GuardPolicy(allowed_ports=ports, extra_allowed_networks=LOOPBACK, max_redirect_hops=5),
        resolver,
    )
    return HttpFetcher(guard, user_agent="TestBot/1.0", budget=budget, max_redirect_hops=5)


@pytest.fixture
def deadline():
    return Deadline(total_ms=30000)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


async def test_successful_fetch(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(probe_site.url("/"), deadline=deadline, scope=None)
    assert outcome.ok
    assert outcome.result.status_code == 200
    assert "word" in outcome.result.html
    assert outcome.result.redirect_chain == [probe_site.url("/")]
    await probe_fetcher.aclose()


async def test_automatic_redirect_following_is_disabled(probe_fetcher) -> None:
    """The httpx client must never follow redirects on its own."""
    assert probe_fetcher._client.follow_redirects is False
    await probe_fetcher.aclose()


@pytest.mark.parametrize("path", ["/redirect-relative", "/redirect-absolute"])
async def test_redirects_are_walked_manually(probe_fetcher, probe_site, deadline, path) -> None:
    outcome = await probe_fetcher.fetch(probe_site.url(path), deadline=deadline, scope=None)
    assert outcome.ok
    assert len(outcome.result.redirect_chain) == 2
    assert outcome.result.redirect_chain[0].endswith(path)
    await probe_fetcher.aclose()


async def test_redirect_loop_detected(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/redirect-loop"), deadline=deadline, scope=None
    )
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.TOO_MANY_REDIRECTS
    await probe_fetcher.aclose()


async def test_redirect_hop_limit_enforced(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/redirect-chain-1"), deadline=deadline, scope=None
    )
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.TOO_MANY_REDIRECTS
    await probe_fetcher.aclose()


async def test_redirect_without_location_is_an_error(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/redirect-no-location"), deadline=deadline, scope=None
    )
    assert not outcome.ok
    await probe_fetcher.aclose()


# ---------------------------------------------------------------------------
# Redirect revalidation and scope
# ---------------------------------------------------------------------------


async def test_every_redirect_hop_is_revalidated(probe_site, offsite, budget, deadline) -> None:
    """Each hop must re-resolve; the resolver call log proves it."""
    ports = frozenset({80, 443, probe_site.port, offsite.port})
    resolver = StaticResolver({"probe.com": ["127.0.0.1"], "offsite.com": ["127.0.0.1"]})
    guard = UrlGuard(
        GuardPolicy(allowed_ports=ports, extra_allowed_networks=LOOPBACK), resolver
    )
    fetcher = HttpFetcher(guard, user_agent="TestBot/1.0", budget=budget)

    await fetcher.fetch(probe_site.url("/redirect-relative"), deadline=deadline, scope=None)
    assert resolver.calls.count("probe.com") == 2  # once per hop
    await fetcher.aclose()


async def test_internal_page_cross_domain_redirect_is_rejected(
    probe_site, offsite, budget, deadline
) -> None:
    """An off-scope redirect on an internal page drops the page.

    This is a data-integrity requirement as much as a security one: following it
    would inject another company's marketing copy into the research payload.
    """
    probe_site.spec.routes["/partners"] = Route(
        redirect_to=f"http://offsite.com:{offsite.port}/", status=302
    )
    ports = frozenset({80, 443, probe_site.port, offsite.port})
    resolver = StaticResolver({"probe.com": ["127.0.0.1"], "offsite.com": ["127.0.0.1"]})
    guard = UrlGuard(
        GuardPolicy(allowed_ports=ports, extra_allowed_networks=LOOPBACK), resolver
    )
    fetcher = HttpFetcher(guard, user_agent="TestBot/1.0", budget=budget)

    scope = CrawlScope(
        registrable_domain="probe.com", seed_host="probe.com", seed_url=probe_site.url("/")
    )
    offsite.clear_requests()
    outcome = await fetcher.fetch(probe_site.url("/partners"), deadline=deadline, scope=scope)

    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.OUT_OF_SCOPE
    assert outcome.failure.url == probe_site.url("/partners")  # the page we asked for
    assert offsite.requests == []  # never contacted
    await fetcher.aclose()


async def test_homepage_cross_domain_redirect_is_allowed_for_rebase(
    probe_site, offsite, budget, deadline
) -> None:
    """A rebrand redirect on the seed is first-party evidence, so it is followed."""
    probe_site.spec.routes["/rebrand"] = Route(
        redirect_to=f"http://offsite.com:{offsite.port}/", status=301
    )
    ports = frozenset({80, 443, probe_site.port, offsite.port})
    resolver = StaticResolver({"probe.com": ["127.0.0.1"], "offsite.com": ["127.0.0.1"]})
    guard = UrlGuard(
        GuardPolicy(allowed_ports=ports, extra_allowed_networks=LOOPBACK), resolver
    )
    fetcher = HttpFetcher(guard, user_agent="TestBot/1.0", budget=budget)

    outcome = await fetcher.fetch(
        probe_site.url("/rebrand"), deadline=deadline, scope=None, allow_domain_rebase=True
    )
    assert outcome.ok
    assert "offsite.com" in outcome.result.final_url
    await fetcher.aclose()


async def test_redirect_to_private_address_is_rejected(probe_site, budget, deadline) -> None:
    probe_site.spec.routes["/ssrf"] = Route(redirect_to="http://internal.example/", status=302)
    resolver = StaticResolver(
        {"probe.com": ["127.0.0.1"], "internal.example": ["10.0.0.5"]}
    )
    guard = UrlGuard(
        GuardPolicy(
            allowed_ports=frozenset({80, 443, probe_site.port}),
            extra_allowed_networks=LOOPBACK,
        ),
        resolver,
    )
    fetcher = HttpFetcher(guard, user_agent="TestBot/1.0", budget=budget)

    outcome = await fetcher.fetch(probe_site.url("/ssrf"), deadline=deadline, scope=None)
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.PRIVATE_ADDRESS
    await fetcher.aclose()


async def test_transport_refuses_unpinned_hosts() -> None:
    """The transport fails closed rather than falling back to system DNS."""
    from app.security.transport import PinnedBackend

    backend = PinnedBackend(PinRegistry(), strict=True)
    with pytest.raises(Exception) as exc:
        await backend.connect_tcp("never-validated.example", 443)
    assert "unvalidated" in str(exc.value)


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


async def test_streamed_byte_cap_enforced(probe_fetcher, probe_site, deadline, budget) -> None:
    outcome = await probe_fetcher.fetch(probe_site.url("/huge"), deadline=deadline, scope=None)
    assert outcome.ok
    assert outcome.result.truncated_bytes is True
    assert len(outcome.result.html.encode()) <= budget.max_response_bytes
    await probe_fetcher.aclose()


async def test_decompressed_size_is_capped(probe_fetcher, probe_site, deadline, budget) -> None:
    """The cap applies after decompression, so it is a gzip-bomb guard too."""
    outcome = await probe_fetcher.fetch(
        probe_site.url("/gzip-bomb"), deadline=deadline, scope=None
    )
    assert outcome.ok
    assert outcome.result.truncated_bytes is True
    assert len(outcome.result.html) <= budget.max_response_bytes
    await probe_fetcher.aclose()


async def test_explicit_byte_cap_override(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/huge"), deadline=deadline, scope=None, max_bytes=4096
    )
    assert outcome.ok
    assert len(outcome.result.html) <= 4096
    await probe_fetcher.aclose()


@pytest.mark.parametrize("path", ["/pdf", "/image", "/json", "/plain"])
async def test_non_html_content_types_rejected(probe_fetcher, probe_site, deadline, path) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url(path), deadline=deadline, scope=None, accept_types=HTML_TYPES
    )
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.NON_HTML_CONTENT
    await probe_fetcher.aclose()


async def test_text_types_accepted_for_robots_and_sitemaps(
    probe_fetcher, probe_site, deadline
) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/plain"), deadline=deadline, scope=None, accept_types=TEXT_TYPES
    )
    assert outcome.ok
    assert "User-agent" in outcome.result.html
    await probe_fetcher.aclose()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/404", ReasonCode.HTTP_ERROR_4XX),
        ("/403", ReasonCode.BOT_BLOCKED),
        ("/429", ReasonCode.BOT_BLOCKED),
        ("/500", ReasonCode.HTTP_ERROR_5XX),
    ],
)
async def test_status_codes_map_to_typed_reasons(
    probe_fetcher, probe_site, deadline, path, reason
) -> None:
    outcome = await probe_fetcher.fetch(probe_site.url(path), deadline=deadline, scope=None)
    assert not outcome.ok
    assert outcome.failure.reason is reason
    await probe_fetcher.aclose()


@pytest.mark.parametrize("path", ["/latin1", "/meta-charset"])
async def test_charset_decoding(probe_fetcher, probe_site, deadline, path) -> None:
    outcome = await probe_fetcher.fetch(probe_site.url(path), deadline=deadline, scope=None)
    assert outcome.ok
    assert "café" in outcome.result.html
    await probe_fetcher.aclose()


async def test_per_request_timeout(probe_fetcher, probe_site, deadline) -> None:
    outcome = await probe_fetcher.fetch(
        probe_site.url("/slow"), deadline=deadline, scope=None, total_timeout_s=0.8
    )
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.TIMEOUT
    await probe_fetcher.aclose()


async def test_expired_deadline_short_circuits(probe_fetcher, probe_site) -> None:
    expired = Deadline(total_ms=0)
    outcome = await probe_fetcher.fetch(probe_site.url("/"), deadline=expired, scope=None)
    assert not outcome.ok
    assert outcome.failure.reason is ReasonCode.DEADLINE_EXCEEDED
    await probe_fetcher.aclose()


async def test_identifiable_user_agent_is_sent(probe_fetcher, probe_site, deadline) -> None:
    assert "Bot" in probe_fetcher._client.headers["User-Agent"]
    await probe_fetcher.aclose()
