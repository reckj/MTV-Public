"""The HTTP/WebSocket API end to end, in mock mode."""

import asyncio

import aiohttp
import pytest

from monitoni.daemon import Daemon
from monitoni.hardware.base import HardwareError, HardwareFault
from monitoni.hardware.mock import MockHardware
from monitoni.purchase import MockPurchaseServer, PurchaseServerError
from monitoni.runtime import Runtime
from monitoni.web.server import qr_data
from tests.conftest import http_purchase
from tests.helpers import command, events, status, wait_for_state, wait_until

STATUS_KEYS = {"name", "location", "machine_id", "app_version", "hostname", "ip", "hardware_mode",
               "purchase_mode", "uptime_s", "state", "reason", "selected_level", "purchase_id",
               "levels", "doors", "countdown_s", "qr_url", "hardware", "motor", "purchase_server",
               "leds", "audio", "settings", "config_view"}


class FailingMotor(MockHardware):
    async def set_motor(self, on: bool) -> None:
        if on:
            raise HardwareError("relay_core: no response")
        await super().set_motor(on)


@pytest.fixture
async def make_daemon(make_config):
    started = []

    async def _make(mode: str = "mock", hardware_cls=MockHardware, maintenance: bool = False,
                    dwell: float = 0.05, purchase_fake=None, leds=None,
                    **timings: float) -> Daemon:
        config = make_config(**timings)
        config.hardware.mode = mode
        purchase = (MockPurchaseServer() if purchase_fake is None
                    else http_purchase(config, purchase_fake))
        runtime = Runtime(config.database.path.parent / "runtime.json", out_of_order=maintenance)
        daemon = Daemon(config, hardware_cls(config.vending.levels), purchase,
                        leds=None if leds is None else leds(config), runtime=runtime)
        daemon.recovery_check_s = 0.02
        daemon.recovery_dwell_s = dwell
        await daemon.start()
        started.append(daemon)
        return daemon

    yield _make
    for daemon in started:
        await daemon.stop()


@pytest.fixture
async def daemon(make_daemon):
    return await make_daemon()


async def test_status_endpoint(client, daemon):
    body = await status(client, daemon)
    assert set(body) == STATUS_KEYS
    assert body["state"] == "idle" and body["hardware_mode"] == "mock" and body["reason"] is None
    assert body["purchase_mode"] == "mock"
    assert body["name"] == "Monitoni" and body["location"] == ""
    assert body["purchase_server"] == {"reachable": True, "since": None, "last_ok": None,
                                       "last_error": None, "outbox_pending": 0,
                                       "base_url": "monitoni.zhdk.ch"}
    assert body["levels"] == 10 and set(body["doors"]) == {str(n) for n in range(1, 11)}
    assert body["hardware"]["mode"] == "mock" and body["motor"]["running"] is False
    assert set(body["doors"].values()) == {"locked"}
    assert body["qr_url"] is None and body["selected_level"] is None
    assert body["countdown_s"] is not None  # idle has a sleep timeout
    assert body["leds"] == {"enabled": True, "reachable": True, "since": None, "pattern": "idle",
                            "level": None, "brightness": 0.6}
    assert body["audio"] == {"enabled": True, "available": True, "since": None, "volume": 0.7,
                             "playing": None}


async def test_index_serves_html(client, daemon):
    async with client.get(daemon.url + "/") as resp:
        assert resp.status == 200 and resp.content_type == "text/html"
        assert "<title>MoniToni</title>" in await resp.text()


async def test_happy_path(client, daemon):
    code, body = await command(client, daemon, command="select_level", level=3)
    assert code == 200
    assert body["state"] == "checking_purchase" and body["selected_level"] == 3
    assert body["qr_url"] == "/api/qr/3.svg" and body["purchase_id"]

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
    assert {r["kind"] for r in rows} >= {"daemon", "dev", "hardware", "purchase_check", "outbox"}
    await wait_until(lambda: daemon.purchase.reports == ["complete", "close"], "reports")


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


async def test_simulate_door_needs_mock_hardware_but_payment_only_the_mock_purchase_server(
        client, make_daemon):
    daemon = await make_daemon(mode="real")
    code, body = await command(client, daemon, command="simulate_door", open=True)
    assert code == 403 and "mock hardware" in body["error"]
    code, body = await command(client, daemon, command="select_level", level=1)
    assert code == 200 and body["hardware_mode"] == "real" and body["purchase_mode"] == "mock"
    code, _ = await command(client, daemon, command="simulate_payment")
    assert code == 200
    await wait_for_state(client, daemon, "door_unlocked")


