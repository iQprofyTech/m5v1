"""Bridge manager: `python -m m5bridge.control` (what autostart runs).

A small always-on process on this PC only (http://localhost:8764) that keeps the bridge running and
lets the dashboard stop it, start it again and switch autostart on or off. The bridge itself cannot
start itself once stopped, so these buttons live here.

  GET  /                      the dashboard (same page the bridge serves)
  GET  /api/activity          proxied to the bridge, 503 while it is stopped
  GET  /control/status        {"bridge": running|starting|stopping|stopped, "pid", "autostart": true|false|null, ...}
  POST /control/start | /control/stop | /control/restart
  POST /control/autostart     {"enabled": true|false}   the Windows scheduled task "m5bridge"

Only loopback clients are served. Requests that change something need the X-M5-Control header and,
from a browser, an Origin of the dashboard itself (so no other web page can press the buttons).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, load

log = logging.getLogger("m5bridge.control")

TASK_NAME = "m5bridge"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
ROOT = Path(__file__).resolve().parents[2]  # the repository: bridge/m5bridge/control.py -> m5v1
START_TIMEOUT = 120  # the speech model can take a while to load


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for enc in ("utf-8", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("latin-1")


class Autostart:
    """The scheduled task that starts the manager at logon (tools/install_autostart.ps1)."""

    def __init__(self, name: str = TASK_NAME):
        self.name = name

    def get(self) -> bool | None:
        """True / False, or None when the task is not installed (or this is not Windows)."""
        if os.name != "nt":
            return None
        res = _run(["schtasks", "/Query", "/TN", self.name, "/XML"])
        if res.returncode != 0:
            return None
        return "<Enabled>false</Enabled>" not in _decode(res.stdout).replace(" ", "")

    def set(self, enabled: bool) -> bool | None:
        if os.name != "nt":
            raise RuntimeError("автозапуск настраивается только в Windows")
        if self.get() is None:
            if not enabled:
                return None
            script = ROOT / "tools" / "install_autostart.ps1"
            res = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-NoStart"], 90)
        else:
            res = _run(["schtasks", "/Change", "/TN", self.name, "/ENABLE" if enabled else "/DISABLE"])
        if res.returncode != 0:
            raise RuntimeError(_decode(res.stderr or res.stdout).strip() or f"код {res.returncode}")
        return self.get()


class Manager:
    """Starts, stops and watches the bridge process."""

    def __init__(self, cfg: Config, config_path: Path, log_dir: Path, autostart: Autostart | None = None):
        self.cfg = cfg
        self.config_path = config_path
        self.log_dir = log_dir
        self.autostart = autostart or Autostart()
        self.proc: asyncio.subprocess.Process | None = None
        self.want = True          # keep the bridge running (False after "stop")
        self.busy = ""            # "starting" / "stopping" while an action is in progress
        self.error = ""
        self.since = 0.0
        self.restarts: list[float] = []
        self.lock = asyncio.Lock()
        self.session: aiohttp.ClientSession | None = None

    @property
    def bridge_url(self) -> str:
        host = "127.0.0.1" if self.cfg.host in ("", "0.0.0.0", "::") else self.cfg.host
        return f"http://{host}:{self.cfg.port}"

    async def activity(self) -> dict | None:
        """The bridge's /api/activity (loopback: no token needed), or None when it does not answer."""
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
        try:
            async with self.session.get(self.bridge_url + "/api/activity") as resp:
                if resp.status == 200:
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        return None

    def child_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def status(self) -> dict:
        data = await self.activity()
        if self.busy:
            state = self.busy
        elif data is not None:
            state = "running"
        elif self.child_alive():
            state = "starting"  # the process is up, still loading the speech model
        else:
            state = "stopped"
        autostart = await asyncio.to_thread(self._autostart_get)
        return {
            "bridge": state,
            "pid": (data or {}).get("pid") or (self.proc.pid if self.child_alive() else None),
            "managed": self.child_alive(),
            "since": self.since if state == "running" else 0,
            "autostart": autostart,
            "error": self.error,
            "version": __version__,
            "port": self.cfg.port,
        }

    def _autostart_get(self):
        try:
            return self.autostart.get()
        except Exception:  # noqa: BLE001 - schtasks missing or broken: just show "unknown"
            log.exception("autostart query failed")
            return None

    async def _spawn(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "m5bridge", "--config", str(self.config_path),
               "--log-file", str(self.log_dir / "bridge.log")]
        err = open(self.log_dir / "bridge-stderr.log", "ab")  # tracebacks the logger never saw
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=err, creationflags=NO_WINDOW,
            )
        finally:
            err.close()
        log.info("bridge started, pid %s", self.proc.pid)

    async def start(self) -> None:
        async with self.lock:
            self.want = True
            self.error = ""
            if await self.activity() is not None or self.child_alive():
                return
            self.busy = "starting"
            try:
                await self._spawn()
                deadline = time.monotonic() + START_TIMEOUT
                while time.monotonic() < deadline:
                    if await self.activity() is not None:
                        self.since = time.time()
                        return
                    if not self.child_alive():
                        self.error = f"мост завершился сразу (код {self.proc.returncode}), см. bridge/logs/bridge.log"
                        log.error(self.error)
                        return
                    await asyncio.sleep(0.5)
                self.error = "мост не ответил за 2 минуты, см. bridge/logs/bridge.log"
            finally:
                self.busy = ""

    async def stop(self) -> None:
        async with self.lock:
            self.want = False
            self.error = ""
            data = await self.activity()
            pids = {p for p in ((data or {}).get("pid"), self.proc.pid if self.child_alive() else None) if p}
            if not pids:
                return
            self.busy = "stopping"
            try:
                for pid in pids:
                    await asyncio.to_thread(self._kill_tree, pid)
                if self.child_alive():
                    try:
                        await asyncio.wait_for(self.proc.wait(), 10)
                    except asyncio.TimeoutError:
                        self.proc.kill()
                for _ in range(20):
                    if await self.activity() is None:
                        break
                    await asyncio.sleep(0.25)
                log.info("bridge stopped")
            finally:
                self.busy = ""
                self.since = 0.0

    @staticmethod
    def _kill_tree(pid: int) -> None:
        # the bridge may be running an agent (claude / codex): stop those with it
        if os.name == "nt":
            _run(["taskkill", "/T", "/F", "/PID", str(pid)])
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def watch(self) -> None:
        """Bring the bridge back if it dies on its own (not after "stop"); at most 5 times in 10 minutes."""
        while True:
            await asyncio.sleep(3)
            if not self.want or self.busy or self.lock.locked() or self.child_alive():
                continue
            if await self.activity() is not None:
                continue  # running, started some other way
            now = time.time()
            self.restarts = [t for t in self.restarts if now - t < 600]
            if len(self.restarts) >= 5:
                self.error = "мост падает снова и снова, автоперезапуск выключен, см. bridge/logs/bridge.log"
                self.want = False
                continue
            self.restarts.append(now)
            log.warning("bridge is not running, starting it")
            await self.start()

    async def close(self) -> None:
        if self.session:
            await self.session.close()


