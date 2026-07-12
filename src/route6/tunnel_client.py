"""Long-lived tunnel client — Python parity with @route6/agent.

Wire is identical: ``POST /tunnel`` with Bearer + ``{}`` body, server writes
newline-delimited JSON frames. Heartbeats are separate ``POST /tunnel/heartbeat``
calls every 30s, reconnect uses exponential backoff (1/2/4/8/16/30s capped),
and an ``If-Resume`` header within 60s of disconnect restores the prior
session id.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable, Iterable

import httpx

from .config import AgentConfig, load_config, save_config

logger = logging.getLogger("route6.tunnel")

HEARTBEAT_INTERVAL_S = 30
RESUME_WINDOW_MS = 60_000
BACKOFFS_S = [1, 2, 4, 8, 16, 30]

FrameHandler = Callable[[dict], Awaitable[None]]


class TunnelClient:
    """Owns the long-lived ``/tunnel`` connection and the heartbeat task.

    The caller (forwarder) registers a frame-handler for ``{type:"incoming"}``
    via ``on_frame``; everything else (session-ack, hostname-add/remove,
    replaced/evicted) is handled internally and surfaced through logs.
    """

    def __init__(
        self,
        *,
        on_frame: FrameHandler,
        gateway_url: str | None = None,
        api_key: str | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> None:
        cfg = load_config()
        self.gateway_url = gateway_url or cfg.gateway_url
        self.api_key = api_key or cfg.api_key
        if not self.api_key:
            raise RuntimeError("Not logged in")
        self._on_frame = on_frame
        self._on_session = on_session
        self.current_session_id: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._reconnect_attempt = 0
        # Two clients: one for the long-lived stream (kept open for hours),
        # one for short bidirectional calls (heartbeat, response, disconnect).
        # Sharing a single httpx.AsyncClient produced silent heartbeat stalls
        # under HTTP/2 — the streaming response monopolised the only pooled
        # connection and short POSTs queued behind it. Two clients = two
        # separate TCP connections = no head-of-line blocking.
        self._stream_client: httpx.AsyncClient | None = None
        self._client: httpx.AsyncClient | None = None

    # ----- public API for the forwarder -----

    async def send_response(self, req_id: str, status: int, headers: dict, body: bytes) -> None:
        """POST /tunnel/response with the reply from localhost."""
        if not self.current_session_id:
            raise RuntimeError("no live session")
        payload = {
            "session_id": self.current_session_id,
            "req_id": req_id,
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii") if body else None,
        }
        assert self._client is not None
        r = await self._client.post(
            f"{self.gateway_url}/tunnel/response",
            json=payload,
            headers={"authorization": f"Bearer {self.api_key}"},
            timeout=15.0,
        )
        if r.status_code >= 400:
            logger.warning("tunnel/response rejected: %s %s", r.status_code, r.text[:200])

    # ----- lifecycle -----

    async def run(self) -> None:
        """Block forever — opens the tunnel, reconnects on disconnect."""
        self._stream_client = httpx.AsyncClient(http2=True, timeout=None)
        self._client = httpx.AsyncClient(http2=True, timeout=None)
        try:
            while not self._shutting_down:
                try:
                    await self._open_once()
                except Exception as e:
                    logger.warning("tunnel connection error: %s", e)
                self._stop_heartbeats()
                if self._shutting_down:
                    break
                await self._backoff()
        finally:
            await self._stream_client.aclose()
            await self._client.aclose()

    async def stop(self) -> None:
        """Graceful shutdown — POST /tunnel/disconnect then exit."""
        self._shutting_down = True
        self._stop_heartbeats()
        if self.current_session_id and self._client is not None:
            try:
                await self._client.post(
                    f"{self.gateway_url}/tunnel/disconnect",
                    json={"session_id": self.current_session_id, "reason": "client_shutdown"},
                    headers={"authorization": f"Bearer {self.api_key}"},
                    timeout=2.0,
                )
            except Exception:
                pass
        # Tiny grace period for any in-flight POSTs
        await asyncio.sleep(0.2)

    # ----- internals -----

    async def _open_once(self) -> None:
        assert self._stream_client is not None
        cfg = load_config()
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": f"route6/py-0.1.0",
        }
        if cfg.last_session_id and cfg.last_session_at and \
                (int(time.time() * 1000) - cfg.last_session_at) < RESUME_WINDOW_MS:
            headers["if-resume"] = cfg.last_session_id

        async with self._stream_client.stream(
            "POST", f"{self.gateway_url}/tunnel",
            headers=headers, content="{}", timeout=None,
        ) as resp:
            if resp.status_code == 409:
                body = await resp.aread()
                self._shutting_down = True
                raise RuntimeError(f"gateway 409: {body.decode(errors='replace')}")
            if resp.status_code == 401:
                self._shutting_down = True
                raise RuntimeError("401 unauthorized — API key invalid or revoked")
            if resp.status_code != 200:
                raise RuntimeError(f"/tunnel returned HTTP {resp.status_code}")

            logger.info("tunnel open, reading frames")
            self._reconnect_attempt = 0
            self._start_heartbeats()

            try:
                async for line in _iter_lines(resp):
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("bad frame: %s", line[:120])
                        continue
                    await self._handle_frame(frame)
            finally:
                self._persist_session()

    async def _handle_frame(self, frame: dict) -> None:
        t = frame.get("type")
        if t == "session":
            self.current_session_id = frame["session_id"]
            self._persist_session()
            if self._on_session:
                self._on_session(frame["session_id"])
            logger.info(
                "tunnel session %s — id=%s hostnames=%s",
                "resumed" if frame.get("resumed_from") else "opened",
                frame["session_id"],
                [h["fqdn"] for h in frame.get("hostnames", [])],
            )
        elif t == "hostname-added":
            logger.info("hostname added to tunnel: %s", frame.get("fqdn"))
        elif t == "hostname-removed":
            logger.info("hostname removed from tunnel: %s", frame.get("fqdn"))
        elif t == "replaced":
            logger.warning("tunnel replaced by another session — exiting")
            self._shutting_down = True
            self.current_session_id = None
        elif t == "evicted":
            logger.warning("tunnel evicted by gateway: %s", frame.get("reason"))
        elif t == "disconnecting":
            logger.info("gateway disconnecting: %s", frame.get("reason"))
        elif t == "incoming":
            await self._on_frame(frame)
        else:
            logger.debug("unknown frame type: %s", t)

    def _start_heartbeats(self) -> None:
        self._stop_heartbeats()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeats(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        assert self._client is not None
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                if not self.current_session_id:
                    continue
                try:
                    await self._client.post(
                        f"{self.gateway_url}/tunnel/heartbeat",
                        json={"session_id": self.current_session_id},
                        headers={"authorization": f"Bearer {self.api_key}"},
                        timeout=10.0,
                    )
                except Exception as e:
                    logger.warning("heartbeat failed: %s", e)
        except asyncio.CancelledError:
            pass

    async def _backoff(self) -> None:
        idx = min(self._reconnect_attempt, len(BACKOFFS_S) - 1)
        s = BACKOFFS_S[idx]
        self._reconnect_attempt += 1
        logger.info("reconnecting in %ss (attempt %d)", s, self._reconnect_attempt)
        await asyncio.sleep(s)

    def _persist_session(self) -> None:
        try:
            cfg = load_config()
            cfg.last_session_id = self.current_session_id
            cfg.last_session_at = int(time.time() * 1000) if self.current_session_id else None
            save_config(cfg)
        except Exception as e:
            logger.warning("failed to persist session: %s", e)


async def _iter_lines(resp: httpx.Response) -> Iterable[str]:
    """Yield text lines from a streamed response.

    httpx's ``aiter_lines`` exists but historically buffered fully for some
    transports — using ``aiter_bytes`` + manual line-splitting matches the
    behavior of the npm client's TextDecoder loop exactly.
    """
    buf = b""
    async for chunk in resp.aiter_bytes():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace").strip()
