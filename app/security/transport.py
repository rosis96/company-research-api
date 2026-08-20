"""A DNS-pinned httpx transport.

Architecture reference: SS12, "DNS rebinding".

``httpx.AsyncHTTPTransport`` does not expose ``network_backend``, so this module
builds an :class:`httpcore.AsyncConnectionPool` directly and wraps it in a small
:class:`httpx.AsyncBaseTransport`.

The mechanism relies on a verified property of httpcore's connection logic:
``AsyncHTTPConnection._connect`` calls ``connect_tcp(host=origin.host)`` for the
TCP layer but passes ``server_hostname=origin.host`` separately to
``start_tls``.  Substituting a validated IP inside ``connect_tcp`` therefore
sends the packets to the address we checked, while TLS SNI and the ``Host``
header keep the real hostname -- certificate validation is unaffected and no
second DNS lookup ever happens.

Verified against httpcore 1.0.9.  This is a version-sensitive integration point;
:func:`assert_pinning_supported` fails loudly if a future httpcore changes the
contract, rather than silently degrading to unpinned resolution.
"""

from __future__ import annotations

import inspect
import ssl
import typing
from collections.abc import AsyncIterator

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.base import AsyncNetworkStream

from app.security.ip_rules import IPAddress


class PinRegistry:
    """Maps ``host:port`` to the validated address the guard cleared.

    Populated by the fetcher immediately before each request (and again for each
    redirect hop).  Entries are overwritten rather than accumulated, so the pin
    always reflects the most recent validation.
    """

    __slots__ = ("_pins",)

    def __init__(self) -> None:
        self._pins: dict[str, str] = {}

    def pin(self, host: str, port: int, address: IPAddress) -> None:
        self._pins[_key(host, port)] = _literal(address)

    def resolve(self, host: str, port: int) -> str | None:
        return self._pins.get(_key(host, port))

    def clear(self) -> None:
        self._pins.clear()

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._pins)


def _key(host: str, port: int) -> str:
    return f"{host.lower().strip('[]')}:{port}"


def _literal(address: IPAddress) -> str:
    text = str(address)
    return f"[{text}]" if ":" in text else text


class PinnedBackend(AnyIOBackend):
    """Network backend that redirects TCP connects to pre-validated addresses."""

    def __init__(self, registry: PinRegistry, *, strict: bool = True) -> None:
        super().__init__()
        self._registry = registry
        self._strict = strict

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> AsyncNetworkStream:
        pinned = self._registry.resolve(host, port)
        if pinned is None:
            if self._strict:
                # A connection attempt for a host the guard never cleared.  This
                # is invariant 4 failing closed rather than falling back to the
                # system resolver.
                raise httpcore.ConnectError(
                    f"refusing to connect to unvalidated host {host!r}:{port}"
                )
            pinned = host
        return await super().connect_tcp(
            host=pinned,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _ResponseStream(httpx.AsyncByteStream):
    """Adapts an httpcore response stream to the httpx interface."""

    __slots__ = ("_stream",)

    def __init__(self, stream: typing.AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        closer = getattr(self._stream, "aclose", None)
        if closer is not None:
            await closer()


class GuardedTransport(httpx.AsyncBaseTransport):
    """httpx transport whose connections are pinned to guard-validated addresses."""

    def __init__(
        self,
        registry: PinRegistry,
        *,
        verify: bool = True,
        http2: bool = True,
        max_connections: int = 32,
        max_keepalive_connections: int = 8,
        keepalive_expiry: float = 5.0,
        strict: bool = True,
    ) -> None:
        context = ssl.create_default_context()
        if not verify:  # pragma: no cover - not used in production wiring
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._registry = registry
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=True,
            http2=http2,
            retries=0,
            network_backend=PinnedBackend(registry, strict=strict),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        assert isinstance(response.stream, typing.AsyncIterable)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def assert_pinning_supported() -> None:
    """Fail fast if httpcore's connect/SNI contract has changed.

    Called at application startup.  If a future httpcore stops passing the
    origin host to ``connect_tcp``, or starts deriving SNI from the connect
    target, pinning would silently stop working -- which is exactly the kind of
    security regression that must not be discovered in production.
    """
    signature = inspect.signature(AnyIOBackend.connect_tcp)
    for required in ("host", "port"):
        if required not in signature.parameters:
            raise RuntimeError(
                "httpcore AnyIOBackend.connect_tcp signature changed; "
                "DNS pinning in app.security.transport must be revalidated"
            )

    source = inspect.getsource(httpcore._async.connection.AsyncHTTPConnection._connect)
    if "server_hostname" not in source or "sni_hostname" not in source:
        raise RuntimeError(
            "httpcore no longer sets server_hostname independently of the connect "
            "target; DNS pinning in app.security.transport must be revalidated"
        )