async def test_forced_door_path(client, daemon):
    code, _ = await command(client, daemon, command="simulate_door", open=True)
    assert code == 200
    body = await wait_for_state(client, daemon, "door_forced")
    assert body["countdown_s"] is None and daemon.hardware.status()["alarm"] is True
    code, _ = await command(client, daemon, command="select_level", level=2)
    assert code == 409
    await command(client, daemon, command="simulate_door", open=False)
    body = await wait_for_state(client, daemon, "idle")
    assert daemon.hardware.status()["alarm"] is False and set(body["doors"].values()) == {"locked"}
    rows = await events(client, daemon)
    assert [r["details"]["to"] for r in reversed(rows) if r["kind"] == "transition"] == [
        "door_forced", "idle"]
    assert not [r for r in rows if r["kind"] == "outbox"]


async def test_qr_svg_is_a_bare_path_without_a_quiet_zone(client, daemon):
    async with client.get(daemon.url + "/api/qr/3.svg") as resp:
        assert resp.status == 200 and resp.content_type == "image/svg+xml"
        svg = await resp.text()
    assert svg.startswith("<svg ") and svg.endswith("</svg>") and svg.count("<path ") == 1
    assert 'viewBox="0 0 29 29"' in svg  # 29 modules for this data: version 3, border 0
    assert ' d="M0,0H1V1H0z' in svg  # the first module sits at the origin: no quiet zone
    assert "fill=" not in svg and "id=" not in svg  # the page colours it
    async with client.get(daemon.url + "/api/qr/3.json") as resp:
        assert resp.status == 200
        assert await resp.json() == {"level": 3, "data": "https://www.monitoni.zhdk.ch?level=3"}
    async with client.get(daemon.url + "/api/qr/11.json") as resp:
        assert resp.status == 404
    for bad in ("0", "11", "abc"):
        async with client.get(daemon.url + f"/api/qr/{bad}.svg") as resp:
            assert resp.status == 404, bad
    async with client.get(daemon.url + "/api/qr/3.png") as resp:
        assert resp.status == 404  # the PNG route is gone


def test_qr_data_matches_the_old_machines():
    assert qr_data("https://www.monitoni.zhdk.ch", 3) == "https://www.monitoni.zhdk.ch?level=3"


async def test_simulate_server_flips_the_mock_purchase_server(client, daemon):
    code, body = await command(client, daemon, command="simulate_server", reachable=False)
    assert code == 200
    ps = body["purchase_server"]
    assert ps["reachable"] is False and ps["since"] and ps["last_error"] == "simulated: unreachable"
    with pytest.raises(PurchaseServerError):
        await daemon.purchase.permission()
    await command(client, daemon, command="select_level", level=3)
    await command(client, daemon, command="simulate_payment")
    await asyncio.sleep(0.05)  # several polls, all failing
    assert (await status(client, daemon))["state"] == "checking_purchase"
    code, body = await command(client, daemon, command="simulate_server", reachable=True)
    assert code == 200 and body["purchase_server"]["reachable"] is True
    await wait_for_state(client, daemon, "door_unlocked")  # the pending payment went through
    rows = [r["details"] for r in reversed(await events(client, daemon)) if r["kind"] == "network"]
    assert rows == [{"purchase_server": "unreachable", "error": "simulated: unreachable"},
                    {"purchase_server": "reachable", "error": None}]
    code, _ = await command(client, daemon, command="simulate_server", reachable="no")
    assert code == 400


async def test_simulate_server_needs_the_mock_purchase_server(client, make_daemon, purchase_fake):
    daemon = await make_daemon(purchase_fake=purchase_fake)
    code, body = await command(client, daemon, command="simulate_server", reachable=False)
    assert code == 403 and "mock purchase server" in body["error"]


