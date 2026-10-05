"""The HTTP/WebSocket API end to end, in mock mode."""

import asyncio

import aiohttp
import pytest

from monitoni.daemon import Daemon
from monitoni.hardware.mock import MockHardware
from monitoni.purchase import MockPurchaseServer
from monitoni.web.server import qr_data

STATUS_KEYS = {"machine_id", "hardware_mode", "uptime_s", "state", "selected_level",
               "purchase_id", "levels", "doors", "countdown_s", "qr_url", "maintenance_message"}


@pytest.fixture
async def make_daemon(make_config):
    started = []

    async def _make(mode: str = "mock", **timings: float) -> Daemon:
        config = make_config(**timings)
        config.hardware.mode = mode
        daemon = Daemon(config, MockHardware(config.vending.levels), MockPurchaseServer())
        await daemon.start()
        started.append(daemon)
        return daemon

    yield _make
    for daemon in started:
        await daemon.stop()


@pytest.fixture
async def daemon(make_daemon):
    return await make_daemon()


@pytest.fixture
async def client():
    async with aiohttp.ClientSession() as session:
        yield session


async def command(client, daemon, **body):
    async with client.post(daemon.url + "/api/command", json=body) as resp:
        return resp.status, await resp.json()


async def status(client, daemon):
    async with client.get(daemon.url + "/api/status") as resp:
        assert resp.status == 200
        return await resp.json()


async def wait_for_state(client, daemon, state, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = await status(client, daemon)
        if current["state"] == state:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"expected {state}, still in {current['state']}")
        await asyncio.sleep(0.01)


async def test_status_endpoint(client, daemon):
    body = await status(client, daemon)
    assert set(body) == STATUS_KEYS
    assert body["state"] == "idle" and body["hardware_mode"] == "mock"
    assert body["levels"] == 10 and set(body["doors"]) == {str(n) for n in range(1, 11)}
    assert set(body["doors"].values()) == {"locked"}
    assert body["qr_url"] is None and body["selected_level"] is None
    assert body["countdown_s"] is not None  # idle has a sleep timeout


async def test_index_serves_html(client, daemon):
    async with client.get(daemon.url + "/") as resp:
        assert resp.status == 200 and resp.content_type == "text/html"
        assert "<title>MoniToni</title>" in await resp.text()


async def test_happy_path(client, daemon):
    code, body = await command(client, daemon, command="select_level", level=3)
    assert code == 200
    assert body["state"] == "checking_purchase" and body["selected_level"] == 3
    assert body["qr_url"] == "/api/qr/3.png" and body["purchase_id"]

    code, _ = await command(client, daemon, command="simulate_payment")
    assert code == 200
    body = await wait_for_state(client, daemon, "door_unlocked")
    assert body["doors"]["3"] == "unlocked" and body["doors"]["4"] == "locked"

    code, _ = await command(client, daemon, command="simulate_door", open=True)
    assert code == 200
    body = await wait_for_state(client, daemon, "door_opened")

    code, _ = await command(client, daemon, command="simulate_door", open=False)
    assert code == 200
    body = await wait_for_state(client, daemon, "idle")
    assert set(body["doors"].values()) == {"locked"}
    assert body["selected_level"] is None and body["purchase_id"] is None

    async with client.get(daemon.url + "/api/events?limit=100") as resp:
        rows = await resp.json()
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
    transitions = [r["details"]["to"] for r in reversed(rows) if r["kind"] == "transition"]
    assert transitions == ["checking_purchase", "door_unlocked", "door_opened",
                           "completing", "idle"]
    assert {r["kind"] for r in rows} >= {"daemon", "dev", "hardware", "purchase_check",
                                         "purchase_complete"}


