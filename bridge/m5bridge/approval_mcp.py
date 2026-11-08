"""Permission prompts from `claude -p`, answered on the Cardputer.

Claude Code runs this as a stdio MCP server (see ClaudeAgent._command) and calls its
`approve` tool via `--permission-prompt-tool` whenever the agent wants a tool that the
config neither allows nor denies (e.g. Gmail / Calendar connectors, a Bash command that is
not in allowed_tools). The request is forwarded to the bridge, which shows it on the
device and waits for Enter (allow) or Esc (deny).

Standard library only: it is started with the bridge's own Python for every agent run.
Environment: M5_APPROVAL_URL (bridge internal endpoint), M5_APPROVAL_SECRET.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TOOL = {
    "name": "approve",
    "description": "Ask the person holding the Cardputer to allow or deny a tool call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}

# Longer than the bridge's own wait, so the bridge decides (and logs) the timeout.
HTTP_TIMEOUT = 600


def ask_bridge(tool_name: str, tool_input: dict) -> dict:
    url = os.environ.get("M5_APPROVAL_URL", "")
    secret = os.environ.get("M5_APPROVAL_SECRET", "")
    if not url or not secret:
        return {"behavior": "deny", "message": "m5bridge approval endpoint is not configured"}
    body = json.dumps({"tool_name": tool_name, "input": tool_input}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
    )
    # The bridge is local; never send this through a system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            allowed = bool(json.loads(resp.read().decode("utf-8")).get("allow"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"behavior": "deny", "message": f"approval failed: {exc}"}
    if allowed:
        return {"behavior": "allow", "updatedInput": tool_input}
    return {"behavior": "deny", "message": "Пользователь запретил это действие на Cardputer."}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    if "id" not in msg:  # notification
        return None
    result: dict
    if method == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "m5", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params", {})
        args = params.get("arguments") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "unknown tool"}}
        tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
        decision = ask_bridge(str(args.get("tool_name", "?")), tool_input)
        result = {"content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def main() -> None:
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False, newline="\n")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
