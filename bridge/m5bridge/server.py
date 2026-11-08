"""HTTP API the Cardputer talks to.

All endpoints require `Authorization: Bearer <token>`. Voice and text commands become
jobs; the device polls `/api/jobs/{id}` until the job is done.

  GET  /api/status                 bridge name, busy flag, current session
  POST /api/voice?mode=&new=        body: raw PCM s16le mono, header X-Sample-Rate
  POST /api/text                   {"text": "...", "mode": "agent"|"desktop", "new": false}
  GET  /api/jobs/{id}              job state / transcript / reply / latest agent steps
  POST /api/jobs/{id}/confirm      run a voice command that waits in state "confirm"
  POST /api/jobs/{id}/approve      {"allow": true|false, "always": false} for state "approve"
  POST /api/jobs/{id}/cancel
  GET  /api/sessions               recent Claude Code sessions
  POST /api/sessions/select        {"id": "..."} or {"new": true}

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

log = logging.getLogger(__name__)

MAX_AUDIO_SECONDS = 60
MODES = ("agent", "desktop")
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
    def __init__(self, cfg: Config, agent, transcriber, desktop):
        self.cfg = cfg
        self.agent = agent
        self.transcriber = transcriber
        self.desktop = desktop
        self.jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self.agent_job: Job | None = None  # the job whose agent run is in progress
        self.always_allowed: set[str] = set()  # "A" on the device; until the bridge restarts

    def _new_job(self, mode: str) -> Job:
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
                if job.mode == "agent" and self.cfg.confirm_voice:
                    job.state = "confirm"
                    try:
                        await asyncio.wait_for(job.confirmed.wait(), CONFIRM_TIMEOUT)
                    except asyncio.TimeoutError:
                        job.state, job.error = "cancelled", "не подтверждено"
                        return
            else:
                job.transcript = text

            job.state = "running"
            if job.mode == "desktop":
                await self.desktop.send(text)
                job.reply = "Отправлено в Claude Desktop"
            else:
                self.agent_job = job
                try:
                    result = await self.agent.run(text, new_session=new, on_step=job.steps.append)
                finally:
                    if self.agent_job is job:
                        self.agent_job = None
                if job.state == "cancelled":  # cancelled while the agent was finishing
                    return
                job.session = result.session_id
                job.reply = self._clip(result.text or "(пустой ответ)")
                if result.is_error:
                    job.state, job.error = "error", job.reply
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
                "busy": self.agent.busy,
                "session": self.agent.current or "",
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
            if job.mode == "agent" and job.state in ("running", "approve"):
                await self.agent.cancel()
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

    async def sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"sessions": self.agent.list_sessions()[:10]})

    async def select_session(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        if data.get("new"):
            self.agent.new_session()
        else:
            try:
                self.agent.select_session(str(data.get("id", "")))
            except AgentError as exc:
                raise web.HTTPNotFound(text=str(exc))
        return web.json_response({"ok": True, "session": self.agent.current or ""})


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


def create_app(cfg: Config, agent, transcriber, desktop) -> web.Application:
    bridge = Bridge(cfg, agent, transcriber, desktop)
    internal_secret = secrets.token_urlsafe(24)
    host = "127.0.0.1" if cfg.host in ("", "0.0.0.0", "::") else cfg.host
    agent.approval = (f"http://{host}:{cfg.port}/internal/permission", internal_secret)
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
            web.post("/internal/permission", bridge.permission),
        ]
    )
    return app
