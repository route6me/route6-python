"""Library API — ``from route6 import Route6``.

Talks to ``https://gw.route6.me/mcp`` over StreamableHTTP, manages the
session id, and exposes every MCP tool as a typed method via ``r6.tools``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import load_config

logger = logging.getLogger("route6.client")


class _ToolNamespace:
    """Attribute-style access to MCP tools.

    Calls translate to ``tools/call`` JSON-RPC requests:

        r6.tools.web_fetch(url="https://example.com")

    becomes

        {"jsonrpc":"2.0","id":N,"method":"tools/call",
         "params":{"name":"web_fetch","arguments":{"url":"https://example.com"}}}

    The result is unwrapped: if the server returns ``content:[{type:"text",text:"..."}]``
    and the text parses as JSON, you get the parsed object; otherwise the text
    is returned verbatim. Raises on JSON-RPC error.
    """

    def __init__(self, client: "Route6") -> None:
        self._c = client

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def call(**arguments: Any) -> Any:
            return self._c._tool_call(name, arguments)
        call.__name__ = name
        return call


class Route6:
    """High-level Route6 client.

    Typical usage::

        from route6 import Route6
        r6 = Route6(api_key="sk_a6_...")  # or omit to read ~/.route6/config.json
        ip = r6.tools.identity_get()
        result = r6.tools.web_fetch(url="https://example.com")

    The connection is HTTP/2 (multiplexed); a single ``Route6()`` instance
    can be reused across many tool calls. Call :py:meth:`close` to release the
    underlying client (or use as a context manager).
    """

    def __init__(self, *, api_key: str | None = None, gateway_url: str | None = None) -> None:
        cfg = load_config()
        self.api_key = api_key or cfg.api_key
        self.gateway_url = gateway_url or cfg.gateway_url
        if not self.api_key:
            raise RuntimeError("Route6: api_key not provided and none in ~/.route6/config.json")
        self._http = httpx.Client(http2=True, timeout=120.0)
        self._session_id: str | None = None
        self._next_id = 1
        self.tools = _ToolNamespace(self)

    def __enter__(self) -> "Route6":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ----- internals -----

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        body = {
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "route6-python", "version": "0.1.0"},
            },
        }
        self._next_id += 1
        r = self._http.post(
            f"{self.gateway_url}/mcp",
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json=body,
        )
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        if not sid:
            raise RuntimeError("gateway did not return Mcp-Session-Id on initialize")
        self._session_id = sid

    def _tool_call(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_session()
        body = {
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._next_id += 1
        assert self._session_id
        r = self._http.post(
            f"{self.gateway_url}/mcp",
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-session-id": self._session_id,
            },
            json=body,
        )
        r.raise_for_status()
        payload = _parse_mcp_response(r.text)
        if "error" in payload:
            raise RuntimeError(f"{name}: {payload['error'].get('message', payload['error'])}")
        result = payload.get("result", {})
        content = result.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result


def _parse_mcp_response(text: str) -> dict[str, Any]:
    """The gateway returns either JSON or an SSE event stream (``data: {...}``).

    We accept both — for SSE, take the first ``data:`` line that parses as JSON.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"no JSON payload in MCP response: {text[:200]}")