async def test_door_alarm_path(client, make_daemon):
    daemon = await make_daemon(door_alarm_delay_s=0.05)
    await command(client, daemon, command="select_level", level=1)
    await command(client, daemon, command="simulate_payment")
    await wait_for_state(client, daemon, "door_unlocked")
    await command(client, daemon, command="simulate_door", open=True)
    await wait_for_state(client, daemon, "door_alarm")
    assert daemon.hardware.status()["alarm"] is True
    await command(client, daemon, command="simulate_door", open=False)
    body = await wait_for_state(client, daemon, "idle")
    assert daemon.hardware.status()["alarm"] is False
    assert set(body["doors"].values()) == {"locked"}


async def test_cancel_path(client, daemon):
    await command(client, daemon, command="select_level", level=2)
    code, body = await command(client, daemon, command="cancel")
    assert code == 200 and body["state"] == "idle" and body["qr_url"] is None


async def test_sleep_and_wake(client, make_daemon):
    daemon = await make_daemon(sleep_timeout_s=0.05)
    await wait_for_state(client, daemon, "sleep")
    code, body = await command(client, daemon, command="touch")
    assert code == 200 and body["state"] == "idle"


async def test_command_not_allowed_in_state(client, daemon):
    code, body = await command(client, daemon, command="cancel")
    assert code == 409 and "not allowed in state idle" in body["error"]
    code, body = await command(client, daemon, command="simulate_payment")
    assert code == 409


async def test_malformed_commands(client, daemon):
    async with client.post(daemon.url + "/api/command", data=b"not json") as resp:
        assert resp.status == 400
    for body in ({"command": "fly"}, {"command": "select_level"},
                 {"command": "select_level", "level": "3"},
                 {"command": "select_level", "level": 99},
                 {"command": "simulate_door", "open": "yes"}, {"nope": 1}, [1, 2]):
        async with client.post(daemon.url + "/api/command", json=body) as resp:
            assert resp.status == 400, body
    assert (await status(client, daemon))["state"] == "idle"


async def test_simulate_commands_forbidden_outside_mock_mode(client, make_daemon):
    daemon = await make_daemon(mode="real")
    code, _ = await command(client, daemon, command="simulate_payment")
    assert code == 403
    code, _ = await command(client, daemon, command="simulate_door", open=True)
    assert code == 403
    code, body = await command(client, daemon, command="select_level", level=1)
    assert code == 200 and body["hardware_mode"] == "real"


async def test_qr_png(client, daemon):
    async with client.get(daemon.url + "/api/qr/3.png") as resp:
        assert resp.status == 200 and resp.content_type == "image/png"
        assert (await resp.read())[:8] == b"\x89PNG\r\n\x1a\n"
    assert (daemon.config.qr.dir / "level_3.png").exists()
    for bad in ("0", "11", "abc"):
        async with client.get(daemon.url + f"/api/qr/{bad}.png") as resp:
            assert resp.status == 404, bad


def test_qr_data_matches_the_old_machines():
    assert qr_data("https://www.monitoni.zhdk.ch", 3) == "https://www.monitoni.zhdk.ch?level=3"


async def test_events_limit(client, daemon):
    async with client.get(daemon.url + "/api/events?limit=1") as resp:
        assert resp.status == 200 and len(await resp.json()) == 1
    async with client.get(daemon.url + "/api/events?limit=x") as resp:
        assert resp.status == 400


async def test_websocket_pushes_on_state_change(client, daemon):
    async with client.ws_connect(daemon.url + "/ws") as ws:
        first = await ws.receive_json(timeout=2)
        assert set(first) == STATUS_KEYS and first["state"] == "idle"
        await command(client, daemon, command="select_level", level=7)
        pushed = await ws.receive_json(timeout=0.5)  # well under the 1 s heartbeat
        assert pushed["state"] == "checking_purchase" and pushed["selected_level"] == 7


async def test_stop_closes_open_websockets(client, make_config):
    config = make_config()
    daemon = Daemon(config, MockHardware(config.vending.levels), MockPurchaseServer())
    await daemon.start()
    ws = await client.ws_connect(daemon.url + "/ws")
    await ws.receive_json(timeout=2)
    await daemon.stop()
    msg = await ws.receive(timeout=5)
    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await ws.close()
