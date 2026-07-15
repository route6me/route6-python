"""Inbound forwarder — on ``{type:"incoming"}`` frame, fetch ``localhost:PORT``
and ship the response back via :py:meth:`TunnelClient.send_response`.

Mapping is built from the CLI's ``--hostname X --to PORT`` pairs and held
in a :class:`dict` keyed by fqdn. ``--to`` accepts a bare port, ``host:port``,
or full ``http://...`` URL — same as the npm client.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Mapping
from urllib.parse import urljoin

import httpx

from .tunnel_client import TunnelClient

logger = logging.getLogger("route6.forwarder")


class Forwarder:
    def __init__(self, *, tunnel: TunnelClient, hostname_to_target: Mapping[str, str]) -> None:
        self.tunnel = tunnel
        self.map = {self._to_fqdn(k): self._to_origin(v) for k, v in hostname_to_target.items()}
        # Reusing a single AsyncClient gives us connection-pooling to local
        # services — important if the app keeps a TCP-expensive socket open
        # (HTTPS upstream, large connection pool, etc.).
        self._client = httpx.AsyncClient(timeout=55.0, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def handle_frame(self, frame: dict) -> None:
        """Tunnel's ``on_frame`` callback — dispatches incoming."""
        if frame.get("type") != "incoming":
            return

        host = (frame["headers"].get("host") or frame["headers"].get("Host") or "").lower().split(":")[0]
        target = self.map.get(host)
        if not target:
            logger.warning("no local target for %s (req=%s)", host, frame.get("req_id"))
            await self.tunnel.send_response(
                frame["req_id"], 502,
                {"content-type": "text/plain"},
                f"No local target configured for {host}\n".encode(),
            )
            return

        upstream = urljoin(target, frame["path"])
        body = base64.b64decode(frame["body_b64"]) if frame.get("body_b64") else None

        forward_headers = {
            k: v
            for k, v in frame["headers"].items()
            if k.lower() not in ("host", "content-length")
        }

        t0 = time.monotonic()
        try:
            r = await self._client.request(
                frame["method"], upstream,
                headers=forward_headers,
                content=body,
            )
            body_bytes = r.content
            resp_headers = dict(r.headers)
            await self.tunnel.send_response(frame["req_id"], r.status_code, resp_headers, body_bytes)
            logger.info(
                "forwarded req=%s %s %s host=%s -> %s status=%d bytes=%d ms=%d",
                frame["req_id"], frame["method"], frame["path"], host,
                target.rstrip("/"), r.status_code, len(body_bytes),
                int((time.monotonic() - t0) * 1000),
            )
        except httpx.ConnectError as e:
            await self._error_502(frame["req_id"], "local service not reachable", e)
        except (httpx.ReadTimeout, httpx.WriteTimeout):
            await self._error_502(frame["req_id"], "local service timed out (55s)", None)
        except Exception as e:  # pragma: no cover — catchall
            await self._error_502(frame["req_id"], str(e), e)

    async def _error_502(self, req_id: str, reason: str, exc: Exception | None) -> None:
        logger.warning("forward failed req=%s: %s", req_id, reason)
        body = f"502 Bad Gateway\n\n{reason}\n".encode()
        await self.tunnel.send_response(
            req_id, 502,
            {"content-type": "text/plain", "x-route6-error": reason},
            body,
        )

    @staticmethod
    def _to_fqdn(name: str) -> str:
        lower = name.lower()
        # .mesh.route6.me names pass through untouched (private mesh endpoints,
        # feature-mesh-webhook WU-2.7) — only bare labels get the public suffix.
        if lower.endswith((".on.route6.me", ".mesh.route6.me")):
            return lower
        return f"{lower}.on.route6.me"

    @staticmethod
    def _to_origin(target: str) -> str:
        if target.isdigit():
            return f"http://127.0.0.1:{target}/"
        if target.startswith(("http://", "https://")):
            return target.rstrip("/") + "/"
        if ":" in target and "/" not in target:
            return f"http://{target}/"
        raise ValueError(f"bad --to target: {target}")
