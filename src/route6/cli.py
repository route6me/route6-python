"""`route6` — a launcher for the Route6 client.

Everything except `upgrade` is handed straight to the r6me binary, so this
package never has to be updated when the client gains a command.
"""

from __future__ import annotations

import os
import sys

from .launcher import LauncherError, bin_dir, ensure_binary

# 0.1.x implemented the protocol itself and had its own vocabulary. That code is
# gone, so name the replacement rather than failing with "unknown command" —
# these are the commands people have in their shell history and their scripts.
RETIRED = {
    "login": (
        "`route6 login` is gone. The client reads its key from a config file:\n"
        "    mkdir -p ~/.r6me && chmod 700 ~/.r6me\n"
        "    echo 'api_key = \"sk_a6_your_key_here\"' > ~/.r6me/config.toml\n"
        "    chmod 600 ~/.r6me/config.toml\n"
        "  or set ROUTE6_API_KEY in the environment."
    ),
    "logout": "`route6 logout` is gone. Delete ~/.r6me/config.toml.",
    "tunnel": (
        "`route6 tunnel` is gone. Inbound is a port forward now — start the daemon\n"
        "  with `route6 up`, then use the port_forward_create MCP tool."
    ),
    "mcp": (
        "`route6 mcp serve` is gone. `route6 up` serves MCP on\n"
        "  http://localhost:3000/mcp as part of running the daemon."
    ),
}

USAGE = """route6 — Route6 client launcher

  route6 up               connect the daemon
  route6 down             disconnect
  route6 status           transport state, config generation, forwards, MCP
  route6 ssh <name>       shell on a team-mate over the private mesh
  route6 version          print the client version
  route6 upgrade          re-fetch the current stable binary

Any other arguments are passed straight through to the r6me binary.
Docs: https://docs.route6.me/quick-start/r6me
"""


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0

    if argv and argv[0] in RETIRED:
        sys.stderr.write(RETIRED[argv[0]] + "\n")
        return 2

    force = False
    if argv and argv[0] == "upgrade":
        force = True
        argv = ["version"]

    try:
        binary = ensure_binary(force_refresh=force)
    except LauncherError as e:
        sys.stderr.write(f"route6: {e}\n")
        return 1

    # R6ME_INSTALL_CHANNEL tells the daemon which ecosystem launched it. Nothing
    # on the wire reveals that — the binary below is byte-identical to the one a
    # `curl | sh` install puts on disk — so the launcher is the only thing that
    # knows. An explicit value already in the environment wins, so a container or
    # a wrapper can still say what it is.
    os.environ.setdefault("R6ME_INSTALL_CHANNEL", "pip")

    # execv replaces this process, so signals, stdio and the exit code belong to
    # the daemon directly — nothing to forward and nothing to get wrong.
    try:
        os.execv(binary, [binary, *argv])
    except OSError as e:
        sys.stderr.write(f"route6: could not run {binary}: {e}\n")
        sys.stderr.write(f"route6: try removing {bin_dir()} and running again\n")
        return 1
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
