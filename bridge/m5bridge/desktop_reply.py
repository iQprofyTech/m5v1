"""Read Claude Code's answer back from its session transcript after typing into Claude Desktop.

Claude Desktop's Code tab (like the CLI) writes every session to
~/.claude/projects/<project>/<session>.jsonl. After the bridge pastes a command into the
Desktop window, this finds the transcript line with that command, reports the tool calls
that follow, and returns the text of the final assistant message (stop_reason "end_turn").

A plain Claude Desktop chat (not the Code tab) writes no transcript; then nothing is found
and the device just gets "sent".
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import describe_tool

PROJECTS_DIR = Path.home() / ".claude" / "projects"
POLL = 0.4
FIND_TIMEOUT = 60.0  # the command should show up in a transcript within this time (queued if busy)
TAIL_QUIET = 1.5     # after end_turn, wait this long for the rest of the message's blocks


def _norm(text: str) -> str:
    return " ".join(text.split())


def _timestamp(entry: dict) -> float:
    try:
        return datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def user_prompt(entry: dict) -> str | None:
    """The text of a prompt the person typed (not a tool result), or None."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


class _Reader:
    """Reads complete JSON lines appended to a file since the last call."""

    def __init__(self, path: Path, offset: int = 0):
        self.path = path
        self.offset = offset

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        out = []
        for line in data[: end + 1].splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out


def find_prompt(text: str, since: float, root: Path = PROJECTS_DIR) -> tuple[_Reader, str] | None:
    """The transcript where `text` was submitted after `since`, positioned right after it."""
    want = _norm(text)
    if not want or not root.is_dir():
        return None
    candidates = []
    for path in root.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since - 2:
            candidates.append((mtime, path))
    for _mtime, path in sorted(candidates, reverse=True):
        reader = _Reader(path)
        for entry in reader.read():
            prompt = user_prompt(entry)
            if prompt is None or want not in _norm(prompt) or _timestamp(entry) < since - 5:
                continue
            # Everything after this line is the answer; continue from just past it.
            return _Reader(path, _offset_after(path, entry.get("uuid"))), str(entry.get("sessionId", ""))
    return None


def _offset_after(path: Path, uuid) -> int:
    offset = 0
    with path.open("rb") as f:
        for line in f:
            offset += len(line)
            try:
                if json.loads(line).get("uuid") == uuid:
                    return offset
            except ValueError:
                continue
    return offset


async def wait_reply(
    text: str,
    since: float,
    on_step: Callable[[str], None] | None = None,
    timeout: float = 900,
    root: Path = PROJECTS_DIR,
) -> str | None:
    """The final answer to `text`, or None if no Claude Code transcript picked it up."""
    deadline = time.monotonic() + FIND_TIMEOUT
    found = None
    while found is None:
        found = await asyncio.to_thread(find_prompt, text, since, root)
        if found is None:
            if time.monotonic() > deadline:
                return None
            await asyncio.sleep(POLL)
    reader, session = found

    texts: dict[str, list[str]] = {}
    final_id: str | None = None
    last_new = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = await asyncio.to_thread(reader.read)
        if entries:
            last_new = time.monotonic()
        for entry in entries:
            if session and entry.get("sessionId") not in (None, session):
                continue
            if entry.get("isSidechain"):
                continue
            if user_prompt(entry) is not None:  # a new prompt: this turn is over
                return _join(texts, final_id)
            if entry.get("type") != "assistant":
                if final_id is not None and entry.get("type") in ("system", "user"):
                    return _join(texts, final_id)
                continue
            message = entry.get("message") or {}
            msg_id = str(message.get("id") or entry.get("uuid"))
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.setdefault(msg_id, []).append(str(block["text"]))
                elif block.get("type") == "tool_use" and on_step:
                    on_step(describe_tool(block.get("name"), block.get("input") or {}))
            if message.get("stop_reason") == "end_turn" or entry.get("isApiErrorMessage"):
                final_id = msg_id
        if final_id is not None and time.monotonic() - last_new > TAIL_QUIET:
            return _join(texts, final_id)
        await asyncio.sleep(POLL)
    raise TimeoutError(f"no answer from Claude Desktop in {int(timeout)}s")


def _join(texts: dict[str, list[str]], msg_id: str | None) -> str:
    if msg_id is None:
        return ""
    return "\n".join(t.strip() for t in texts.get(msg_id, []) if t.strip())
