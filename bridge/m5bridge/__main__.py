"""Run the bridge: `python -m m5bridge` (from the bridge/ folder)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from pathlib import Path

from aiohttp import web

from . import __version__
from .agent import ClaudeAgent
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
    app = create_app(cfg, agent, transcriber, DesktopTyper(cfg))

    if not args.no_preload:
        log.info("loading speech model (first run downloads it)...")
        await asyncio.to_thread(transcriber.preload)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    discovery = await start_discovery(cfg.discovery_port, cfg.port, cfg.name)

    log.info("m5bridge %s on http://%s:%d  (discovery udp/%d)", __version__, cfg.host, cfg.port, cfg.discovery_port)
    for addr in lan_addresses():
        log.info("  LAN: http://%s:%d", addr, cfg.port)
    log.info("claude: %s  workdir: %s  permission_mode: %s", agent.bin, cfg.workdir, cfg.permission_mode)
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
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
