"""Guard tests: the component where a bug is a vulnerability, not a defect.

Covers SS12 in full: scheme/port allowlists, credentials, IP literals and their
shorthand encodings, hostname blocklists, private and IPv4-mapped addresses,
DNS rebinding, and pinned resolution.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.security.ip_rules import IpRules, is_blocked_hostname, parse_ip_literal
from app.security.reasons import ReasonCode
from app.security.resolver import SequenceResolver, StaticResolver
from app.security.transport import PinRegistry, assert_pinning_supported
from app.security.url_guard import (
    CrawlScope,
    GuardError,
    GuardPolicy,
    UrlGuard,
    coerce_input_url,
)

PUBLIC = ["93.184.216.34"]


def make_guard(mapping: dict[str, list[str]] | None = None, **policy_kwargs) -> UrlGuard:
    resolver = StaticResolver(mapping or {"example.com": PUBLIC, "www.example.com": PUBLIC})
    return UrlGuard(GuardPolicy(**policy_kwargs), resolver)


# ---------------------------------------------------------------------------
# Input coercion and normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        ("  example.com  ", "https://example.com"),
        ("http://example.com", "http://example.com"),
        ("//example.com", "https://example.com"),
        ("HTTPS://Example.com", "HTTPS://Example.com"),
    ],
)
def test_coerce_input_url_adds_scheme_only(raw: str, expected: str) -> None:
    assert coerce_input_url(raw) == expected


def test_coerce_input_url_rejects_empty() -> None:
    with pytest.raises(GuardError) as exc:
        coerce_input_url("   ")
    assert exc.value.reason is ReasonCode.INVALID_URL


async def test_validate_normalizes_host_and_default_port() -> None:
    guard = make_guard()
    target = await guard.validate("HTTP://Example.COM:80/About")
    assert target.url == "http://example.com/About"
    assert target.host == "example.com"
    assert target.port == 80
    assert target.registrable_domain == "example.com"


async def test_validate_preserves_query_but_not_fragment() -> None:
    guard = make_guard()
    target = await guard.validate("https://example.com/a?b=1#frag")
    assert target.url == "https://example.com/a?b=1"


async def test_validate_idna_encodes_unicode_hosts() -> None:
    guard = make_guard({"xn--bcher-kva.example.com": PUBLIC})
    target = await guard.validate("https://bücher.example.com/")
    assert target.host == "xn--bcher-kva.example.com"


# ---------------------------------------------------------------------------
# Schemes, ports, credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/html,<h1>hi</h1>",
        "javascript:alert(1)",
        "dict://example.com:11211/",
        "ldap://example.com/",
    ],
)
async def test_disallowed_schemes_rejected(url: str) -> None:
    guard = make_guard()
    with pytest.raises(GuardError) as exc:
        await guard.validate(url)
    assert exc.value.reason is ReasonCode.BLOCKED_SCHEME


@pytest.mark.parametrize("port", [22, 25, 3306, 6379, 8080, 8443, 11211])
async def test_disallowed_ports_rejected(port: int) -> None:
    guard = make_guard()
    with pytest.raises(GuardError) as exc:
        await guard.validate(f"http://example.com:{port}/")
    assert exc.value.reason is ReasonCode.BLOCKED_SCHEME


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@example.com/",
        "http://user@example.com/",
        "https://admin:@example.com/",
        "https://example.com@evil.example.com/",
    ],
)
async def test_credentials_in_url_rejected(url: str) -> None:
    guard = make_guard({"example.com": PUBLIC, "evil.example.com": PUBLIC})
    with pytest.raises(GuardError) as exc:
        await guard.validate(url)
    assert exc.value.reason is ReasonCode.INVALID_URL


# ---------------------------------------------------------------------------
# SSRF: private, loopback, link-local, metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.53.0.9",
        "10.0.0.1",
        "10.255.255.254",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # AWS/Azure/DO metadata
        "169.254.1.1",
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "192.0.0.1",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
async def test_private_ipv4_rejected(address: str) -> None:
    guard = make_guard({"attacker.example": [address]})
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://attacker.example/")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS


@pytest.mark.parametrize(
    "address",
    [
        "::1",
        "::",
        "fc00::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "2002::1",  # 6to4
        "64:ff9b::1",  # NAT64
    ],
)
async def test_private_ipv6_rejected(address: str) -> None:
    guard = make_guard({"attacker.example": [address]})
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://attacker.example/")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS


@pytest.mark.parametrize(
    "address",
    ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:192.168.0.1"],
)
async def test_ipv4_mapped_ipv6_is_unwrapped_and_rejected(address: str) -> None:
    """The commonly missed bypass: a private v4 address wearing a v6 costume."""
    guard = make_guard({"attacker.example": [address]})
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://attacker.example/")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS


def test_ipv4_mapped_normalization_is_explicit() -> None:
    mapped = ipaddress.ip_address("::ffff:127.0.0.1")
    assert IpRules.normalize(mapped) == ipaddress.ip_address("127.0.0.1")
    assert IpRules().is_blocked(mapped)


async def test_ipv4_mapped_public_address_is_allowed() -> None:
    guard = make_guard({"ok.example": ["::ffff:93.184.216.34"]})
    target = await guard.validate("https://ok.example/")
    assert str(target.pinned_address) == "93.184.216.34"


async def test_mixed_public_and_private_records_rejected() -> None:
    """Filtering the private record would let the attacker pick the address."""
    guard = make_guard({"attacker.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://attacker.example/")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "foo.localhost",
        "printer.local",
        "svc.internal",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    ],
)
async def test_blocked_hostnames_rejected_before_dns(host: str) -> None:
    guard = make_guard({host: PUBLIC})  # even if DNS would say otherwise
    with pytest.raises(GuardError) as exc:
        await guard.validate(f"http://{host}/")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS
    assert is_blocked_hostname(host)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://2130706433/",  # decimal
        "http://0x7f000001/",  # hex
        "http://127.1/",  # shorthand
        "http://0177.0.0.1/",
    ],
)
async def test_ip_literals_and_shorthand_encodings_rejected(url: str) -> None:
    guard = make_guard()
    with pytest.raises(GuardError) as exc:
        await guard.validate(url)
    assert exc.value.reason is ReasonCode.INVALID_URL


def test_parse_ip_literal_handles_brackets() -> None:
    assert parse_ip_literal("[::1]") == ipaddress.ip_address("::1")
    assert parse_ip_literal("example.com") is None


async def test_dns_failure_is_typed() -> None:
    guard = make_guard({})
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://nowhere.example/")
    assert exc.value.reason is ReasonCode.DNS_FAILURE


# ---------------------------------------------------------------------------
# DNS rebinding / pinned resolution
# ---------------------------------------------------------------------------


async def test_dns_rebinding_does_not_change_the_pinned_address() -> None:
    """First lookup is public, a second would be loopback.

    The guard resolves once and pins the validated address; the transport never
    performs a second lookup, so the rebind has nothing to act on.
    """
    resolver = SequenceResolver({"rebind.example": [["93.184.216.34"], ["127.0.0.1"]]})
    guard = UrlGuard(GuardPolicy(), resolver)

    target = await guard.validate("https://rebind.example/")
    assert str(target.pinned_address) == "93.184.216.34"

    registry = PinRegistry()
    registry.pin(target.host, target.port, target.pinned_address)
    assert registry.resolve("rebind.example", 443) == "93.184.216.34"

    # A later validation sees the rebound answer and refuses it outright.
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://rebind.example/second")
    assert exc.value.reason is ReasonCode.PRIVATE_ADDRESS


def test_pin_registry_roundtrip_and_ipv6_bracketing() -> None:
    registry = PinRegistry()
    registry.pin("example.com", 443, ipaddress.ip_address("93.184.216.34"))
    registry.pin("v6.example.com", 443, ipaddress.ip_address("2606:2800:220:1::1"))
    assert registry.resolve("EXAMPLE.COM", 443) == "93.184.216.34"
    assert registry.resolve("v6.example.com", 443) == "[2606:2800:220:1::1]"
    assert registry.resolve("example.com", 80) is None


def test_pinning_contract_still_holds_for_installed_httpcore() -> None:
    """Guards against a silent httpcore upgrade breaking DNS pinning."""
    assert_pinning_supported()


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


async def test_scope_rejects_other_registrable_domain() -> None:
    guard = make_guard({"example.com": PUBLIC, "other.com": PUBLIC})
    scope = CrawlScope(
        registrable_domain="example.com",
        seed_host="example.com",
        seed_url="https://example.com/",
    )
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://other.com/", scope=scope)
    assert exc.value.reason is ReasonCode.OUT_OF_SCOPE


async def test_scope_allows_subdomains_of_the_same_registrable_domain() -> None:
    guard = make_guard({"solutions.example.com": PUBLIC})
    scope = CrawlScope(
        registrable_domain="example.com",
        seed_host="www.example.com",
        seed_url="https://www.example.com/",
    )
    target = await guard.validate("https://solutions.example.com/x", scope=scope)
    assert target.host == "solutions.example.com"


async def test_lookalike_domain_does_not_satisfy_scope() -> None:
    guard = make_guard({"example.com.evil.com": PUBLIC})
    scope = CrawlScope(
        registrable_domain="example.com",
        seed_host="example.com",
        seed_url="https://example.com/",
    )
    with pytest.raises(GuardError) as exc:
        await guard.validate("https://example.com.evil.com/", scope=scope)
    assert exc.value.reason is ReasonCode.OUT_OF_SCOPE


def test_check_syntax_does_not_touch_dns() -> None:
    """The browser route interceptor needs a fast, DNS-free decision."""
    resolver = StaticResolver({"example.com": PUBLIC})
    guard = UrlGuard(GuardPolicy(), resolver)
    scheme, host, port, path = guard.check_syntax("https://example.com/a?b=1")
    assert (scheme, host, port) == ("https", "example.com", 443)
    assert path == "/a?b=1"
    assert resolver.calls == []


def test_extra_allowed_networks_is_constructor_only() -> None:
    """Loopback is reachable only when a policy explicitly opts in."""
    strict = IpRules()
    permissive = IpRules(extra_allowed_networks=(ipaddress.IPv4Network("127.0.0.0/8"),))
    loopback = ipaddress.ip_address("127.0.0.1")
    assert strict.is_blocked(loopback)
    assert permissive.is_allowed(loopback)
    assert GuardPolicy().extra_allowed_networks == ()
