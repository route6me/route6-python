"""Persistent config — ``~/.route6/config.json`` (mode 0600).

Matches the layout the npm client uses so both clients can coexist on the
same machine and share session-resume state if a user happens to run both
(uncommon, but no reason to fight it)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_GATEWAY = os.environ.get("ROUTE6_GATEWAY", "https://gw.route6.me")
DEFAULT_API = os.environ.get("ROUTE6_API", "https://api.route6.me")

CONFIG_DIR = Path(os.environ.get("ROUTE6_CONFIG_DIR") or (Path.home() / ".route6"))
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class AgentConfig:
    api_key: str | None = None
    gateway_url: str = DEFAULT_GATEWAY
    api_url: str = DEFAULT_API
    last_session_id: str | None = None
    last_session_at: int | None = None  # epoch ms — matches npm client


def load_config() -> AgentConfig:
    if not CONFIG_PATH.exists():
        return AgentConfig()
    raw = json.loads(CONFIG_PATH.read_text())
    return AgentConfig(
        api_key=raw.get("api_key"),
        gateway_url=raw.get("gateway_url", DEFAULT_GATEWAY),
        api_url=raw.get("api_url", DEFAULT_API),
        last_session_id=raw.get("last_session_id"),
        last_session_at=raw.get("last_session_at"),
    )


def save_config(cfg: AgentConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        # Windows / non-POSIX — best effort
        pass


def require_api_key(cfg: AgentConfig) -> str:
    if not cfg.api_key:
        raise RuntimeError("Not logged in. Run: route6 login <api_key>")
    return cfg.api_key
