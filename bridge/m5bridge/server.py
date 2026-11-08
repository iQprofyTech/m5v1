"""HTTP API the Cardputer talks to.

All endpoints require `Authorization: Bearer <token>`. Voice and text commands become
jobs; the device polls `/api/jobs/{id}` until the job is done.

  GET  /api/status                 bridge name, busy flag, current session
  POST /api/voice?mode=&new=        body: raw PCM s16le mono, header X-Sample-Rate
  POST /api/text                   {"text": "...", "mode": MODE, "new": false}
                                   MODE: claude (= agent) | codex | window | desktop
                                   | type (dictation: typed into the active PC window)
  GET  /api/jobs/{id}              job state / transcript / reply / latest agent steps
  POST /api/jobs/{id}/confirm      run a voice command that waits in state "confirm"
  POST /api/jobs/{id}/approve      {"allow": true|false, "always": false} for state "approve"
  POST /api/jobs/{id}/cancel
  GET  /api/sessions?agent=        recent sessions of claude (default) or codex
  POST /api/sessions/select?agent= {"id": "..."} or {"new": true}
  GET  /api/windows                open windows of the PC and the selected one
  POST /api/windows/select         {"id": hwnd}: target of mode "window"
  POST /api/input                  {"text"} | {"key", "mods", "repeat"} | {"dx", "dy", "click", "scroll"}
  GET  /api/commands               quick PC commands (built-in + config.toml [commands])
  POST /api/commands/run           {"name": "..."}

  POST /internal/permission        from approval_mcp (loopback + per-run secret): the agent
                                   asks to use a tool; held until the device answers
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import itertools
import logging
import secrets
import time
import wave
from dataclasses import dataclass, field

import numpy as np
from aiohttp import web

from . import __version__
from .agent import AgentCancelled, AgentError, describe_tool
from .config import Config
from . import pc

log = logging.getLogger(__name__)

MAX_AUDIO_SECONDS = 60
MODES = ("agent", "claude", "codex", "desktop", "window", "type")
AGENT_MODES = ("claude", "codex")
CONFIRM_TIMEOUT = 120
APPROVAL_TIMEOUT = 180
STEPS_SENT = 8  # the device only shows the latest few actions


@dataclass
class Job:
    id: str
    mode: str
    state: str = "queued"  # queued, transcribing, confirm, running, approve, done, error, cancelled
    transcript: str = ""
    reply: str = ""
    error: str = ""
    session: str = ""
    steps: list[str] = field(default_factory=list)  # agent tool calls, e.g. "Bash: git status"
    created: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    confirmed: asyncio.Event = field(default_factory=asyncio.Event)
    approval: str = ""  # tool call waiting for the device's answer, e.g. "Gmail: list_drafts"
    approval_answer: asyncio.Future | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "state": self.state,
            "transcript": self.transcript,
            "reply": self.reply,
            "error": self.error,
            "session": self.session,
            "step_count": len(self.steps),
            "steps": self.steps[-STEPS_SENT:],
            "approval": self.approval,
        }


class Bridge:
    def __init__(self, cfg: Config, agents, transcriber, desktop, chat=None):
        self.cfg = cfg
        # {"claude": ClaudeAgent, "codex": CodexAgent}; a single agent means Claude only.
        self.agents = agents if isinstance(agents, dict) else {"claude": agents}
        self.agent = self.agents.get("claude") or next(iter(self.agents.values()))
        self.chat = chat
        self.transcriber = transcriber
        self.desktop = desktop
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self.agent_job: Job | None = None  # the job whose agent run is in progress
        self.always_allowed: set[str] = set()  # "A" on the device; until the bridge restarts

    def _new_job(self, mode: str) -> Job:
        mode = "claude" if mode == "agent" else mode
        job = Job(id=f"{int(time.time()) % 100000}-{next(self._ids)}", mode=mode)
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-50]:
            self.jobs.pop(old.id, None)
        return job

    def _clip(self, text: str) -> str:
        limit = self.cfg.max_reply_chars
        return text if len(text) <= limit else text[: limit - 1] + "…"

    async def _process(self, job: Job, *, pcm: bytes | None, rate: int, text: str, new: bool) -> None:
        try:
            if pcm is not None:
                job.state = "transcribing"
                text = await self.transcriber.transcribe(pcm, rate)
                job.transcript = text
                if not text:
                    job.state, job.error = "error", "речь не распознана"
                    return
                # A misheard command must not reach an agent that can change files and run
                # programs: show the transcript on the device and wait for Enter.
                # Desktop mode also reaches an agent (Claude Code in Claude Desktop).
                if self.cfg.confirm_voice:
                    job.state = "confirm"
                    try:
                        await asyncio.wait_for(job.confirmed.wait(), CONFIRM_TIMEOUT)
                    except asyncio.TimeoutError:
                        job.state, job.error = "cancelled", "не подтверждено"
                        return
            else:
                job.transcript = text

            job.state = "running"
            if job.mode == "type":
                await asyncio.to_thread(pc.type_text, text)
                job.reply = "Напечатано на ПК"
            elif job.mode == "window":
                if self.chat is None:
                    raise pc.PcError("управление окнами недоступно на этом ПК")
                self._audit(f"> [window {self.chat.title[:30]}] {text}")
                reply = await self.chat.ask(text, on_step=job.steps.append)
                job.reply = self._clip(reply) if reply else "Отправлено в окно; новый текст в нём не появился"
                self._audit(f"  = [window] {reply}")
            elif job.mode == "desktop" or (job.mode == "claude" and self.cfg.agent_backend == "desktop"):
                since = time.time()
                await self.desktop.send(text)
                job.reply = "Отправлено в Claude Desktop"
                wait_reply = getattr(self.desktop, "wait_reply", None)
                if wait_reply is not None:
                    self._audit(f"> [desktop] {text}")
                    reply = await wait_reply(text, since, on_step=job.steps.append)
                    if reply is None:
                        job.reply = (
                            "Отправлено в Claude Desktop, но ответ прочитать нельзя: в окне открыт обычный "
                            "чат. Откройте вкладку Code с сессией для голоса и повторите."
                        )
                    elif not reply:
                        job.reply = "Отправлено в Claude Desktop, но ответа нет: сессия была занята другой задачей"
                    else:
                        job.reply = self._clip(reply or "(пустой ответ)")
                        self._audit(f"  = [desktop] {reply}")
            else:
                agent = self.agents.get(job.mode)
                if agent is None:
                    raise AgentError(f"{job.mode}: агент не найден на ПК")
                if job.mode == "claude":
                    self.agent_job = job  # permission prompts come from Claude Code only
                try:
                    result = await agent.run(text, new_session=new, on_step=job.steps.append)
                finally:
                    if self.agent_job is job:
                        self.agent_job = None
                if job.state == "cancelled":  # cancelled while the agent was finishing
                    return
                job.session = result.session_id
                job.reply = self._clip(result.text or "(пустой ответ)")
                if result.is_error:
                    job.state, job.error = "error", _explain_agent_error(job.reply)
                    return
            job.state = "done"
        except AgentCancelled:
            job.state = "cancelled"
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as exc:  # reported to the device, not raised
            log.exception("job %s failed", job.id)
            job.state, job.error = "error", str(exc)[:300] or exc.__class__.__name__

    def _save_last_clip(self, pcm: bytes, rate: int) -> None:
        """Keep the latest recording as bridge/logs/last_voice.wav for checking the microphone."""
        try:
            path = self.cfg.state_path.parent / "logs" / "last_voice.wav"
            path.parent.mkdir(exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm)
        except OSError:
            log.exception("could not save last voice clip")

    def _start(self, job: Job, **kwargs) -> None:
        job.task = asyncio.create_task(self._process(job, **kwargs))

    # --- handlers ---------------------------------------------------------

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "name": self.cfg.name,
                "version": __version__,
                "busy": any(a.busy for a in self.agents.values()),
                "session": self.agent.current or "",
                "agents": list(self.agents),
                "window": self.chat.title if self.chat and self.chat.hwnd else "",
            }
        )

    async def voice(self, request: web.Request) -> web.Response:
        mode = request.query.get("mode", "agent")
        if mode not in MODES:
            raise web.HTTPBadRequest(text="bad mode")
        try:
            rate = int(request.headers.get("X-Sample-Rate", "16000"))
        except ValueError:
            raise web.HTTPBadRequest(text="bad X-Sample-Rate")
        if not 8000 <= rate <= 48000:
            raise web.HTTPBadRequest(text="bad X-Sample-Rate")

        limit = MAX_AUDIO_SECONDS * rate * 2
        chunks, size = [], 0
        async for chunk in request.content.iter_chunked(8192):
            size += len(chunk)
            if size > limit:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
            chunks.append(chunk)
        pcm = b"".join(chunks)
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
        log.info("voice: %.1fs of audio, peak=%d rms=%.0f, mode=%s", samples.size / rate, peak, rms, mode)
        self._save_last_clip(pcm, rate)

        job = self._new_job(mode)
        self._start(job, pcm=pcm, rate=rate, text="", new=request.query.get("new") == "1")
        return web.json_response({"job": job.id})

    async def text(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        text = str(data.get("text", "")).strip()
        mode = data.get("mode", "agent")
        if not text or mode not in MODES:
            raise web.HTTPBadRequest(text="text and mode required")
        job = self._new_job(mode)
        self._start(job, pcm=None, rate=0, text=text[:4000], new=bool(data.get("new")))
        return web.json_response({"job": job.id})

    def _job(self, request: web.Request) -> Job:
        job = self.jobs.get(request.match_info["id"])
        if job is None:
            raise web.HTTPNotFound(text="no such job")
        return job

    async def job(self, request: web.Request) -> web.Response:
        return web.json_response(self._job(request).to_json())

    async def confirm(self, request: web.Request) -> web.Response:
        job = self._job(request)
        if job.state != "confirm":
            raise web.HTTPConflict(text=f"job is {job.state}")
        job.confirmed.set()
        return web.json_response(job.to_json())

    async def cancel(self, request: web.Request) -> web.Response:
        job = self._job(request)
        if job.state in ("queued", "transcribing", "confirm", "running", "approve"):
            self._answer(job, False)
            if job.mode in AGENT_MODES and job.state in ("running", "approve") and job.mode in self.agents:
                await self.agents[job.mode].cancel()
            elif job.task:
                job.task.cancel()
            job.state = "cancelled"
        return web.json_response(job.to_json())

    @staticmethod
    def _answer(job: Job, allow: bool) -> None:
        if job.approval_answer is not None and not job.approval_answer.done():
            job.approval_answer.set_result(allow)

    async def approve(self, request: web.Request) -> web.Response:
        job = self._job(request)
        if job.state != "approve":
            raise web.HTTPConflict(text=f"job is {job.state}")
        try:
            data = await request.json()
        except ValueError:
            data = {}
        allow = bool(data.get("allow"))
        if allow and data.get("always") and job.approval:
            self.always_allowed.add(job.approval)
        self._answer(job, allow)
        return web.json_response(job.to_json())

    def _audit(self, text: str) -> None:
        audit = getattr(self.agent, "audit", None)
        if audit:
            audit(text)

    async def permission(self, request: web.Request) -> web.Response:
        """Called by approval_mcp while `claude -p` waits; answers {"allow": bool}."""
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        tool = describe_tool(data.get("tool_name", "?"), data.get("input") or {})
        job = self.agent_job
        if job is None or job.state not in ("running", "approve"):
            self._audit(f"  ? {tool} -> denied (no active job)")
            return web.json_response({"allow": False})
        if tool in self.always_allowed:
            self._audit(f"  ? {tool} -> allowed (always)")
            return web.json_response({"allow": True})
        # One question at a time: a parallel tool call waits for the first answer.
        while job.approval_answer is not None and not job.approval_answer.done():
            await asyncio.shield(job.approval_answer)
        if job.state not in ("running", "approve"):
            return web.json_response({"allow": False})
        job.approval_answer = asyncio.get_running_loop().create_future()
        job.approval, job.state = tool, "approve"
        try:
            allow = await asyncio.wait_for(asyncio.shield(job.approval_answer), APPROVAL_TIMEOUT)
            self._audit(f"  ? {tool} -> {'allowed' if allow else 'denied'} on the device")
        except asyncio.TimeoutError:
            allow = False
            self._answer(job, False)
            self._audit(f"  ? {tool} -> no answer in {APPROVAL_TIMEOUT}s, denied")
        finally:
            job.approval = ""
            if job.state == "approve":
                job.state = "running"
        return web.json_response({"allow": allow})

    def _agent_of(self, request: web.Request):
        name = request.query.get("agent", "claude")
        name = "claude" if name == "agent" else name
        if name not in self.agents:
            raise web.HTTPNotFound(text=f"no agent {name}")
        return self.agents[name]

    async def sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"sessions": self._agent_of(request).list_sessions()[:10]})

    async def select_session(self, request: web.Request) -> web.Response:
        agent = self._agent_of(request)
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        if data.get("new"):
            agent.new_session()
        else:
            try:
                agent.select_session(str(data.get("id", "")))
            except AgentError as exc:
                raise web.HTTPNotFound(text=str(exc))
        return web.json_response({"ok": True, "session": agent.current or ""})

    # --- PC control ---------------------------------------------------------

    async def windows(self, request: web.Request) -> web.Response:
        wins = await asyncio.to_thread(pc.list_windows)
        selected = self.chat.hwnd if self.chat else None
        return web.json_response({"windows": wins[:30], "selected": selected or 0})

    async def select_window(self, request: web.Request) -> web.Response:
        if self.chat is None:
            raise web.HTTPNotImplemented(text="no window control")
        try:
            data = await request.json()
            hwnd = int(data.get("id", 0))
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="bad id")
        try:
            win = await asyncio.to_thread(self.chat.select, hwnd)
        except pc.PcError as exc:
            raise web.HTTPNotFound(text=str(exc))
        return web.json_response({"ok": True, "window": win})

    async def input(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        try:
            if data.get("text"):
                await asyncio.to_thread(pc.type_text, str(data["text"])[:2000])
            elif data.get("key"):
                mods = [str(m) for m in data.get("mods") or []]
                await asyncio.to_thread(pc.press, str(data["key"]), mods, int(data.get("repeat", 1)))
            else:
                await asyncio.to_thread(
                    pc.mouse,
                    max(-2000, min(2000, int(data.get("dx", 0)))),
                    max(-2000, min(2000, int(data.get("dy", 0)))),
                    str(data.get("click", "")),
                    max(-20, min(20, int(data.get("scroll", 0)))),
                )
        except pc.PcError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"ok": True})

    async def commands(self, request: web.Request) -> web.Response:
        return web.json_response({"commands": pc.command_names(self.cfg)})

    async def run_command(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        name = str(data.get("name", ""))
        try:
            await asyncio.to_thread(pc.run_command, self.cfg, name)
        except pc.PcError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        self._audit(f"> [pc] {name}")
        return web.json_response({"ok": True, "name": name})


def _explain_agent_error(text: str) -> str:
    """Short, actionable text for the 240x135 screen instead of a raw API error."""
    if "Verification Portal" in text or "can't receive replies" in text:
        return (
            "Консольный Claude Code заблокирован для этого аккаунта: нужна проверка на "
            "portal.anthropic.com/verify. Пока работает режим DESK (Tab)."
        )
    if "Not logged in" in text or "/login" in text:
        return "Консольный Claude Code не авторизован: выполните на ПК claude auth login"
    return text


def _is_loopback(addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def _auth_middleware(token: str, internal_secret: str):
    expected = f"Bearer {token}".encode()
    internal = f"Bearer {internal_secret}".encode()

    @web.middleware
    async def auth(request: web.Request, handler):
        got = request.headers.get("Authorization", "").encode()
        if request.path.startswith("/internal/"):
            # Only the approval helper started by this bridge: loopback plus a per-run secret,
            # never the device token (that one may travel over a tunnel).
            if not (_is_loopback(request.remote) and hmac.compare_digest(got, internal)):
                raise web.HTTPForbidden(text="internal endpoint")
            return await handler(request)
        if not hmac.compare_digest(got, expected):
            log.warning("rejected request from %s to %s", request.remote, request.path)
            raise web.HTTPUnauthorized(text="bad token")
        return await handler(request)

    return auth


INTERNAL_SECRET_KEY = web.AppKey("internal_secret", str)
BRIDGE_KEY = web.AppKey("bridge", Bridge)


def create_app(cfg: Config, agents, transcriber, desktop, chat=None) -> web.Application:
    bridge = Bridge(cfg, agents, transcriber, desktop, chat)
    internal_secret = secrets.token_urlsafe(24)
    host = "127.0.0.1" if cfg.host in ("", "0.0.0.0", "::") else cfg.host
    bridge.agent.approval = (f"http://{host}:{cfg.port}/internal/permission", internal_secret)
    app = web.Application(
        middlewares=[_auth_middleware(cfg.token, internal_secret)], client_max_size=4 * 1024 * 1024
    )
    app[BRIDGE_KEY] = bridge
    app[INTERNAL_SECRET_KEY] = internal_secret
    app.add_routes(
        [
            web.get("/api/status", bridge.status),
            web.post("/api/voice", bridge.voice),
            web.post("/api/text", bridge.text),
            web.get("/api/jobs/{id}", bridge.job),
            web.post("/api/jobs/{id}/confirm", bridge.confirm),
            web.post("/api/jobs/{id}/approve", bridge.approve),
            web.post("/api/jobs/{id}/cancel", bridge.cancel),
            web.get("/api/sessions", bridge.sessions),
            web.post("/api/sessions/select", bridge.select_session),
            web.get("/api/windows", bridge.windows),
            web.post("/api/windows/select", bridge.select_window),
            web.post("/api/input", bridge.input),
            web.get("/api/commands", bridge.commands),
            web.post("/api/commands/run", bridge.run_command),
            web.post("/internal/permission", bridge.permission),
        ]
    )
    return app