def _is_loopback(addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def create_control_app(manager: Manager) -> web.Application:
    cfg = manager.cfg
    local = ("localhost", "127.0.0.1", "[::1]")
    origins = {f"http://{h}:{p}" for h in local for p in (cfg.control_port, cfg.port)}
    page = Path(__file__).parent / "web" / "index.html"

    @web.middleware
    async def guard(request: web.Request, handler):
        if not _is_loopback(request.remote) or request.host.rsplit(":", 1)[0] not in local:
            raise web.HTTPForbidden(text="only from this PC")  # also stops DNS rebinding
        origin = request.headers.get("Origin")
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin in origins else {}
        if request.method == "OPTIONS":
            if not cors:
                raise web.HTTPForbidden()
            return web.Response(headers={**cors, "Access-Control-Allow-Methods": "GET, POST",
                                         "Access-Control-Allow-Headers": "Content-Type, X-M5-Control"})
        if request.method == "POST":
            if (origin and origin not in origins) or request.headers.get("X-M5-Control") != "1":
                raise web.HTTPForbidden(text="not from the dashboard")
        resp = await handler(request)
        resp.headers.update(cors)
        return resp

    async def index(request):
        return web.FileResponse(page)

    async def activity(request):
        data = await manager.activity()
        if data is None:
            return web.json_response({"stopped": True}, status=503)
        return web.json_response(data)

    async def status(request):
        return web.json_response(await manager.status())

    def action(fn):
        async def handler(request):
            # run in the background: the page keeps polling /control/status and shows "starting..."
            asyncio.get_running_loop().create_task(fn())
            await asyncio.sleep(0.2)
            return web.json_response(await manager.status())
        return handler

    async def autostart(request):
        try:
            enabled = bool((await request.json())["enabled"])
            value = await asyncio.to_thread(manager.autostart.set, enabled)
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text='{"enabled": true|false}')
        except Exception as exc:  # noqa: BLE001 - shown on the page
            log.exception("autostart change failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"autostart": value})

    async def preflight(request):  # answered by the guard
        return web.Response()

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index),
        web.get("/ui", index),
        web.get("/api/activity", activity),
        web.get("/control/status", status),
        web.post("/control/start", action(manager.start)),
        web.post("/control/stop", action(manager.stop)),
        web.post("/control/restart", action(manager.restart)),
        web.post("/control/autostart", autostart),
        web.route("OPTIONS", "/{tail:.*}", preflight),
    ])
    return app


async def main_async(args) -> None:
    config_path = Path(args.config).resolve()
    cfg = load(config_path)
    manager = Manager(cfg, config_path, Path(args.log_dir).resolve())
    runner = web.AppRunner(create_control_app(manager))
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", cfg.control_port).start()
    except OSError:
        log.error("port %d is busy: the bridge manager is already running", cfg.control_port)
        await runner.cleanup()
        return
    log.info("m5bridge manager %s on http://localhost:%d", __version__, cfg.control_port)
    if not args.no_start:
        await manager.start()
    watcher = asyncio.create_task(manager.watch())
    try:
        await asyncio.Event().wait()
    finally:
        watcher.cancel()
        await manager.close()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(prog="m5bridge.control", description="start/stop the m5 bridge, autostart")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--log-dir", default=str(Path(__file__).resolve().parents[1] / "logs"))
    parser.add_argument("--log-file", help="write the manager's log here instead of the console")
    parser.add_argument("--no-start", action="store_true", help="do not start the bridge right away")
    args = parser.parse_args()
    handlers = None
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.FileHandler(args.log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
