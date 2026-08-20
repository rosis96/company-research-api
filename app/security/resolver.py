"""DNS resolution.

The guard resolves hostnames itself rather than delegating to the HTTP client,
because validating a *hostname* and then handing that hostname to a client that
performs its own lookup creates the DNS-rebinding TOCTOU window described in
SS12.  Resolution happens once, the resulting addresses are validated, and the
connection is then pinned to a validated address (see :mod:`app.security.transport`).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Protocol

import anyio

from app.security.ip_rules import IPAddress


class Resolver(Protocol):
    """Resolves a hostname to a list of IP addresses."""

    async def resolve(self, host: str, port: int) -> list[IPAddress]:  # pragma: no cover - protocol
        ...


class SystemResolver:
    """Resolves via the system resolver, off the event loop thread."""

    __slots__ = ()

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        def _lookup() -> list[IPAddress]:
            infos = socket.getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM
            )
            addresses: list[IPAddress] = []
            seen: set[str] = set()
            for family, _type, _proto, _canon, sockaddr in infos:
                raw = sockaddr[0]
                if raw in seen:
                    continue
                seen.add(raw)
                try:
                    addresses.append(ipaddress.ip_address(raw))
                except ValueError:  # pragma: no cover - defensive
                    continue
            return addresses

        return await anyio.to_thread.run_sync(_lookup)


class StaticResolver:
    """Deterministic resolver used by the test suite.

    Maps hostnames to fixed addresses so fixture sites can be reached without
    touching real DNS, and so rebinding scenarios can be simulated exactly.
    """

    __slots__ = ("_mapping", "_default", "calls")

    def __init__(
        self,
        mapping: dict[str, list[str]] | None = None,
        default: list[str] | None = None,
    ) -> None:
        self._mapping = {k.lower(): v for k, v in (mapping or {}).items()}
        self._default = default
        self.calls: list[str] = []

    def set(self, host: str, addresses: list[str]) -> None:
        self._mapping[host.lower()] = addresses

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        self.calls.append(host.lower())
        addresses = self._mapping.get(host.lower(), self._default)
        if addresses is None:
            raise socket.gaierror(f"no static mapping for {host!r}")
        return [ipaddress.ip_address(a) for a in addresses]


class SequenceResolver:
    """Returns a different answer on each call for a given host.

    Used to prove that the pinned transport is immune to DNS rebinding: the
    first lookup (validation) returns a public address, a second lookup would
    return a loopback address, and the test asserts the second lookup never
    influences the connection.
    """

    __slots__ = ("_sequences", "calls")

    def __init__(self, sequences: dict[str, list[list[str]]]) -> None:
        self._sequences = {k.lower(): list(v) for k, v in sequences.items()}
        self.calls: list[str] = []

    async def resolve(self, host: str, port: int) -> list[IPAddress]:
        key = host.lower()
        self.calls.append(key)
        queue = self._sequences.get(key)
        if not queue:
            raise socket.gaierror(f"no sequence for {host!r}")
        addresses = queue.pop(0) if len(queue) > 1 else queue[0]
        return [ipaddress.ip_address(a) for a in addresses]
