from pathlib import Path

import aiohttp
import pytest

from monitoni.config import load_config
from monitoni.daemon import Daemon
from monitoni.hardware.mock import MockHardware

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
STATUS_KEYS = {"machine_id", "hardware_mode", "uptime_s", "state"}


@pytest.fixture
async def daemon():
    config = load_config(DEFAULT_PATH)
    config.hardware.mode = "mock"
    config.web.port = 0  # let the OS pick a free port
    daemon = Daemon(config, MockHardware())
    await daemon.start()
    yield daemon
    await daemon.stop()


async def test_status_endpoint(daemon):
    async with aiohttp.ClientSession() as session:
        async with session.get(daemon.url + "/api/status") as resp:
            assert resp.status == 200
            body = await resp.json()
    assert set(body) == STATUS_KEYS
    assert body["machine_id"] == "VM001"
    assert body["hardware_mode"] == "mock"
    assert body["state"] == "idle"
    assert body["uptime_s"] >= 0


async def test_index_serves_html(daemon):
    async with aiohttp.ClientSession() as session:
        async with session.get(daemon.url + "/") as resp:
            assert resp.status == 200
            assert resp.content_type == "text/html"
            assert "<title>MoniToni</title>" in await resp.text()


async def test_websocket_pushes_status_on_connect(daemon):
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(daemon.url + "/ws") as ws:
            body = await ws.receive_json(timeout=5)
    assert set(body) == STATUS_KEYS
    assert body["state"] == "idle"


async def test_stop_closes_open_websockets():
    config = load_config(DEFAULT_PATH)
    config.web.port = 0
    daemon = Daemon(config, MockHardware())
    await daemon.start()
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(daemon.url + "/ws")
        await ws.receive_json(timeout=5)
        await daemon.stop()
        msg = await ws.receive(timeout=5)
        assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        await ws.close()
