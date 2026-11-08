"""Run the bridge: `python -m m5bridge` (from the bridge/ folder)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from pathlib import Path

from aiohttp import web

from . import __version__
from .agent import AgentError, ClaudeAgent, CodexAgent
from .pc import WindowChat
from .config import DEFAULT_CONFIG_PATH, load
from .desktop import DesktopTyper
from .discovery import start_discovery
from .server import create_app
from .stt import Transcriber

log = logging.getLogger("m5bridge")


def lan_addresses() -> list[str]:
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


async def main_async(args) -> None:
    cfg = load(Path(args.config))
    transcriber = Transcriber(cfg)
    agent = ClaudeAgent(cfg)
    agents = {"claude": agent}
    try:
        agents["codex"] = CodexAgent(cfg)
    except AgentError as exc:
        log.warning("codex agent disabled: %s", exc)
    app = create_app(cfg, agents, transcriber, DesktopTyper(cfg), WindowChat(cfg))

    if not args.no_preload:
        log.info("loading speech model (first run downloads it)...")
        await asyncio.to_thread(transcriber.preload)

    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access") if args.verbose else None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    discovery = await start_discovery(cfg.discovery_port, cfg.port, cfg.name)

    log.info("m5bridge %s on http://%s:%d  (discovery udp/%d)", __version__, cfg.host, cfg.port, cfg.discovery_port)
    for addr in lan_addresses():
        log.info("  LAN: http://%s:%d", addr, cfg.port)
    log.info("claude: %s  workdir: %s  permission_mode: %s", agent.bin, cfg.workdir, cfg.permission_mode)
    if "codex" in agents:
        log.info("codex: %s  sandbox: %s", agents["codex"].bin, cfg.codex_sandbox)
    log.info("config: %s", Path(args.config).resolve())
    try:
        await asyncio.Event().wait()
    finally:
        discovery.close()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(prog="m5bridge", description="M5 Cardputer -> Claude bridge")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--no-preload", action="store_true", help="load the speech model on first use")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", help="write the log here instead of the console (for autostart)")
    args = parser.parse_args()
    handlers = None
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.FileHandler(args.log_file, encoding="utf-8")]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S" if args.log_file else "%H:%M:%S",
        handlers=handlers,
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
