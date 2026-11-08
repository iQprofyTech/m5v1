import asyncio
import json
import time
from datetime import datetime, timezone

from m5bridge import desktop_reply
from m5bridge.desktop_reply import wait_reply


def ts(offset=0.0):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat().replace("+00:00", "Z")


def entry(kind, session="s1", **kw):
    return {"type": kind, "sessionId": session, "uuid": kw.pop("uuid", f"u{time.monotonic_ns()}"), "timestamp": ts(), **kw}


def assistant(msg_id, blocks, stop="tool_use"):
    return entry("assistant", message={"id": msg_id, "role": "assistant", "content": blocks, "stop_reason": stop})


def append(path, *entries):
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


async def test_reply_from_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_reply, "TAIL_QUIET", 0.2)
    project = tmp_path / "F--tmp-MyApps"
    project.mkdir()
    path = project / "s1.jsonl"
    append(path, entry("user", message={"role": "user", "content": "старый вопрос"}))
    since = time.time()
    steps = []
    task = asyncio.create_task(wait_reply("проверь   почту", since, on_step=steps.append, timeout=10, root=tmp_path))

    await asyncio.sleep(0.5)
    append(
        path,
        entry("user", message={"role": "user", "content": "проверь почту"}),
        assistant("m1", [{"type": "thinking", "thinking": "..."}]),
        assistant("m1", [{"type": "tool_use", "name": "mcp__claude_ai_Gmail__list_drafts", "input": {}}]),
        entry("user", message={"role": "user", "content": [{"type": "tool_result", "content": "2 drafts"}]}),
        assistant("m2", [{"type": "text", "text": "В черновиках 2 письма."}], stop="end_turn"),
        entry("system", subtype="stop_hook_summary"),
    )
    reply = await asyncio.wait_for(task, 10)
    assert reply == "В черновиках 2 письма."
    assert steps == ["Gmail: list_drafts"]


async def test_no_transcript_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_reply, "FIND_TIMEOUT", 0.5)
    assert await wait_reply("привет", time.time(), timeout=5, root=tmp_path) is None


async def test_ignores_old_prompt_with_same_text(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_reply, "FIND_TIMEOUT", 0.6)
    project = tmp_path / "p"
    project.mkdir()
    old = entry("user", message={"role": "user", "content": "статус"})
    old["timestamp"] = ts(-3600)
    append(project / "s1.jsonl", old, assistant("m0", [{"type": "text", "text": "старый ответ"}], stop="end_turn"))
    assert await wait_reply("статус", time.time(), timeout=5, root=tmp_path) is None
