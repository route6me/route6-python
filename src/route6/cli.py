"""``route6`` CLI — Python parity with @route6/agent.

Subcommands: login, logout, status, tunnel start, mcp serve.
Argparse-based; no extra dependency beyond ``httpx``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any, Iterable

import httpx

from .config import CONFIG_PATH, AgentConfig, load_config, require_api_key, save_config
from .forwarder import Forwarder
from .local_mcp import McpProxy
from .tunnel_client import TunnelClient

logger = logging.getLogger("route6.cli")


def _green(s: str) -> str:
    return f"\x1b[32m{s}\x1b[0m" if sys.stdout.isatty() else s


def _red(s: str) -> str:
    return f"\x1b[31m{s}\x1b[0m" if sys.stderr.isatty() else s


# ----- subcommand handlers -----

def _cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.api_key = args.api_key
    if args.gateway:
        cfg.gateway_url = args.gateway
    if args.api:
        cfg.api_url = args.api
    cfg.last_session_id = None
    cfg.last_session_at = None
    save_config(cfg)
    try:
        r = httpx.get(
            f"{cfg.gateway_url}/whoami",
            headers={"authorization": f"Bearer {args.api_key}"},
            timeout=10.0,
        )
        if r.status_code != 200:
            sys.stderr.write(_red(f"✗ Saved key to {CONFIG_PATH} but /whoami returned HTTP {r.status_code}.\n"))
            return 1
        j = r.json()
        sys.stdout.write(_green(
            f"✓ Logged in — agent {j.get('agent_id')} / customer {j.get('customer_id')}"
            f" / plan {j.get('plan')} / tier {j.get('connection_type')}\n"
        ))
        sys.stdout.write(f"  Config written to {CONFIG_PATH}\n")
        return 0
    except httpx.HTTPError as e:
        sys.stderr.write(_red(f"✗ Failed to reach {cfg.gateway_url}: {e}\n"))
        return 1


def _cmd_logout(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.api_key = None
    cfg.last_session_id = None
    cfg.last_session_at = None
    save_config(cfg)
    sys.stdout.write(_green(f"✓ Logged out ({CONFIG_PATH})\n"))
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    sys.stdout.write(f"config: {CONFIG_PATH}\n")
    sys.stdout.write(f"gateway: {cfg.gateway_url}\n")
    sys.stdout.write(f"api: {cfg.api_url}\n")
    if not cfg.api_key:
        sys.stdout.write(_red("api_key: (not logged in — run `route6 login <api_key>`)\n"))
        return 1
    sys.stdout.write(f"api_key: {cfg.api_key[:12]}…\n")
    if cfg.last_session_id:
        import time
        age = int((time.time() * 1000 - (cfg.last_session_at or 0)) / 1000)
        sys.stdout.write(f"last_session: {cfg.last_session_id} ({age}s ago)\n")
    try:
        r = httpx.get(
            f"{cfg.gateway_url}/whoami",
            headers={"authorization": f"Bearer {cfg.api_key}"},
            timeout=10.0,
        )
        sys.stdout.write(f"\nGET {cfg.gateway_url}/whoami → HTTP {r.status_code}\n{r.text}\n")
        return 0 if r.status_code == 200 else 1
    except httpx.HTTPError as e:
        sys.stderr.write(_red(f"✗ {cfg.gateway_url}/whoami unreachable: {e}\n"))
        return 1


def _cmd_tunnel_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    require_api_key(cfg)
    pairs = _pair_hostname_to(args.hostname or [], args.to or [])
    if not pairs:
        sys.stderr.write(_red("✗ At least one --hostname X --to PORT pair required.\n"))
        return 1
    for h, t in pairs.items():
        sys.stdout.write(f"  → {h}.on.route6.me  →  {t}\n")

    return asyncio.run(_tunnel_start_async(pairs, want_mcp=not args.no_mcp, mcp_port=args.mcp_port))


async def _tunnel_start_async(pairs: dict[str, str], *, want_mcp: bool, mcp_port: int) -> int:
    tc = TunnelClient(
        on_frame=lambda f: fwd.handle_frame(f),
        on_session=lambda sid: sys.stdout.write(_green(f"✓ tunnel session: {sid}\n")),
    )
    fwd = Forwarder(tunnel=tc, hostname_to_target=pairs)

    mcp: McpProxy | None = None
    if want_mcp:
        try:
            mcp = McpProxy(port=mcp_port)
            mcp.start()
            sys.stdout.write(_green(f"✓ local MCP proxy: http://127.0.0.1:{mcp_port}/mcp\n"))
        except OSError as e:
            sys.stderr.write(_red(f"✗ MCP proxy failed to bind {mcp_port}: {e}\n"))
            mcp = None

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sig(sig: int) -> None:
        sys.stdout.write(f"\nsignal {sig} received — shutting down…\n")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _handle_sig, s)

    runner = asyncio.create_task(tc.run())
    waiter = asyncio.create_task(stop_event.wait())
    done, _pending = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if waiter in done:
        await tc.stop()
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
    if mcp is not None:
        mcp.stop()
    await fwd.aclose()
    return 0


def _cmd_tunnel_stop(_args: argparse.Namespace) -> int:
    sys.stdout.write("Press Ctrl+C in the terminal where `route6 tunnel start` is running.\n")
    sys.stdout.write("(Background-daemon mode ships in a later release.)\n")
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    require_api_key(cfg)
    mcp = McpProxy(port=args.port)
    mcp.start()
    sys.stdout.write(_green(
        f"✓ MCP proxy: http://127.0.0.1:{args.port}/mcp → {cfg.gateway_url}/mcp\n"
    ))
    sys.stdout.write("Configure your editor (Cursor, Claude Code, Cline, etc.) with that URL.\n")
    sys.stdout.write("Ctrl+C to stop.\n")
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        mcp.stop()
    return 0


# ----- argument parsing -----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="route6",
        description="Route6 thin client — tunnel localhost ports to *.on.route6.me + local MCP proxy.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("login", help="Store API key in ~/.route6/config.json")
    pl.add_argument("api_key", help="Your Route6 API key (sk_a6_...)")
    pl.add_argument("--gateway", help="Override gateway URL (default https://gw.route6.me)")
    pl.add_argument("--api", help="Override controller API URL (default https://api.route6.me)")
    pl.set_defaults(func=_cmd_login)

    sub.add_parser("logout", help="Clear the stored API key.").set_defaults(func=_cmd_logout)
    sub.add_parser("status", help="Show config + connectivity check.").set_defaults(func=_cmd_status)

    pt = sub.add_parser("tunnel", help="Open / close the inbound tunnel.")
    pts = pt.add_subparsers(dest="tunnel_command", required=True)
    pst = pts.add_parser("start", help="Open the tunnel and forward inbound traffic to local ports.")
    pst.add_argument("--hostname", action="append", help="Hostname (bare or full fqdn). Repeatable.")
    pst.add_argument("--to", action="append", help="Target port for the latest --hostname. Repeatable.")
    pst.add_argument("--no-mcp", action="store_true", help="Skip the local MCP proxy.")
    pst.add_argument("--mcp-port", type=int, default=3000, help="Local MCP proxy port (default 3000).")
    pst.set_defaults(func=_cmd_tunnel_start)
    pts.add_parser("stop", help="Tell a running `route6 tunnel start` daemon to stop.").set_defaults(func=_cmd_tunnel_stop)

    pm = sub.add_parser("mcp", help="MCP-only modes.")
    pms = pm.add_subparsers(dest="mcp_command", required=True)
    pmsv = pms.add_parser("serve", help="Run only the local MCP proxy (no inbound tunnel).")
    pmsv.add_argument("--port", type=int, default=3000, help="localhost port to bind (default 3000).")
    pmsv.set_defaults(func=_cmd_mcp_serve)

    return p


def _pair_hostname_to(hostnames: Iterable[str], tos: Iterable[str]) -> dict[str, str]:
    hs = list(hostnames or [])
    ts = list(tos or [])
    if len(hs) != len(ts):
        raise SystemExit(f"--hostname and --to count mismatch ({len(hs)} hostnames, {len(ts)} targets). Pair them in order.")
    return dict(zip(hs, ts))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
