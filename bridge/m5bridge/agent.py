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
    "с экраном 240x135: пользователь управляет этим компьютером удалённо и не видит экран ПК. "
    "Выполняй задания сам, а в конце отвечай кратко (до 5-6 строк), по-русски, без таблиц и "
    "markdown: что сделано и где лежит результат. Голосовая расшифровка может содержать "
    "ошибки — если команда неоднозначна или может что-то необратимо удалить, сначала уточни."
)


_STEP_ARG = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Task": "description",
}


def describe_tool(name: str, args) -> str:
    """'Bash: git status', 'Gmail: list_drafts' — short enough for the Cardputer screen."""
    name = str(name or "?")
    if name.startswith("mcp__"):
        # mcp__claude_ai_Gmail__list_drafts -> Gmail: list_drafts
        server, _, tool = name[5:].partition("__")
        server = server.removeprefix("claude_ai_").removeprefix("plugin_")
        text = f"{server}: {tool}" if tool else server
    else:
        arg = args.get(_STEP_ARG.get(name, ""), "") if isinstance(args, dict) else ""
        text = f"{name}: {arg}" if arg else name
    return " ".join(str(text).split())[:160]


def describe_steps(event: dict) -> list[str]:
    """One short line per tool call in a stream-json event, e.g. 'Bash: git status'."""
    if event.get("type") != "assistant":
        return []
    return [
        describe_tool(block.get("name"), block.get("input") or {})
        for block in event.get("message", {}).get("content", [])
        if block.get("type") == "tool_use"
    ]


APPROVAL_TOOL = "mcp__m5__approve"
BRIDGE_DIR = Path(__file__).resolve().parent.parent


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




def find_codex(explicit: str = "") -> str:
    """codex CLI: explicit path, PATH, or the copy bundled with the Codex desktop app."""
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found and not found.lower().endswith((".cmd", ".bat", ".ps1")):
        return found
    if sys.platform == "win32":
        # The Codex desktop app (Microsoft Store package) ships the CLI. Its folder under
        # WindowsApps cannot be listed, but the package knows where it is installed.
        import subprocess

        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for location in out.splitlines():
            exe = Path(location.strip()) / "app" / "resources" / "codex.exe"
            if location.strip() and exe.exists():
                return str(exe)
    raise AgentError("Codex CLI not found; install the Codex app or set [codex].bin in config.toml")


class BaseAgent:
    """Runs one headless agent CLI at a time, keeps its sessions and an audit log."""

    name = "agent"

    def __init__(self, cfg: Config, state_path: Path):
        self.cfg = cfg
        self.state_path = state_path
        self.sessions: dict[str, SessionInfo] = {}
        self.current: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._load_state()

    # --- persistent state -------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for s in data.get("sessions", []):
            self.sessions[s["id"]] = SessionInfo(**s)
        self.current = data.get("current")

    def _save_state(self) -> None:
        recent = sorted(self.sessions.values(), key=lambda s: s.updated, reverse=True)[:20]
        self.sessions = {s.id: s for s in recent}
        data = {"current": self.current, "sessions": [asdict(s) for s in recent]}
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

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

    # --- audit log --------------------------------------------------------

    def audit(self, text: str) -> None:
        self._audit(text)

    def _audit(self, text: str) -> None:
        """Append to bridge/logs/agent.log: every prompt, tool call and result, readable as text."""
        try:
            path = self.cfg.state_path.parent / "logs" / "agent.log"
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            log.exception("could not write agent.log")

    # --- hooks for each CLI -----------------------------------------------

    def _command(self, resume: str | None) -> list[str]:
        raise NotImplementedError

    def _prompt(self, prompt: str, resume: str | None) -> str:
        return prompt

    def _on_event(self, event: dict, ctx: dict) -> list[str]:
        """Update ctx from one JSON event; return the tool calls it describes."""
        raise NotImplementedError

    def _parse(self, ctx: dict, err: bytes, code: int | None) -> AgentResult:
        raise NotImplementedError

    # --- running ----------------------------------------------------------

    async def run(self, prompt: str, new_session: bool = False, on_step=None) -> AgentResult:
        prompt = prompt.strip()
        if not prompt:
            raise AgentError("empty prompt")
        async with self._lock:
            if new_session:
                self.current = None
            resume = self.current
            if resume and not SESSION_ID_RE.match(resume):
                raise AgentError("bad session id")
            self._audit(f"> [{self.name} {(resume or 'new')[:8]}] {prompt}")
            # The prompt goes through stdin so it can never be parsed as a CLI flag.
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(resume),
                cwd=self.cfg.workdir,
                env=agent_env(self.cfg.proxy),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,  # tool results (file contents) arrive as single long lines
            )
            proc = self._proc
            proc.stdin.write(self._prompt(prompt, resume).encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            stderr_task = asyncio.create_task(proc.stderr.read())
            ctx: dict = {}
            try:
                async with asyncio.timeout(self.cfg.agent_timeout):
                    while line := await proc.stdout.readline():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        for step in self._on_event(event, ctx):
                            self._audit(f"    {step}")
                            if on_step:
                                on_step(step)
                    await proc.wait()
            except TimeoutError:
                proc.kill()
                await proc.wait()
                self._audit(f"  ! timeout after {self.cfg.agent_timeout}s")
                raise AgentError(f"timeout after {self.cfg.agent_timeout}s")
            finally:
                self._proc = None
            err = await stderr_task

            if getattr(proc, "_m5_cancelled", False):
                self._audit("  ! cancelled")
                raise AgentCancelled("cancelled")
            result = self._parse(ctx, err, proc.returncode)
            cost = f" (${result.cost_usd:.3f})" if result.cost_usd else ""
            self._audit(f"  {'!' if result.is_error else '='} [{result.session_id[:8]}]{cost} {result.text}")
            if result.session_id and not result.is_error:
                info = self.sessions.get(result.session_id)
                title = info.title if info else prompt[:60]
                self.sessions[result.session_id] = SessionInfo(result.session_id, title, time.time())
                self.current = result.session_id
                self._save_state()
            return result

    async def cancel(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        proc._m5_cancelled = True  # type: ignore[attr-defined]
        proc.kill()
        return True


class ClaudeAgent(BaseAgent):
    """Claude Code: `claude -p --output-format stream-json`."""

    name = "claude"

    def __init__(self, cfg: Config):
        self.bin = find_claude(cfg.claude_bin)
        # (url, secret) of the bridge's permission endpoint; set by create_app.
        self.approval: tuple[str, str] | None = None
        super().__init__(cfg, cfg.state_path)

    def _command(self, resume: str | None) -> list[str]:
        # stream-json emits every tool call as it happens, so the device and the audit log
        # can show what the agent is doing, not only its final answer.
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--append-system-prompt", SYSTEM_HINT]
        if self.cfg.permission_mode:
            cmd += ["--permission-mode", self.cfg.permission_mode]
        if self.cfg.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.cfg.allowed_tools)]
        if self.cfg.disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self.cfg.disallowed_tools)]
        for extra in self.cfg.add_dirs:
            cmd += ["--add-dir", extra]
        if self.approval and self.cfg.ask_permissions:
            # Tools that are neither allowed nor denied are asked about on the Cardputer
            # instead of being refused because nobody can click "Allow" on the PC.
            url, secret = self.approval
            server = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "m5bridge.approval_mcp"],
                "env": {
                    "M5_APPROVAL_URL": url,
                    "M5_APPROVAL_SECRET": secret,
                    "PYTHONPATH": str(BRIDGE_DIR),
                    "PYTHONIOENCODING": "utf-8",
                },
            }
            cmd += ["--mcp-config", json.dumps({"mcpServers": {"m5": server}})]
            cmd += ["--permission-prompt-tool", APPROVAL_TOOL]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def _on_event(self, event: dict, ctx: dict) -> list[str]:
        if event.get("type") == "result":
            ctx["final"] = event
        return describe_steps(event)

    def _parse(self, ctx: dict, err: bytes, code: int | None) -> AgentResult:
        data = ctx.get("final") or {}
        if not data:
            detail = err.decode("utf-8", "replace").strip() or f"exit code {code}"
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


