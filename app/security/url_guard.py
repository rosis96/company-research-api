"""The URL guard: the single security boundary every outbound fetch passes through.

Architecture reference: SS12 and invariant 4.  Nothing in this codebase opens a
connection to a URL that has not been returned by :meth:`UrlGuard.validate`.

The guard performs, in order:

1. syntactic parsing and scheme/port/credential checks
2. hostname sanity, IDNA encoding, IP-literal and numeric-host rejection
3. hostname blocklist
4. DNS resolution *performed here*, once
5. address validation against :mod:`app.security.ip_rules`
6. crawl-scope enforcement when a scope has been frozen

The validated address is then pinned for the connection by
:mod:`app.security.transport`, closing the rebinding window.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from app.security.domains import registrable_domain
from app.security.ip_rules import (
    IPAddress,
    IPNetwork,
    IpRules,
    is_blocked_hostname,
    parse_ip_literal,
)
from app.security.reasons import ReasonCode
from app.security.resolver import Resolver, SystemResolver

_ALL_DIGITS = re.compile(r"^\d+$")
_HEX_LITERAL = re.compile(r"^0[xX][0-9a-fA-F]+$")
_VALID_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class GuardError(Exception):
    """Raised when a URL must not be fetched."""

    def __init__(self, reason: ReasonCode, detail: str = "") -> None:
        super().__init__(detail or str(reason))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """Tunable guard behaviour.

    ``extra_allowed_networks`` and ``allowed_ports`` are constructor-level
    settings rather than environment variables so that the production wiring in
    :mod:`app.api.deps` cannot accidentally relax them; the test suite passes
    its own policy explicitly.
    """

    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_ports: frozenset[int] = frozenset({80, 443})
    extra_allowed_networks: tuple[IPNetwork, ...] = ()
    allow_ip_literals: bool = False
    max_redirect_hops: int = 5


@dataclass(frozen=True, slots=True)
class CrawlScope:
    """The frozen crawl scope.

    Established exactly once, from the *final* URL of the homepage, and never
    modified afterwards (SS2, SS6).
    """

    registrable_domain: str
    seed_host: str
    seed_url: str

    def contains(self, host: str) -> bool:
        return registrable_domain(host) == self.registrable_domain


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """A URL that has been cleared for fetching."""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    addresses: tuple[IPAddress, ...]
    registrable_domain: str
    #: The address the connection will actually be pinned to.
    pinned_address: IPAddress = field(init=False)

    def __post_init__(self) -> None:  # pragma: no cover - trivial
        object.__setattr__(self, "pinned_address", self.addresses[0])

    @property
    def origin_key(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def coerce_input_url(raw: str) -> str:
    """Apply the minimal fix-ups a user-supplied URL is allowed to receive.

    Only a missing scheme is added.  Everything else is the caller's problem and
    will be rejected by :meth:`UrlGuard.validate`.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise GuardError(ReasonCode.INVALID_URL, "empty url")
    if "://" not in candidate:
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        else:
            candidate = f"https://{candidate}"
    return candidate


def _encode_host(host: str) -> str:
    """Lowercase and IDNA-encode a hostname, rejecting malformed input."""
    stripped = host.strip().rstrip(".")
    if not stripped:
        raise GuardError(ReasonCode.INVALID_URL, "empty host")
    if len(stripped) > 253:
        raise GuardError(ReasonCode.INVALID_URL, "host too long")

    lowered = stripped.lower()
    if not lowered.isascii():
        try:
            lowered = lowered.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError) as exc:
            raise GuardError(ReasonCode.INVALID_URL, f"idna encoding failed: {exc}") from exc

    if lowered.startswith("[") and lowered.endswith("]"):
        return lowered  # IPv6 literal; handled separately by the caller

    labels = lowered.split(".")
    if any(not label for label in labels):
        raise GuardError(ReasonCode.INVALID_URL, "empty label in host")
    for label in labels:
        if not _VALID_LABEL.match(label):
            raise GuardError(ReasonCode.INVALID_URL, f"invalid host label {label!r}")
    return lowered


def _looks_numeric(host: str) -> bool:
    """Detect hostnames that are really encoded IP addresses.

    Catches the shorthand forms Python's :func:`ipaddress.ip_address` refuses to
    parse (``127.1``, ``2130706433``, ``0x7f000001``), which would otherwise
    reach the system resolver and be interpreted as loopback.
    """
    labels = host.split(".")
    if _ALL_DIGITS.match(host) or _HEX_LITERAL.match(host):
        return True
    if len(labels) > 1 and _ALL_DIGITS.match(labels[-1]):
        return True
    return any(_HEX_LITERAL.match(label) for label in labels)


