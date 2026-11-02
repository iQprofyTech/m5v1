"""Bridge configuration: bridge/config.toml (created on first run, git-ignored)."""

from __future__ import annotations

import platform
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BRIDGE_DIR / "config.toml"
DEFAULT_STATE_PATH = BRIDGE_DIR / "state.json"

TEMPLATE = """\
# M5 Cardputer bridge config. Keep this file private: the token grants control of this PC.

[server]
host = "0.0.0.0"          # listen on all interfaces (LAN). Use "127.0.0.1" if only a tunnel should reach it.
port = 8765
discovery_port = 8766     # UDP port the Cardputer broadcasts to find the bridge on the LAN
name = "{name}"
token = "{token}"

[speech]
model = "small"           # faster-whisper model: tiny / base / small / medium / large-v3
device = "auto"           # auto / cpu / cuda
compute_type = "int8"
language = "ru"

[agent]
claude_bin = ""           # empty = find claude / claude.exe automatically
workdir = "{workdir}"     # folder Claude Code agents work in
permission_mode = "default"   # default / acceptEdits / plan / bypassPermissions
allowed_tools = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
timeout_sec = 900
max_reply_chars = 3000
proxy = "auto"            # auto = Windows system proxy (e.g. Hiddify) / none / http://host:port

[desktop]
window_title = "Claude"   # title of the Claude Desktop window to type into
press_enter = true
"""


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    discovery_port: int = 8766
    name: str = field(default_factory=platform.node)
    token: str = ""

    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute: str = "int8"
    language: str = "ru"

    claude_bin: str = ""
    workdir: str = field(default_factory=lambda: str(Path.home()))
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=list)
    agent_timeout: int = 900
    max_reply_chars: int = 3000
    proxy: str = "auto"

    desktop_window_title: str = "Claude"
    desktop_press_enter: bool = True

    state_path: Path = DEFAULT_STATE_PATH


def _write_template(path: Path) -> None:
    text = TEMPLATE.format(
        name=platform.node(),
        token=secrets.token_urlsafe(24),
        workdir=str(Path.home()).replace("\\", "\\\\"),
    )
    path.write_text(text, encoding="utf-8")


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        _write_template(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    server = data.get("server", {})
    speech = data.get("speech", {})
    agent = data.get("agent", {})
    desktop = data.get("desktop", {})

    cfg = Config(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8765)),
        discovery_port=int(server.get("discovery_port", 8766)),
        name=server.get("name") or platform.node(),
        token=server.get("token", ""),
        whisper_model=speech.get("model", "small"),
        whisper_device=speech.get("device", "auto"),
        whisper_compute=speech.get("compute_type", "int8"),
        language=speech.get("language", "ru"),
        claude_bin=agent.get("claude_bin", ""),
        workdir=agent.get("workdir") or str(Path.home()),
        permission_mode=agent.get("permission_mode", "default"),
        allowed_tools=list(agent.get("allowed_tools", [])),
        agent_timeout=int(agent.get("timeout_sec", 900)),
        max_reply_chars=int(agent.get("max_reply_chars", 3000)),
        proxy=str(agent.get("proxy", "auto")),
        desktop_window_title=desktop.get("window_title", "Claude"),
        desktop_press_enter=bool(desktop.get("press_enter", True)),
        state_path=path.parent / "state.json",
    )
    if len(cfg.token) < 16:
        raise ValueError(f"{path}: [server].token must be at least 16 characters")
    return cfg