CODEX_HINT = (
    "[Команда пришла голосом или с клавиатуры карманного устройства с экраном 240x135; "
    "пользователь не видит экран ПК. Выполни задание сам, а в конце ответь кратко (до 5-6 строк), "
    "по-русски, без таблиц и markdown. Если команда неоднозначна или может что-то необратимо "
    "удалить, сначала уточни.]\n\n"
)


def describe_codex_item(item: dict) -> str | None:
    """'Bash: git status', 'Edit: main.py', 'Gmail: list_drafts' for a Codex thread item."""
    kind = item.get("type")
    if kind == "command_execution":
        return describe_tool("Bash", {"command": item.get("command", "")})
    if kind == "file_change":
        paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
        return describe_tool("Edit", {"file_path": ", ".join(p for p in paths if p)})
    if kind == "mcp_tool_call":
        return describe_tool(f"mcp__{item.get('server', '?')}__{item.get('tool', '?')}", {})
    if kind == "web_search":
        return describe_tool("WebSearch", {"query": item.get("query", "")})
    return None


class CodexAgent(BaseAgent):
    """OpenAI Codex: `codex exec --json` (sessions resumed with `codex exec resume`)."""

    name = "codex"

    def __init__(self, cfg: Config):
        self.bin = find_codex(cfg.codex_bin)
        super().__init__(cfg, cfg.state_path.with_name("state_codex.json"))

    def _command(self, resume: str | None) -> list[str]:
        cmd = [self.bin, "exec"]
        if resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{self.cfg.codex_sandbox}"']
        cmd += list(self.cfg.codex_args)
        if resume:
            cmd.append(resume)
        return cmd + ["-"]

    def _prompt(self, prompt: str, resume: str | None) -> str:
        return prompt if resume else CODEX_HINT + prompt

    def _on_event(self, event: dict, ctx: dict) -> list[str]:
        kind = event.get("type")
        if kind == "thread.started":
            ctx["session"] = str(event.get("thread_id") or "")
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and kind == "item.completed":
                ctx["text"] = str(item.get("text") or "")
            seen = ctx.setdefault("seen", set())
            step = describe_codex_item(item)
            if step and item.get("id") not in seen:
                seen.add(item.get("id"))
                return [step]
        elif kind == "turn.completed":
            ctx["done"] = True
        elif kind in ("turn.failed", "error"):
            err = event.get("error") or {}
            ctx["error"] = str(err.get("message") if isinstance(err, dict) else err or event.get("message") or kind)
        return []

    def _parse(self, ctx: dict, err: bytes, code: int | None) -> AgentResult:
        session = ctx.get("session", "")
        if ctx.get("error") and not ctx.get("text"):
            return AgentResult(text=ctx["error"][:500], session_id=session, is_error=True)
        if not ctx.get("done") and not ctx.get("text"):
            detail = err.decode("utf-8", "replace").strip() or f"exit code {code}"
            raise AgentError(detail[-500:])
        return AgentResult(text=ctx.get("text", "").strip(), session_id=session)
