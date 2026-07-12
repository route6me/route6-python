"""route6 — Python thin client for Route6.me.

Two surfaces:
  - CLI:  ``route6 login`` / ``route6 tunnel start`` / ``route6 mcp serve``  (see route6.cli)
  - Lib:  ``from route6 import Route6; r6 = Route6(api_key=...); r6.tools.web_fetch(url=...)``

The library calls the public MCP endpoint at ``https://gw.route6.me/mcp`` and
unwraps the JSON-RPC result for you. Identical behavior to the npm package
(@route6/agent) — same protocol, same fail modes.
"""

from .client import Route6  # re-export

__all__ = ["Route6"]
__version__ = "0.1.0"
