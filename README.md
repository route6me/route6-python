# route6

Python thin client for [Route6.me](https://route6.me) — Python parity of [`@route6/agent`](https://www.npmjs.com/package/@route6/agent).

Two surfaces:
- **CLI** — `route6 tunnel start --hostname X --to PORT` exposes a localhost port at `https://X.on.route6.me`, plus a local MCP proxy at `http://127.0.0.1:3000/mcp` for Cursor / Claude Code / Cline / any MCP-aware editor.
- **Library** — `from route6 import Route6` calls every MCP tool with typed Python ergonomics.

## Install

```bash
pip install route6
```

Requires Python ≥ 3.10. Linux x64, macOS arm64 / x64 — tested matrix.

## Quick start (CLI)

```bash
route6 login sk_a6_<your-api-key>

python3 -m http.server 3000 &

route6 tunnel start --hostname my-app --to 3000
```

In another terminal:

```bash
curl https://my-app.on.route6.me/
# → your local server's directory listing, served over the public internet.
```

The MCP proxy is also live at `http://127.0.0.1:3000/mcp` — point your editor at it.

## Quick start (library)

```python
from route6 import Route6

with Route6(api_key="sk_a6_...") as r6:
    ident = r6.tools.identity_get()
    print(ident["active_ipv6"], ident["prefix"])

    page = r6.tools.web_fetch(url="https://example.com")
    print(page["body"][:200])
```

Calls hit the public MCP at `https://gw.route6.me/mcp` over HTTP/2. The result is unwrapped — `tools.web_fetch(...)` returns the parsed dict the gateway produced, not the raw JSON-RPC envelope.

Available tools mirror the Node SDK and the container: `identity_get`, `identity_set_ipv6`, `web_fetch`, `net_ping`, `hostname_register`, `team_chat`, `team_task`, etc. — full list at <https://docs.route6.me>.

## Commands

| Command | Purpose |
|--------|---------|
| `route6 login <api_key>` | Save the API key to `~/.route6/config.json` (mode 0600), verify against the gateway. |
| `route6 logout` | Clear the stored key. |
| `route6 status` | Print config + `GET /whoami` from the gateway. |
| `route6 tunnel start --hostname X --to PORT` | Open inbound tunnel + start local MCP proxy. Pair `--hostname` and `--to` repeat-by-repeat for multi-host. |
| `route6 tunnel start ... --no-mcp` | Tunnel only, skip the MCP proxy. |
| `route6 mcp serve --port 3000` | MCP-only mode (no inbound tunnel) — useful for hosted agents. |

## Tier comparison

| | Lite (this client) | Pro (Docker container) |
|--|--|--|
| Install | `pip install route6` | `docker compose up` |
| Outbound source IP | Your `/64` (preserved via the Route6 edge) | Your `/64` directly |
| Inbound to public hostname | via `gw.route6.me` reverse tunnel | direct to your container |
| Mesh between agents | Not in v1 | Native WireGuard |
| MCP tools | All 28 | All 28 |

## Links

- **Get an API key / manage your agents:** [route6.me](https://route6.me)
- **Docs:** [docs.route6.me](https://docs.route6.me)
- **Examples** (webhooks, clean-IP fetch, team coordination): [github.com/route6me/examples](https://github.com/route6me/examples)
- **Node.js client:** [`@route6/agent` on npm](https://www.npmjs.com/package/@route6/agent) · [source](https://github.com/route6me/route6-agent)

## License

MIT © [Route6](https://route6.me) — the client is open source; the Route6 network service it connects to is a commercial product.
