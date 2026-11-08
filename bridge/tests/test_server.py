import asyncio

import numpy as np
import pytest

from m5bridge.agent import AgentResult
from m5bridge.config import Config
from m5bridge.server import create_app
from m5bridge.stt import pcm16_to_float

TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeAgent:
    def __init__(self):
        self.prompts = []
        self.current = None
        self.busy = False

    async def run(self, prompt, new_session=False, on_step=None):
        if on_step:
            on_step("Bash: echo hi")
        self.prompts.append((prompt, new_session))
        self.current = "11111111-2222-3333-4444-555555555555"
        return AgentResult(text=f"ok: {prompt}", session_id=self.current)

    async def cancel(self):
        return False

    def list_sessions(self):
        return [{"id": self.current, "title": "t", "current": True}] if self.current else []

    def new_session(self):
        self.current = None

    def select_session(self, session_id):
        self.current = session_id


class FakeTranscriber:
    async def transcribe(self, pcm, rate):
        return "привет" if pcm else ""


class FakeDesktop:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


@pytest.fixture
def parts():
    return Config(token=TOKEN, confirm_voice=False), FakeAgent(), FakeTranscriber(), FakeDesktop()


@pytest.fixture
async def client(aiohttp_client, parts):
    return await aiohttp_client(create_app(*parts))


async def wait_job(client, job_id):
    for _ in range(50):
        resp = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
        data = await resp.json()
        if data["state"] in ("done", "error", "cancelled"):
            return data
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


async def test_requires_token(client):
    assert (await client.get("/api/status")).status == 401
    bad = {"Authorization": "Bearer wrong"}
    assert (await client.get("/api/status", headers=bad)).status == 401
    assert (await client.get("/api/status", headers=AUTH)).status == 200


async def test_text_to_agent(client, parts):
    resp = await client.post("/api/text", json={"text": "сколько времени", "mode": "agent"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "done"
    assert job["reply"] == "ok: сколько времени"
    assert parts[1].prompts == [("сколько времени", False)]


async def test_voice_to_desktop(client, parts):
    pcm = (np.sin(np.arange(16000) / 5) * 8000).astype("<i2").tobytes()
    resp = await client.post(
        "/api/voice?mode=desktop", data=pcm, headers={**AUTH, "X-Sample-Rate": "16000"}
    )
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "done"
    assert job["transcript"] == "привет"
    assert parts[3].sent == ["привет"]


async def test_empty_voice_is_error(client):
    resp = await client.post("/api/voice?mode=agent", data=b"", headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "error"


async def test_new_session_flag(client, parts):
    resp = await client.post("/api/text", json={"text": "x", "mode": "agent", "new": True}, headers=AUTH)
    await wait_job(client, (await resp.json())["job"])
    assert parts[1].prompts[-1] == ("x", True)


async def test_bad_mode_rejected(client):
    resp = await client.post("/api/text", json={"text": "x", "mode": "shell"}, headers=AUTH)
    assert resp.status == 400


async def test_agent_steps_reported(client):
    resp = await client.post("/api/text", json={"text": "x", "mode": "agent"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["step_count"] == 1
    assert job["steps"] == ["Bash: echo hi"]


@pytest.fixture
async def confirm_client(aiohttp_client, parts):
    cfg, *rest = parts
    cfg.confirm_voice = True
    return await aiohttp_client(create_app(cfg, *rest))


async def wait_state(client, job_id, state):
    for _ in range(50):
        data = await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).json()
        if data["state"] == state:
            return data
        await asyncio.sleep(0.02)
    raise AssertionError(f"job never reached {state}")


async def test_voice_agent_waits_for_confirm(confirm_client, parts):
    pcm = (np.ones(16000) * 3000).astype("<i2").tobytes()
    resp = await confirm_client.post("/api/voice?mode=agent", data=pcm, headers=AUTH)
    job_id = (await resp.json())["job"]
    job = await wait_state(confirm_client, job_id, "confirm")
    assert job["transcript"] == "привет"
    assert parts[1].prompts == []  # nothing ran yet

    assert (await confirm_client.post(f"/api/jobs/{job_id}/confirm", headers=AUTH)).status == 200
    job = await wait_job(confirm_client, job_id)
    assert job["state"] == "done"
    assert parts[1].prompts == [("привет", False)]


async def test_voice_agent_cancel_while_confirming(confirm_client, parts):
    pcm = (np.ones(16000) * 3000).astype("<i2").tobytes()
    resp = await confirm_client.post("/api/voice?mode=agent", data=pcm, headers=AUTH)
    job_id = (await resp.json())["job"]
    await wait_state(confirm_client, job_id, "confirm")
    await confirm_client.post(f"/api/jobs/{job_id}/cancel", headers=AUTH)
    job = await wait_job(confirm_client, job_id)
    assert job["state"] == "cancelled"
    assert parts[1].prompts == []


async def test_desktop_backend_routes_agent_mode(aiohttp_client, parts):
    cfg, agent, transcriber, desktop = parts
    cfg.agent_backend = "desktop"
    client = await aiohttp_client(create_app(cfg, agent, transcriber, desktop))
    resp = await client.post("/api/text", json={"text": "статус", "mode": "agent"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "done"
    assert desktop.sent == ["статус"]
    assert agent.prompts == []


def test_pcm_resample_and_normalise():
    pcm = (np.ones(8000) * 4000).astype("<i2").tobytes()
    audio = pcm16_to_float(pcm, 8000)
    assert audio.size == 16000
    assert 0.8 < float(audio.max()) <= 0.91
