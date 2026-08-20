"""A real local HTTP server for fixture sites.

The crawler is tested against actual sockets rather than a mocked transport, so
that redirect walking, streaming byte caps, content-type gating, timeouts and
the pinned-resolver transport are all exercised end to end.

Hostnames are mapped to ``127.0.0.1`` by a
:class:`~app.security.resolver.StaticResolver`, and the loopback range is
allowed only by passing ``extra_allowed_networks`` to the guard *policy* in
tests.  Production wiring never sets that field, so the guard is not weakened.
"""

from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Route:
    """One fixture response."""

    body: str | bytes = ""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0
    #: When set, the response is a redirect to this location.
    redirect_to: str | None = None
    gzip_body: bool = False


@dataclass
class SiteSpec:
    """A fixture website: a hostname plus its routes."""

    host: str
    routes: dict[str, Route]
    #: Called with the request path; return a Route to override the table.
    dynamic: Callable[[str], Route | None] | None = None
    default_status: int = 404


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FixtureServer/1.0"

    @property
    def spec(self) -> SiteSpec:
        return self.server.spec  # type: ignore[attr-defined]

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence test noise
        return

    def _resolve(self, path: str) -> Route | None:
        key = path.split("?", 1)[0]
        if self.spec.dynamic is not None:
            dynamic = self.spec.dynamic(key)
            if dynamic is not None:
                return dynamic
        route = self.spec.routes.get(key)
        if route is None and not key.endswith("/"):
            route = self.spec.routes.get(key + "/")
        if route is None and key.endswith("/") and len(key) > 1:
            route = self.spec.routes.get(key.rstrip("/"))
        return route

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.server.request_log.append(self.path)  # type: ignore[attr-defined]
        route = self._resolve(self.path)

        if route is None:
            body = b"<html><body><h1>Not Found</h1></body></html>"
            self.send_response(self.spec.default_status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route.delay_s:
            time.sleep(route.delay_s)

        if route.redirect_to is not None:
            self.send_response(route.status if route.status in (301, 302, 303, 307, 308) else 302)
            self.send_header("Location", route.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        raw = route.body.encode("utf-8") if isinstance(route.body, str) else route.body
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        for name, value in route.headers.items():
            self.send_header(name, value)
        if route.gzip_body:
            raw = gzip.compress(raw)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client aborted at its byte cap; that is the point


class FixtureServer:
    """Runs a :class:`SiteSpec` on an ephemeral loopback port."""

    def __init__(self, spec: SiteSpec) -> None:
        self.spec = spec
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.spec = spec  # type: ignore[attr-defined]
        self._server.request_log = []  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        return self.spec.host

    @property
    def origin(self) -> str:
        return f"http://{self.spec.host}:{self.port}"

    @property
    def requests(self) -> list[str]:
        return list(self._server.request_log)  # type: ignore[attr-defined]

    def clear_requests(self) -> None:
        self._server.request_log.clear()  # type: ignore[attr-defined]

    def url(self, path: str = "/") -> str:
        return f"{self.origin}{path}"

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
