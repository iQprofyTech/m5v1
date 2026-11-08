import time

import pytest

from m5bridge.config import Config
from m5bridge.control import Manager, create_control_app
from m5bridge.server import Bridge

ORIGIN = {"Origin": "http://127.0.0.1:8764"}  # the manager page itself
CTL = {"X-M5-Control": "1"}


class FakeAutostart:
    def __init__(self):
        self.value = True

    def get(self):
        return self.value

    def set(self, enabled):
        self.value = enabled
        return self.value


@pytest.fixture
def manager(tmp_path):
    # port 9 (discard) has no bridge on it: the manager sees a stopped bridge
    return Manager(Config(token="t" * 20, port=9), tmp_path / "config.toml", tmp_path / "logs", FakeAutostart())


@pytest.fixture
async def client(aiohttp_client, manager):
    c = await aiohttp_client(create_control_app(manager))
    yield c
    await manager.close()


async def test_status_of_stopped_bridge(client):
    data = await (await client.get("/control/status")).json()
    assert data["bridge"] == "stopped" and data["autostart"] is True and data["pid"] is None


async def test_activity_503_while_stopped(client):
    assert (await client.get("/api/activity")).status == 503


async def test_page_is_served(client):
    assert "m5v1" in await (await client.get("/")).text()


async def test_changes_need_the_dashboard_header(client):
    assert (await client.post("/control/stop")).status == 403
    assert (await client.post("/control/stop", headers={**CTL, "Origin": "http://evil.example"})).status == 403
    assert (await client.post("/control/autostart", json={"enabled": False}, headers={**CTL, **ORIGIN})).status == 200


async def test_autostart_toggle(client, manager):
    resp = await client.post("/control/autostart", json={"enabled": False}, headers=CTL)
    assert (await resp.json())["autostart"] is False
    assert manager.autostart.value is False


async def test_cors_for_the_bridge_page(client):
    resp = await client.options("/control/start", headers={**ORIGIN, "Access-Control-Request-Method": "POST"})
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == ORIGIN["Origin"]


async def test_stop_marks_bridge_unwanted(client, manager):
    resp = await client.post("/control/stop", headers=CTL)
    assert resp.status == 200
    assert manager.want is False


def test_device_online_by_ping():
    bridge = Bridge(Config(token="t" * 20), {"claude": object()}, None, None)
    bridge.last_seen = {"t": time.time() - 600, "ip": "192.168.137.79", "path": "/api/status"}
    assert bridge.device_state()["online"] is False
    bridge.ping_ok = {"ip": "192.168.137.79", "t": time.time()}
    assert bridge.device_state()["online"] is True