class UrlGuard:
    """Validates every URL before a connection is opened."""

    def __init__(
        self,
        policy: GuardPolicy | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.policy = policy or GuardPolicy()
        self.resolver = resolver or SystemResolver()
        self._ip_rules = IpRules(extra_allowed_networks=self.policy.extra_allowed_networks)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate(self, raw_url: str, scope: CrawlScope | None = None) -> ValidatedTarget:
        """Validate ``raw_url``; raise :class:`GuardError` if it must not be fetched.

        ``scope`` is supplied for every URL except the seed, and enforces that
        the crawler never leaves the company's own registrable domain.
        """
        parts = self._parse(raw_url)
        scheme, host, port, path_qs = parts

        if scope is not None and not scope.contains(host):
            raise GuardError(
                ReasonCode.OUT_OF_SCOPE,
                f"{host} is outside crawl scope {scope.registrable_domain}",
            )

        addresses = await self._resolve_and_check(host, port)

        normalized = urlunsplit(
            (
                scheme,
                host if port == _DEFAULT_PORTS[scheme] else f"{host}:{port}",
                *_split_path_qs(path_qs),
                "",
            )
        )
        return ValidatedTarget(
            url=normalized,
            scheme=scheme,
            host=host,
            port=port,
            path=_split_path_qs(path_qs)[0],
            addresses=tuple(addresses),
            registrable_domain=registrable_domain(host),
        )

    def check_syntax(self, raw_url: str) -> tuple[str, str, int, str]:
        """Run the non-network portion of validation.

        Exposed for the Playwright route interceptor, which must make a fast
        allow/deny decision for every browser request without paying for DNS on
        each subresource.
        """
        return self._parse(raw_url)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse(self, raw_url: str) -> tuple[str, str, int, str]:
        if not raw_url or not raw_url.strip():
            raise GuardError(ReasonCode.INVALID_URL, "empty url")

        try:
            split = urlsplit(raw_url.strip())
        except ValueError as exc:
            raise GuardError(ReasonCode.INVALID_URL, str(exc)) from exc

        scheme = (split.scheme or "").lower()
        if scheme not in self.policy.allowed_schemes:
            raise GuardError(ReasonCode.BLOCKED_SCHEME, f"scheme {scheme!r} not allowed")

        if split.username is not None or split.password is not None or "@" in (split.netloc or ""):
            raise GuardError(ReasonCode.INVALID_URL, "credentials in url are not allowed")

        raw_host = split.hostname
        if not raw_host:
            raise GuardError(ReasonCode.INVALID_URL, "missing host")

        try:
            port = split.port
        except ValueError as exc:
            raise GuardError(ReasonCode.INVALID_URL, f"invalid port: {exc}") from exc
        port = port or _DEFAULT_PORTS[scheme]
        if port not in self.policy.allowed_ports:
            raise GuardError(ReasonCode.BLOCKED_SCHEME, f"port {port} not allowed")

        literal = parse_ip_literal(raw_host)
        if literal is not None:
            if not self.policy.allow_ip_literals:
                raise GuardError(
                    ReasonCode.INVALID_URL, "bare IP addresses are not accepted as company sites"
                )
            host = raw_host.lower().strip("[]")
        else:
            host = _encode_host(raw_host)
            if _looks_numeric(host):
                raise GuardError(
                    ReasonCode.INVALID_URL, f"host {host!r} looks like an encoded IP address"
                )

        if is_blocked_hostname(host):
            raise GuardError(ReasonCode.PRIVATE_ADDRESS, f"host {host!r} is blocked")

        path_qs = split.path or "/"
        if split.query:
            path_qs = f"{path_qs}?{split.query}"
        return scheme, host, port, path_qs

    async def _resolve_and_check(self, host: str, port: int) -> list[IPAddress]:
        literal = parse_ip_literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                addresses = await self.resolver.resolve(host, port)
            except (socket.gaierror, OSError) as exc:
                raise GuardError(ReasonCode.DNS_FAILURE, f"{host}: {exc}") from exc

        if not addresses:
            raise GuardError(ReasonCode.DNS_FAILURE, f"{host} resolved to no addresses")

        # Every returned record must be acceptable.  A host that resolves to one
        # public and one private address is refused outright rather than having
        # the private record filtered out -- filtering would let an attacker
        # control which address the pool happens to pick.
        for address in addresses:
            if self._ip_rules.is_blocked(address):
                raise GuardError(
                    ReasonCode.PRIVATE_ADDRESS,
                    f"{host} resolves to blocked address {address}",
                )
        return [IpRules.normalize(a) for a in addresses]


def _split_path_qs(path_qs: str) -> tuple[str, str]:
    if "?" in path_qs:
        path, query = path_qs.split("?", 1)
        return path or "/", query
    return path_qs or "/", ""
