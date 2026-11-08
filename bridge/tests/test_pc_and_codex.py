import pytest

from m5bridge import pc
from m5bridge.agent import AgentResult, CodexAgent, describe_codex_item
from m5bridge.config import Config
from m5bridge.server import create_app

from test_server import AUTH, TOKEN, FakeAgent, FakeDesktop, FakeTranscriber, wait_job


class FakeChat:
    def __init__(self):
        self.hwnd = None
        self.title = ""
        self.sent = []

    def select(self, hwnd):
        self.hwnd, self.title = hwnd, "Firefox"
        return {"id": hwnd, "title": "Firefox", "app": "firefox"}

    async def ask(self, text, on_step=None):
        self.sent.append(text)
        if on_step:
            on_step("Окно: Firefox")
        return "ответ из окна"


@pytest.fixture
def setup():
    claude, codex = FakeAgent(), FakeAgent()
    chat = FakeChat()
    cfg = Config(token=TOKEN, confirm_voice=False, commands={"Проводник": "explorer"})
    return cfg, {"claude": claude, "codex": codex}, chat


@pytest.fixture
async def client(aiohttp_client, setup):
    cfg, agents, chat = setup
    return await aiohttp_client(create_app(cfg, agents, FakeTranscriber(), FakeDesktop(), chat))


async def test_codex_mode_uses_codex_agent(client, setup):
    _cfg, agents, _chat = setup
    resp = await client.post("/api/text", json={"text": "сделай ревью", "mode": "codex"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "done" and job["mode"] == "codex"
    assert agents["codex"].prompts == [("сделай ревью", False)]
    assert agents["claude"].prompts == []


async def test_agent_mode_is_claude(client, setup):
    _cfg, agents, _chat = setup
    resp = await client.post("/api/text", json={"text": "x", "mode": "agent"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["mode"] == "claude"
    assert agents["claude"].prompts == [("x", False)]


async def test_window_mode(client, setup):
    _cfg, _agents, chat = setup
    resp = await client.post("/api/windows/select", json={"id": 42}, headers=AUTH)
    assert resp.status == 200
    resp = await client.post("/api/text", json={"text": "привет", "mode": "window"}, headers=AUTH)
    job = await wait_job(client, (await resp.json())["job"])
    assert job["state"] == "done"
    assert job["reply"] == "ответ из окна"
    assert job["steps"] == ["Окно: Firefox"]
    assert chat.sent == ["привет"]


async def test_sessions_per_agent(client, setup):
    _cfg, agents, _chat = setup
    agents["codex"].current = "abc"
    data = await (await client.get("/api/sessions?agent=codex", headers=AUTH)).json()
    assert data["sessions"][0]["id"] == "abc"
    assert (await client.get("/api/sessions?agent=gemini", headers=AUTH)).status == 404


async def test_commands_and_input(client, monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "press", lambda key, mods=(), repeat=1: calls.append(("press", key, list(mods), repeat)))
    monkeypatch.setattr(pc, "type_text", lambda text: calls.append(("type", text)))
    monkeypatch.setattr(pc, "mouse", lambda dx=0, dy=0, click="", scroll=0: calls.append(("mouse", dx, dy, click, scroll)))

    names = (await (await client.get("/api/commands", headers=AUTH)).json())["commands"]
    assert "Громкость +" in names and "Проводник" in names
    assert (await client.post("/api/commands/run", json={"name": "Громкость +"}, headers=AUTH)).status == 200
    assert (await client.post("/api/commands/run", json={"name": "rm -rf /"}, headers=AUTH)).status == 400

    await client.post("/api/input", json={"text": "привет"}, headers=AUTH)
    await client.post("/api/input", json={"key": "c", "mods": ["ctrl"]}, headers=AUTH)
    await client.post("/api/input", json={"dx": 99999, "dy": -5, "click": "left"}, headers=AUTH)
    assert calls == [
        ("press", "volume_up", [], 3),
        ("type", "привет"),
        ("press", "c", ["ctrl"], 1),
        ("mouse", 2000, -5, "left", 0),
    ]


def test_new_text_diff():
    before = ["Чат", "старое сообщение", "Prompt"]
    after = ["Чат", "старое сообщение", "Как дела?", "Всё хорошо, спасибо!", "Prompt"]
    assert pc.new_text(before, after, "Как дела?") == ["Всё хорошо, спасибо!"]


def test_vk_names():
    assert pc._vk("enter") == 0x0D and pc._vk("a") == ord("A") and pc._vk("F5") == 0x74
    with pytest.raises(pc.PcError):
        pc._vk("щ")


def test_codex_events():
    agent = CodexAgent.__new__(CodexAgent)
    ctx: dict = {}
    events = [
        {"type": "thread.started", "thread_id": "01a1bb13-c1b3-73d0-9c94-c2387a99e045"},
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "git status"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "git status"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Чисто."}},
        {"type": "turn.completed"},
    ]
    steps = [s for e in events for s in agent._on_event(e, ctx)]
    assert steps == ["Bash: git status"]
    result = agent._parse(ctx, b"", 0)
    assert result == AgentResult(text="Чисто.", session_id="01a1bb13-c1b3-73d0-9c94-c2387a99e045")


def test_codex_failed_turn_is_error():
    agent = CodexAgent.__new__(CodexAgent)
    ctx: dict = {}
    agent._on_event({"type": "thread.started", "thread_id": "t"}, ctx)
    agent._on_event({"type": "turn.failed", "error": {"message": "quota exceeded"}}, ctx)
    result = agent._parse(ctx, b"", 1)
    assert result.is_error and result.text == "quota exceeded"


def test_describe_codex_item():
    assert describe_codex_item({"type": "mcp_tool_call", "server": "gmail", "tool": "search"}) == "gmail: search"
    assert describe_codex_item({"type": "file_change", "changes": [{"path": "a.py"}]}) == "Edit: a.py"
    assert describe_codex_item({"type": "reasoning"}) is None


def test_new_text_ignores_relative_times():
    after = ["a", "19 minutes ago", "2 мин назад", "17:42", "только что", "Настоящий ответ"]
    assert pc.new_text(["a"], after, "вопрос") == ["Настоящий ответ"]
