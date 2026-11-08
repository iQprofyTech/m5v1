"""Exercise a running bridge the way the Cardputer does (no device needed).

    .venv\\Scripts\\python tools\\test_bridge.py discover
    .venv\\Scripts\\python tools\\test_bridge.py status
    .venv\\Scripts\\python tools\\test_bridge.py text "привет, ответь одним словом"
    .venv\\Scripts\\python tools\\test_bridge.py voice speech.wav [--mode claude|codex|window|type]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import tomllib
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = tomllib.loads((ROOT / "bridge" / "config.toml").read_text(encoding="utf-8"))["server"]


def call(base: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, data=body, method=method)
    req.add_header("Authorization", f"Bearer {CFG['token']}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def wait(base: str, job: str) -> dict:
    seen = None
    while True:
        data = call(base, "GET", f"/api/jobs/{job}")
        if data["state"] != seen:
            seen = data["state"]
            print(f"  state: {seen}")
        if data["state"] in ("done", "error", "cancelled"):
            return data
        time.sleep(0.8)


def discover(port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(2)
    s.sendto(b"M5BRIDGE?", ("255.255.255.255", port))
    try:
        data, addr = s.recvfrom(256)
        print(f"reply from {addr[0]}: {data.decode()}")
    except socket.timeout:
        print("no discovery reply (firewall blocking UDP?)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["discover", "status", "text", "voice"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--mode", default="claude", choices=["claude", "codex", "window", "type", "agent", "desktop"])
    p.add_argument("--url", default=f"http://127.0.0.1:{CFG.get('port', 8765)}")
    a = p.parse_args()

    if a.cmd == "discover":
        discover(CFG.get("discovery_port", 8766))
        return
    if a.cmd == "status":
        print(call(a.url, "GET", "/api/status"))
        return
    if a.cmd == "text":
        body = json.dumps({"text": a.arg, "mode": a.mode}).encode()
        job = call(a.url, "POST", "/api/text", body, {"Content-Type": "application/json"})["job"]
    else:
        with wave.open(a.arg, "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                sys.exit("need 16-bit mono WAV")
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        print(f"  sending {len(pcm) / 2 / rate:.1f}s at {rate} Hz")
        job = call(a.url, "POST", f"/api/voice?mode={a.mode}", pcm, {"X-Sample-Rate": str(rate)})["job"]
    result = wait(a.url, job)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