async def test_events_filters_before_and_summary_over_http(client, daemon):
    await command(client, daemon, command="select_level", level=2)
    await command(client, daemon, command="simulate_payment")
    await wait_for_state(client, daemon, "door_unlocked")
    await command(client, daemon, command="simulate_door", open=True)
    await command(client, daemon, command="simulate_door", open=False)
    await wait_for_state(client, daemon, "idle")
    await wait_until(lambda: daemon.purchase.reports == ["complete", "close"], "reports")
    async with client.get(daemon.url + "/api/events?filter=vends&limit=3") as resp:
        vends = await resp.json()
    assert len(vends) == 3 and all(r["kind"] in ("transition", "outbox") for r in vends)
    async with client.get(daemon.url + f"/api/events?filter=vends&before={vends[-1]['id']}") as r:
        older = await r.json()
    assert older and older[0]["id"] < vends[-1]["id"]
    async with client.get(daemon.url + "/api/events?filter=network") as resp:
        assert await resp.json() == []
    async with client.get(daemon.url + "/api/events?filter=nope") as resp:
        assert resp.status == 400
    async with client.get(daemon.url + "/api/events?before=x") as resp:
        assert resp.status == 400
    async with client.get(daemon.url + "/api/events/summary") as resp:
        assert await resp.json() == {"vends_today": 1, "vends_total": 1, "alarms_today": 0,
                                     "faults_today": 0, "outbox_pending": 0}


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


# -- motor commands ------------------------------------------------------------

def switches(daemon) -> list[str]:
    return [c for c in daemon.hardware.calls if c.startswith("set_")]


async def test_motor_press_and_release(client, daemon):
    code, body = await command(client, daemon, command="motor_press")
    assert code == 200 and body["motor"] == {"pressed": True, "running": True, "spindle_open": True}
    assert body["hardware"]["motor"] == {"running": True, "spindle_open": True}
    code, body = await command(client, daemon, command="motor_release")
    assert code == 200
    assert body["motor"]["running"] is False and body["motor"]["spindle_open"] is False
    assert switches(daemon) == ["set_spindle(True)", "set_motor(True)",
                                "set_motor(False)", "set_spindle(False)"]
    rows = [r["details"] for r in reversed(await events(client, daemon)) if r["kind"] == "motor"]
    assert rows == [{"event": "start"}, {"event": "stop", "reason": "release"}]


async def test_motor_press_resets_the_sleep_timer(client, make_daemon):
    daemon = await make_daemon(sleep_timeout_s=0.3)
    await asyncio.sleep(0.2)
    await command(client, daemon, command="motor_press")
    await asyncio.sleep(0.2)
    assert (await status(client, daemon))["state"] == "idle"  # would have slept at 0.3 s
    await command(client, daemon, command="motor_release")


async def test_motor_changes_are_pushed_over_the_websocket(client, daemon):
    async with client.ws_connect(daemon.url + "/ws") as ws:
        await ws.receive_json(timeout=2)
        await command(client, daemon, command="motor_press")
        pushed = await ws.receive_json(timeout=0.5)  # well under the 1 s heartbeat
        assert pushed["motor"]["spindle_open"] is True
        await command(client, daemon, command="motor_release")


async def test_motor_commands_only_in_idle(client, daemon):
    await command(client, daemon, command="select_level", level=1)
    for cmd in ("motor_press", "motor_release"):
        code, body = await command(client, daemon, command=cmd)
        assert code == 409 and "only allowed in idle or settings" in body["error"], cmd
    assert switches(daemon) == []


async def test_leaving_idle_stops_the_motor(client, daemon):
    await command(client, daemon, command="motor_press")
    await command(client, daemon, command="select_level", level=2)
    await wait_until(lambda: not daemon.motor.active, "motor stop")
    assert switches(daemon)[-2:] == ["set_motor(False)", "set_spindle(False)"]
    rows = [r["details"] for r in await events(client, daemon) if r["kind"] == "motor"]
    assert rows[0] == {"event": "stop", "reason": "leave_idle"}


async def test_last_websocket_closing_stops_the_motor(client, daemon):
    ws = await client.ws_connect(daemon.url + "/ws")
    await ws.receive_json(timeout=2)
    await command(client, daemon, command="motor_press")
    await ws.close()
    await wait_until(lambda: not daemon.motor.active, "motor stop")
    rows = [r["details"] for r in await events(client, daemon) if r["kind"] == "motor"]
    assert rows[0] == {"event": "stop", "reason": "ws_closed"}


async def test_daemon_stop_stops_the_motor(client, make_config):
    config = make_config()
    hardware = MockHardware(config.vending.levels)
    daemon = Daemon(config, hardware, MockPurchaseServer())
    await daemon.start()
    await command(client, daemon, command="motor_press")
    await daemon.stop()
    calls = hardware.calls
    assert calls.index("set_motor(False)") < calls.index("stop")


