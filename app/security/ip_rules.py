"""IP address and hostname blocklists for SSRF protection.

Architecture reference: SS12, "Layer 1 - application-level validation".

The rules are deliberately expressed as explicit network lists rather than
relying solely on :mod:`ipaddress` helper properties, because the helpers do
not cover everything we care about (CGNAT, 6to4, NAT64, benchmarking ranges)
and because an explicit list is auditable.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _v4(cidr: str) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(cidr)


def _v6(cidr: str) -> ipaddress.IPv6Network:
    return ipaddress.IPv6Network(cidr)


#: IPv4 ranges that must never be reachable from the fetcher.
BLOCKED_IPV4: tuple[ipaddress.IPv4Network, ...] = (
    _v4("0.0.0.0/8"),  # "this network"
    _v4("10.0.0.0/8"),  # RFC1918 private
    _v4("100.64.0.0/10"),  # CGNAT
    _v4("127.0.0.0/8"),  # loopback
    _v4("169.254.0.0/16"),  # link-local - cloud metadata lives at 169.254.169.254
    _v4("172.16.0.0/12"),  # RFC1918 private
    _v4("192.0.0.0/24"),  # IETF protocol assignments
    _v4("192.0.2.0/24"),  # TEST-NET-1
    _v4("192.88.99.0/24"),  # 6to4 relay anycast
    _v4("192.168.0.0/16"),  # RFC1918 private
    _v4("198.18.0.0/15"),  # benchmarking
    _v4("198.51.100.0/24"),  # TEST-NET-2
    _v4("203.0.113.0/24"),  # TEST-NET-3
    _v4("224.0.0.0/4"),  # multicast
    _v4("240.0.0.0/4"),  # reserved (includes 255.255.255.255)
)

#: IPv6 ranges that must never be reachable from the fetcher.
BLOCKED_IPV6: tuple[ipaddress.IPv6Network, ...] = (
    _v6("::/128"),  # unspecified
    _v6("::1/128"),  # loopback
    _v6("fc00::/7"),  # unique local addresses
    _v6("fe80::/10"),  # link-local
    _v6("ff00::/8"),  # multicast
    _v6("2001:db8::/32"),  # documentation
    _v6("2002::/16"),  # 6to4
    _v6("64:ff9b::/96"),  # NAT64
    _v6("100::/64"),  # discard-only
)

#: IPv4-mapped IPv6 space.  Addresses here are unwrapped and re-checked against
#: the IPv4 list rather than being blanket-blocked -- a commonly missed bypass.
IPV4_MAPPED_V6 = _v6("::ffff:0:0/96")

#: Hostnames that are blocked before any DNS resolution happens.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: Hostname suffixes that are blocked before any DNS resolution happens.
BLOCKED_HOSTNAME_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)


@dataclass(frozen=True, slots=True)
class IpRules:
    """Decides whether a resolved address may be connected to.

    ``extra_allowed_networks`` exists so the test suite can point fixture
    hostnames at ``127.0.0.1`` without weakening the production guard.  It is a
    constructor argument rather than a global setting precisely so that it
    cannot be switched on by an environment variable in production; the API
    layer never passes it.
    """

    extra_allowed_networks: tuple[IPNetwork, ...] = field(default=())

    @staticmethod
    def normalize(address: IPAddress) -> IPAddress:
        """Unwrap IPv4-mapped IPv6 addresses to their IPv4 form."""
        if isinstance(address, ipaddress.IPv6Address):
            if address in IPV4_MAPPED_V6 and address.ipv4_mapped is not None:
                return address.ipv4_mapped
            # ``::ffff:127.0.0.1`` is caught above; also handle the 6to4 and
            # NAT64 embeddings by leaving them to the network list below.
        return address

    def is_allowed(self, address: IPAddress) -> bool:
        """Return ``True`` when this address may be connected to."""
        candidate = self.normalize(address)

        for allowed in self.extra_allowed_networks:
            if candidate.version == allowed.version and candidate in allowed:
                return True

        if isinstance(candidate, ipaddress.IPv4Address):
            return not any(candidate in net for net in BLOCKED_IPV4)

        # Any IPv6 address that survived normalization.
        if candidate in IPV4_MAPPED_V6:
            # Mapped but ``ipv4_mapped`` was None (malformed) -- refuse.
            return False
        return not any(candidate in net for net in BLOCKED_IPV6)

    def is_blocked(self, address: IPAddress) -> bool:
        return not self.is_allowed(address)


def is_blocked_hostname(host: str) -> bool:
    """Hostname-level blocklist, applied before DNS resolution."""
    lowered = host.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES:
        return True
    return lowered.endswith(BLOCKED_HOSTNAME_SUFFIXES)


def parse_ip_literal(host: str) -> IPAddress | None:
    """Return an IP address if ``host`` is an IP literal, else ``None``.

    Handles the bracketed IPv6 form (``[::1]``).  Note that Python's
    :func:`ipaddress.ip_address` rejects the legacy shorthand encodings
    (``127.1``, ``0x7f000001``, ``2130706433``), which is why the guard
    additionally refuses all-numeric hostnames outright.
    """
    stripped = host.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    try:
        return ipaddress.ip_address(stripped)
    except ValueError:
        return None
