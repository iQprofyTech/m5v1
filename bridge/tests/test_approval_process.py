"""approval_mcp as Claude Code runs it: a separate process speaking MCP over stdio."""

import asyncio
import json
import os
import sys

from m5bridge.agent import BRIDGE_DIR
from m5bridge.config import Config
from m5bridge.server import create_app

from test_approval import BlockingAgent, start_agent_job
from test_server import AUTH, TOKEN, FakeDesktop, FakeTranscriber, wait_state


async def test_mcp_process_round_trip(aiohttp_client):
    agent = BlockingAgent()
    cfg = Config(token=TOKEN, confirm_voice=False)
    app = create_app(cfg, agent, FakeTranscriber(), FakeDesktop())
    client = await aiohttp_client(app)
    url, secret = agent.approval
    # The test server listens on a random port, not cfg.port.
    url = str(client.make_url("/internal/permission"))

    job_id = await start_agent_job(client)
    env = {**os.environ, "M5_APPROVAL_URL": url, "M5_APPROVAL_SECRET": secret, "PYTHONPATH": str(BRIDGE_DIR)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "m5bridge.approval_mcp",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env,
    )

    async def rpc(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
        if "id" in msg:
            return json.loads(await asyncio.wait_for(proc.stdout.readline(), 20))

    try:
        await rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = asyncio.create_task(
            rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {"command": "dir"}}},
                }
            )
        )
        job = await wait_state(client, job_id, "approve")
        assert job["approval"] == "Bash: dir"
        await client.post(f"/api/jobs/{job_id}/approve", json={"allow": True}, headers=AUTH)
        reply = await call
        decision = json.loads(reply["result"]["content"][0]["text"])
        assert decision == {"behavior": "allow", "updatedInput": {"command": "dir"}}
    finally:
        proc.stdin.close()
        await proc.wait()
        agent.release.set()