async def test_motor_hardware_error_is_503_and_faults(client, make_daemon):
    daemon = await make_daemon(hardware_cls=FailingMotor)
    daemon.hardware.is_healthy = False  # otherwise the mock recovers in the same drainer pass
    code, body = await command(client, daemon, command="motor_press")
    assert code == 503 and "no response" in body["error"]
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "hardware" and body["motor"]["running"] is False
    rows = await events(client, daemon)
    fault = [r for r in rows if r["kind"] == "hardware" and r["details"].get("event") == "fault"]
    assert fault and fault[0]["details"]["error"].startswith("motor: ")


# -- hardware fault and recovery over the queue -----------------------------------

async def test_hardware_fault_and_recovery(client, daemon):
    daemon.hardware.is_healthy = False
    daemon.hardware.events.put_nowait(HardwareFault("relay_core: connection closed by the module"))
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "hardware" and set(body["doors"].values()) == {"locked"}
    await asyncio.sleep(0.1)
    assert (await status(client, daemon))["state"] == "out_of_order"  # not healthy yet
    daemon.hardware.is_healthy = True
    body = await wait_for_state(client, daemon, "idle")
    assert body["reason"] is None
    rows = await events(client, daemon)
    fault = [r for r in rows if r["kind"] == "hardware" and r["details"]["event"] == "fault"]
    assert fault and fault[0]["details"]["error"].startswith("relay_core:")
    transitions = [r["details"]["event"] for r in reversed(rows) if r["kind"] == "transition"]
    assert transitions == ["hardware_fault", "hardware_ok"]


async def test_recovery_waits_for_the_dwell(client, make_daemon):
    daemon = await make_daemon(dwell=0.3)
    daemon.hardware.is_healthy = False
    daemon.hardware.events.put_nowait(HardwareFault("relay_core: gone"))
    await wait_for_state(client, daemon, "out_of_order")
    daemon.hardware.is_healthy = True
    await asyncio.sleep(0.15)
    assert (await status(client, daemon))["state"] == "out_of_order"  # healthy, but not long enough
    await wait_for_state(client, daemon, "idle")


async def test_a_blip_inside_the_dwell_restarts_it(client, make_daemon):
    daemon = await make_daemon(dwell=0.3)
    daemon.hardware.is_healthy = False
    daemon.hardware.events.put_nowait(HardwareFault("relay_core: gone"))
    await wait_for_state(client, daemon, "out_of_order")
    daemon.hardware.is_healthy = True
    await asyncio.sleep(0.2)
    daemon.hardware.is_healthy = False  # one unhealthy check
    await asyncio.sleep(0.05)
    daemon.hardware.is_healthy = True
    await asyncio.sleep(0.2)  # 0.45 s after the first healthy check, 0.2 s after the blip
    assert (await status(client, daemon))["state"] == "out_of_order"
    await wait_for_state(client, daemon, "idle")


async def test_no_recovery_attempt_while_in_maintenance(client, make_daemon):
    daemon = await make_daemon(maintenance=True)
    await asyncio.sleep(0.1)
    body = await status(client, daemon)
    assert body["state"] == "out_of_order" and body["reason"] == "maintenance"
    assert daemon.hardware.calls.count("lock_all_doors") == 1


# -- the HTTP purchase server, seen from the daemon ------------------------------------

async def test_http_purchase_server_in_the_status_and_the_log(client, make_daemon, purchase_fake):
    daemon = await make_daemon(purchase_fake=purchase_fake)
    body = await status(client, daemon)
    assert body["purchase_mode"] == "real" and body["purchase_server"]["reachable"] is None
    await command(client, daemon, command="select_level", level=2)
    await wait_until(lambda: purchase_fake.requests, "first poll")
    assert purchase_fake.requests[0] == {"method": "GET", "path": "/api/vending/permission",
                                         "token_ok": True, "body": b""}
    body = await status(client, daemon)
    assert body["purchase_server"]["reachable"] is True and body["purchase_server"]["last_ok"]
    assert "token" not in body["purchase_server"] and "test-token" not in str(body)
    rows = [r["details"] for r in await events(client, daemon) if r["kind"] == "network"]
    assert rows == [{"purchase_server": "reachable", "error": None}]
    code, body = await command(client, daemon, command="simulate_payment")
    assert code == 403 and "mock purchase server" in body["error"]


