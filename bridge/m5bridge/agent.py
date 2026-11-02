"""Runs Claude Code agents headlessly (`claude -p`) and keeps track of their sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

SYSTEM_HINT = (
    "Команды приходят голосом или с клавиатуры карманного устройства M5Stack Cardputer "
    "с экраном 240x135. Отвечай кратко (до 5-6 строк), по-русски, без таблиц и markdown, "
    "если пользователь не просит иначе. Голосовая расшифровка может содержать ошибки — "
    "уточняй, если команда неоднозначна или опасна."
)


class AgentError(Exception):
    pass


class AgentCancelled(AgentError):
    pass


@dataclass
class SessionInfo:
    id: str
    title: str
    updated: float


@dataclass
class AgentResult:
    text: str
    session_id: str
    is_error: bool = False
    cost_usd: float | None = None


def windows_system_proxy() -> str:
    """The proxy set in Windows Internet Settings (e.g. by Hiddify in system-proxy mode), or ""."""
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        ) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except OSError:
        return ""
    # Either "host:port" or per-protocol "http=host:port;https=host:port".
    if "=" in server:
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = parts.get("https") or parts.get("http") or ""
    if server and "://" not in server:
        server = "http://" + server
    return server


def agent_env(proxy: str = "auto") -> dict[str, str]:
    """Environment for `claude -p` without a parent Claude session's variables.

    When the bridge is started from inside Claude Code / Claude Desktop it inherits
    CLAUDECODE, CLAUDE_CODE_* and CLAUDE_AGENT_SDK_*; with those the child CLI expects the
    host app to supply auth and reports "Not logged in" instead of using ~/.claude.

    Console programs ignore the Windows system proxy, so where Claude is only reachable
    through a VPN in system-proxy mode the CLI fails (ECONNRESET). proxy="auto" passes the
    system proxy on as HTTPS_PROXY unless one is already set; "none" leaves it alone; any
    other value is used as the proxy URL.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k == "CLAUDECODE" or k == "CLAUDE_PID" or k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")))
    }
    url = windows_system_proxy() if proxy == "auto" else ("" if proxy == "none" else proxy)
    if url and (proxy != "auto" or not (env.get("HTTPS_PROXY") or env.get("https_proxy"))):
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = url
        env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    return env


def find_claude(explicit: str = "") -> str:
    if explicit:
        return explicit
    # Prefer the native executable: running claude.cmd would route the prompt through cmd.exe.
    npm_exe = Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if npm_exe.exists():
        return str(npm_exe)
    for name in ("claude.exe", "claude"):
        found = shutil.which(name)
        if found and not found.lower().endswith((".cmd", ".bat", ".ps1")):
            return found
    raise AgentError("Claude Code CLI not found; set [agent].claude_bin in config.toml")


class ClaudeAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bin = find_claude(cfg.claude_bin)
        self.sessions: dict[str, SessionInfo] = {}
        self.current: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._load_state()

    # --- persistent state -------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for s in data.get("sessions", []):
            self.sessions[s["id"]] = SessionInfo(**s)
        self.current = data.get("current")

    def _save_state(self) -> None:
        recent = sorted(self.sessions.values(), key=lambda s: s.updated, reverse=True)[:20]
        self.sessions = {s.id: s for s in recent}
        data = {"current": self.current, "sessions": [asdict(s) for s in recent]}
        self.cfg.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- sessions ---------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        ordered = sorted(self.sessions.values(), key=lambda s: s.updated, reverse=True)
        return [{"id": s.id, "title": s.title, "current": s.id == self.current} for s in ordered]

    def new_session(self) -> None:
        self.current = None
        self._save_state()

    def select_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            raise AgentError("unknown session")
        self.current = session_id
        self._save_state()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # --- running ----------------------------------------------------------

    def _command(self, resume: str | None) -> list[str]:
        cmd = [self.bin, "-p", "--output-format", "json", "--append-system-prompt", SYSTEM_HINT]
        if self.cfg.permission_mode:
            cmd += ["--permission-mode", self.cfg.permission_mode]
        if self.cfg.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.cfg.allowed_tools)]
        if resume:
            if not SESSION_ID_RE.match(resume):
                raise AgentError("bad session id")
            cmd += ["--resume", resume]
        return cmd

    async def run(self, prompt: str, new_session: bool = False) -> AgentResult:
        prompt = prompt.strip()
        if not prompt:
            raise AgentError("empty prompt")
        async with self._lock:
            if new_session:
                self.current = None
            resume = self.current
            # The prompt goes through stdin so it can never be parsed as a CLI flag.
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(resume),
                cwd=self.cfg.workdir,
                env=agent_env(self.cfg.proxy),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            proc = self._proc
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self.cfg.agent_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise AgentError(f"timeout after {self.cfg.agent_timeout}s")
            finally:
                self._proc = None

            if getattr(proc, "_m5_cancelled", False):
                raise AgentCancelled("cancelled")
            result = self._parse(out, err, proc.returncode)
            if result.session_id and not result.is_error:
                info = self.sessions.get(result.session_id)
                title = info.title if info else prompt[:60]
                self.sessions[result.session_id] = SessionInfo(result.session_id, title, time.time())
                self.current = result.session_id
                self._save_state()
            return result

    def _parse(self, out: bytes, err: bytes, code: int | None) -> AgentResult:
        text = out.decode("utf-8", "replace").strip()
        try:
            data = json.loads(text.splitlines()[-1]) if text else {}
        except ValueError:
            data = {}
        if not data:
            detail = err.decode("utf-8", "replace").strip() or text or f"exit code {code}"
            raise AgentError(detail[-500:])
        reply = str(data.get("result") or "").strip()
        if not reply and data.get("subtype"):
            reply = f"[{data['subtype']}]"
        return AgentResult(
            text=reply,
            session_id=str(data.get("session_id") or ""),
            is_error=bool(data.get("is_error")),
            cost_usd=data.get("total_cost_usd"),
        )

    async def cancel(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        proc._m5_cancelled = True  # type: ignore[attr-defined]
        proc.kill()
        return True
