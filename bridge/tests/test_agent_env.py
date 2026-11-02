from m5bridge import agent


def test_strips_parent_claude_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "1")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = agent.agent_env("none")
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CLAUDE_AGENT_SDK_VERSION" not in env
    assert env["KEEP_ME"] == "yes"


def test_auto_uses_system_proxy_when_env_has_none(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(agent, "windows_system_proxy", lambda: "http://127.0.0.1:12334")
    env = agent.agent_env("auto")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:12334"


def test_auto_keeps_existing_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://corp:3128")
    monkeypatch.setattr(agent, "windows_system_proxy", lambda: "http://127.0.0.1:12334")
    assert agent.agent_env("auto")["HTTPS_PROXY"] == "http://corp:3128"


def test_describe_steps():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git  status\n"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "F:/p/main.py"}},
                {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}},
            ]
        },
    }
    assert agent.describe_steps(event) == ["Bash: git status", "Edit: F:/p/main.py", "TodoWrite"]
    assert agent.describe_steps({"type": "user"}) == []


def test_explicit_proxy_wins(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://corp:3128")
    assert agent.agent_env("http://10.0.0.1:8080")["HTTPS_PROXY"] == "http://10.0.0.1:8080"
