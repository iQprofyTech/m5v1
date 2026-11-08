import asyncio
import json

import pytest

from m5bridge import approval_mcp
from m5bridge.agent import AgentResult, describe_tool
from m5bridge.config import Config
from m5bridge.server import INTERNAL_SECRET_KEY, create_app

from test_server import AUTH, TOKEN, FakeDesktop, FakeTranscriber, wait_job, wait_state


class BlockingAgent:
    """Runs until the test releases it, like `claude -p` waiting on a permission prompt."""

    def __init__(self):
        self.current = None
        self.busy = False
        self.release = asyncio.Event()
        self.audit_lines = []

    async def run(self, prompt, new_session=False, on_step=None):
        await self.release.wait()
        return AgentResult(text="done", session_id="11111111-2222-3333-4444-555555555555")

    async def cancel(self):
        self.release.set()
        return True

    def audit(self, text):
        self.audit_lines.append(text)

    def list_sessions(self):
        return []


@pytest.fixture
async def setup(aiohttp_client):
    agent = BlockingAgent()
    app = create_app(Config(token=TOKEN, confirm_voice=False), agent, FakeTranscriber(), FakeDesktop())
    client = await aiohttp_client(app)
    internal = {"Authorization": f"Bearer {app[INTERNAL_SECRET_KEY]}"}
    return client, agent, internal


async def start_agent_job(client):
    resp = await client.post("/api/text", json={"text": "проверь почту", "mode": "agent"}, headers=AUTH)
    job_id = (await resp.json())["job"]
    await wait_state(client, job_id, "running")
    return job_id


def ask(client, internal, tool="mcp__claude_ai_Gmail__list_drafts", tool_input=None):
    return asyncio.create_task(
        client.post(
            "/internal/permission", json={"tool_name": tool, "input": tool_input or {}}, headers=internal
        )
    )


async def test_device_allows_tool(setup):
    client, agent, internal = setup
    job_id = await start_agent_job(client)
    pending = ask(client, internal)
    job = await wait_state(client, job_id, "approve")
    assert job["approval"] == "Gmail: list_drafts"

    resp = await client.post(f"/api/jobs/{job_id}/approve", json={"allow": True}, headers=AUTH)
    assert resp.status == 200
    assert (await (await pending).json()) == {"allow": True}
    job = await wait_state(client, job_id, "running")
    assert job["approval"] == ""
    agent.release.set()
    assert (await wait_job(client, job_id))["state"] == "done"


async def test_device_denies_tool(setup):
    client, agent, internal = setup
    job_id = await start_agent_job(client)
    pending = ask(client, internal, "Bash", {"command": "git push"})
    job = await wait_state(client, job_id, "approve")
    assert job["approval"] == "Bash: git push"
    await client.post(f"/api/jobs/{job_id}/approve", json={"allow": False}, headers=AUTH)
    assert (await (await pending).json()) == {"allow": False}
    agent.release.set()


async def test_always_allow_skips_next_question(setup):
    client, agent, internal = setup
    job_id = await start_agent_job(client)
    pending = ask(client, internal)
    await wait_state(client, job_id, "approve")
    await client.post(f"/api/jobs/{job_id}/approve", json={"allow": True, "always": True}, headers=AUTH)
    assert (await (await pending).json())["allow"] is True
    # Same tool again: answered at once, the device is not asked.
    resp = await client.post(
        "/internal/permission", json={"tool_name": "mcp__claude_ai_Gmail__list_drafts", "input": {}}, headers=internal
    )
    assert (await resp.json())["allow"] is True
    agent.release.set()


async def test_cancel_while_asking_denies(setup):
    client, agent, internal = setup
    job_id = await start_agent_job(client)
    pending = ask(client, internal)
    await wait_state(client, job_id, "approve")
    await client.post(f"/api/jobs/{job_id}/cancel", headers=AUTH)
    assert (await (await pending).json())["allow"] is False
    assert (await wait_job(client, job_id))["state"] == "cancelled"


async def test_no_active_job_denies(setup):
    client, _agent, internal = setup
    resp = await client.post("/internal/permission", json={"tool_name": "Bash", "input": {}}, headers=internal)
    assert (await resp.json())["allow"] is False


async def test_internal_endpoint_rejects_device_token(setup):
    client, _agent, _internal = setup
    resp = await client.post("/internal/permission", json={"tool_name": "Bash", "input": {}}, headers=AUTH)
    assert resp.status == 403


async def test_approve_requires_pending_question(setup):
    client, agent, _internal = setup
    job_id = await start_agent_job(client)
    resp = await client.post(f"/api/jobs/{job_id}/approve", json={"allow": True}, headers=AUTH)
    assert resp.status == 409
    agent.release.set()


def test_describe_tool():
    assert describe_tool("Bash", {"command": "git  status"}) == "Bash: git status"
    assert describe_tool("mcp__claude_ai_Google_Calendar__list_events", {}) == "Google_Calendar: list_events"
    assert describe_tool("Read", {"file_path": "C:/x.txt"}) == "Read: C:/x.txt"


def test_mcp_protocol(monkeypatch):
    init = approval_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert approval_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = approval_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools["result"]["tools"][0]["name"] == "approve"

    monkeypatch.setattr(approval_mcp, "ask_bridge", lambda name, inp: {"behavior": "allow", "updatedInput": inp})
    call = approval_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}},
        }
    )
    decision = json.loads(call["result"]["content"][0]["text"])
    assert decision == {"behavior": "allow", "updatedInput": {"command": "ls"}}


def test_mcp_without_bridge_denies(monkeypatch):
    monkeypatch.delenv("M5_APPROVAL_URL", raising=False)
    assert approval_mcp.ask_bridge("Bash", {})["behavior"] == "deny"