async def test_unreachable_purchase_server_keeps_polling_and_logs_once(client, make_daemon,
                                                                       purchase_fake):
    daemon = await make_daemon(purchase_fake=purchase_fake)
    purchase_fake.fail_next = 5
    await command(client, daemon, command="select_level", level=1)
    await wait_until(lambda: len(purchase_fake.requests) >= 5, "five failed polls")
    body = await status(client, daemon)
    assert body["state"] == "checking_purchase"
    assert body["purchase_server"]["reachable"] is False
    assert body["purchase_server"]["last_error"] == "HTTP 500 from /api/vending/permission"
    purchase_fake.pay(1)
    body = await wait_for_state(client, daemon, "door_unlocked")
    assert body["selected_level"] == 1 and body["purchase_server"]["reachable"] is True
    rows = [r["details"]["purchase_server"] for r in reversed(await events(client, daemon))
            if r["kind"] == "network"]
    assert rows == ["unreachable", "reachable"]


# -- LEDs and audio in the status ----------------------------------------------------------

async def test_feedback_follows_a_vend_in_the_status(client, make_daemon):
    daemon = await make_daemon(door_alarm_delay_s=0.05)
    await command(client, daemon, command="select_level", level=3)
    body = await status(client, daemon)
    assert (body["leds"]["pattern"], body["leds"]["level"]) == ("selected", 3)
    await command(client, daemon, command="simulate_payment")
    body = await wait_for_state(client, daemon, "door_unlocked")
    assert (body["leds"]["pattern"], body["leds"]["level"]) == ("unlocked", 3)
    assert body["audio"]["playing"] == "success"
    await command(client, daemon, command="simulate_door", open=True)
    body = await wait_for_state(client, daemon, "door_alarm")
    assert body["leds"]["pattern"] == "alarm" and body["audio"]["playing"] == "alarm"
    await command(client, daemon, command="simulate_door", open=False)
    body = await wait_for_state(client, daemon, "idle")
    assert body["leds"]["pattern"] == "idle" and body["audio"]["playing"] is None


async def test_wled_reachability_is_a_network_row_and_status_only(client, make_daemon,
                                                                   make_config):
    from monitoni.leds import ArtnetLeds
    from tests.fake_artnet import FakeArtnet, parse_zones

    fake = FakeArtnet(parse_zones("10x12"))
    await fake.start()
    try:
        def artnet_leds(config):
            config.hardware.wled.ip_address, config.hardware.wled.port = "127.0.0.1", fake.port
            config.hardware.wled.pixel_count = 120
            config.hardware.wled.health_poll_s = 0.05
            return ArtnetLeds(config, health_url=f"http://127.0.0.1:{fake.port}/json/info")

        daemon = await make_daemon(leds=artnet_leds)
        body = await wait_until_reachable(client, daemon, True)
        assert body["state"] == "idle" and body["leds"]["pattern"] == "idle"
        await wait_until(lambda: fake.frames_received >= 1, "a frame")
        fake.http_up = False
        body = await wait_until_reachable(client, daemon, False)
        assert body["state"] == "idle" and body["reason"] is None  # never a fault
        fake.http_up = True
        await wait_until_reachable(client, daemon, True)
        rows = [r["details"] for r in reversed(await events(client, daemon))
                if r["kind"] == "network"]
        assert rows == [{"component": "wled", "reachable": True},
                        {"component": "wled", "reachable": False},
                        {"component": "wled", "reachable": True}]
    finally:
        await fake.stop()


async def wait_until_reachable(client, daemon, value):
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        body = await status(client, daemon)
        if body["leds"]["reachable"] is value:
            return body
        assert asyncio.get_running_loop().time() < deadline, f"leds.reachable never {value}"
        await asyncio.sleep(0.01)


async def test_a_lost_report_is_not_recovered_by_the_drainer(client, make_daemon, monkeypatch):
    daemon = await make_daemon(dwell=0.02)

    async def refuse(kind, level):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(daemon.outbox, "enqueue", refuse)
    await command(client, daemon, command="select_level", level=1)
    await command(client, daemon, command="simulate_payment")
    await wait_for_state(client, daemon, "door_unlocked")
    await command(client, daemon, command="simulate_door", open=True)
    await command(client, daemon, command="simulate_door", open=False)
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "database" and body["leds"]["pattern"] == "fault"
    await asyncio.sleep(0.2)  # hardware is healthy the whole time; the dwell would have passed
    body = await status(client, daemon)
    assert body["state"] == "out_of_order" and body["reason"] == "database"
    code, body = await command(client, daemon, command="select_level", level=1)
    assert code == 409
