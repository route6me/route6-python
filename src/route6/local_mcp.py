"""Local MCP proxy — Python parity with the npm client.

Exposes ``http://127.0.0.1:PORT/mcp`` and forwards every request to
``https://gw.route6.me/mcp`` with the stored API key injected as Bearer.
Both transports (StreamableHTTP POST + SSE GET) are streamed through
chunk-by-chunk so SSE events arrive in real time and the
``Mcp-Session-Id`` round-trips correctly.

Uses stdlib :mod:`http.server` + a worker thread per request, but the
upstream call goes through ``httpx`` (HTTP/2) and is run on its own asyncio
loop in a thread so the standard HTTP server stays simple.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .config import load_config

logger = logging.getLogger("route6.mcp_proxy")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "route6-mcp-proxy/0.1.0"

    # Subclass-injected via class attribute, set in McpProxy.start()
    gateway_url: str = ""
    api_key: str = ""
    upstream: httpx.Client | None = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: object) -> None:  # quiet the default access log
        return

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")

    def _proxy(self, method: str) -> None:
        if not (self.path == "/mcp" or self.path.startswith("/mcp/") or self.path.startswith("/mcp?")):
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found","hint":"MCP proxy serves /mcp only"}')
            return

        out_headers: dict[str, str] = {"authorization": f"Bearer {self.api_key}"}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "authorization":
                continue
            out_headers[k] = v

        # Read request body (if any). MCP messages are small enough that
        # buffering is OK; SSE responses are the streamy bit, which we
        # handle below.
        content_length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(content_length) if content_length else None

        url = f"{self.gateway_url}{self.path}"

        assert self.upstream is not None
        try:
            with self.upstream.stream(method, url, headers=out_headers, content=body, timeout=None) as resp:
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.end_headers()
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
        except httpx.HTTPError as e:
            logger.warning("upstream MCP failed: %s", e)
            try:
                self.send_response(502)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(f'{{"error":"upstream_unreachable","message":{e!s}}}'.encode())
            except Exception:
                pass


class McpProxy:
    def __init__(self, *, port: int = 3000, gateway_url: str | None = None, api_key: str | None = None) -> None:
        cfg = load_config()
        self.port = port
        self.gateway_url = gateway_url or cfg.gateway_url
        self.api_key = api_key or cfg.api_key
        if not self.api_key:
            raise RuntimeError("Not logged in")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._upstream: httpx.Client | None = None

    def start(self) -> None:
        # Build a per-process upstream client. http2=True opts into the
        # multiplexed transport when the gateway supports it.
        self._upstream = httpx.Client(http2=True, timeout=None)

        # Bind classvars on a fresh subclass so we can run multiple McpProxy
        # instances in the same process without trampling each other.
        Handler = type(
            "BoundHandler",
            (_Handler,),
            {
                "gateway_url": self.gateway_url,
                "api_key": self.api_key,
                "upstream": self._upstream,
            },
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("local MCP proxy listening on http://127.0.0.1:%d/mcp", self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._upstream is not None:
            self._upstream.close()
            self._upstream = None
