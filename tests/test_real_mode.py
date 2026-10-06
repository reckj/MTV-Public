"""The daemon in real mode against two fake Waveshare modules, end to end over HTTP."""

import asyncio

import pytest

from monitoni.daemon import Daemon
from monitoni.hardware.real import RealHardware
from monitoni.purchase import MockPurchaseServer
from tests.conftest import http_purchase
from tests.helpers import command, events, status, wait_for_state, wait_until


def make_real_daemon(config, purchase_fake=None, leds=None) -> Daemon:
    purchase = (MockPurchaseServer() if purchase_fake is None
                else http_purchase(config, purchase_fake))
    daemon = Daemon(config, RealHardware(config), purchase, leds=leds)
    daemon.recovery_check_s = 0.02
    daemon.recovery_dwell_s = 0.05
    return daemon


@pytest.fixture
async def daemon(real_config):
    daemon = make_real_daemon(real_config)
    await daemon.start()
    yield daemon
    await daemon.stop()


async def to_door_unlocked(client, daemon, level: int = 3) -> dict:
    code, _ = await command(client, daemon, command="select_level", level=level)
    assert code == 200
    code, _ = await command(client, daemon, command="simulate_payment")  # the purchase mock
    assert code == 200
    return await wait_for_state(client, daemon, "door_unlocked")


async def test_happy_path_with_the_fake_door_sensor(client, daemon, fakes):
    core, levels = fakes
    body = await status(client, daemon)
    assert body["state"] == "idle" and body["hardware_mode"] == "real" and body["reason"] is None
    assert body["purchase_mode"] == "mock"
    assert body["hardware"]["relay_core"]["connected"]
    assert body["hardware"]["relay_levels"]["connected"]
    assert body["hardware"]["door_open"] is False and set(body["doors"].values()) == {"locked"}

    body = await to_door_unlocked(client, daemon)
    assert levels.coils[2] is True
    assert body["doors"]["3"] == "unlocked" and body["doors"]["4"] == "locked"

    core.inputs[0] = False  # door opened (di_active low)
    body = await wait_for_state(client, daemon, "door_opened")
    assert body["hardware"]["door_open"] is True
    core.inputs[0] = True
    body = await wait_for_state(client, daemon, "idle")
    assert levels.coils == [False] * 30 and set(body["doors"].values()) == {"locked"}

    rows = await events(client, daemon)
    transitions = [r["details"]["to"] for r in reversed(rows) if r["kind"] == "transition"]
    assert transitions == ["checking_purchase", "door_unlocked", "door_opened", "completing",
                           "idle"]
    hw_rows = [r["details"]["event"] for r in reversed(rows) if r["kind"] == "hardware"]
    assert hw_rows == ["known_state", "door_opened", "door_closed"]

    code, _ = await command(client, daemon, command="simulate_door", open=True)
    assert code == 403  # dev commands stay mock-only


async def test_levels_connection_lost_mid_unlock_then_recovers(client, daemon, fakes):
    core, levels = fakes
    await to_door_unlocked(client, daemon)
    await levels.drop_connections()
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "hardware" and body["selected_level"] is None
    assert body["hardware"]["relay_levels"]["last_error"] == "connection closed by the module"
    body = await wait_for_state(client, daemon, "idle")
    assert body["reason"] is None and body["hardware"]["relay_levels"]["connected"]
    assert levels.coils == [False] * 30 and set(body["doors"].values()) == {"locked"}
    rows = await events(client, daemon)
    kinds = [(r["kind"], r["details"].get("event")) for r in reversed(rows)]
    assert ("hardware", "fault") in kinds and ("transition", "hardware_ok") in kinds


async def test_starts_out_of_order_with_a_module_unreachable_and_recovers(client, real_config,
                                                                           fakes):
    core, levels = fakes
    port = levels.port
    await levels.stop()
    daemon = make_real_daemon(real_config)
    await daemon.start()
    try:
        body = await status(client, daemon)
        assert body["state"] == "out_of_order" and body["reason"] == "hardware"
        assert body["hardware"]["relay_levels"]["connected"] is False
        assert body["hardware"]["relay_levels"]["last_error"].startswith("connect failed")
        assert body["hardware"]["relay_core"]["connected"] is True
        assert set(body["doors"].values()) == {"unknown"}
        await levels.start(port=port)
        body = await wait_for_state(client, daemon, "idle")
        assert set(body["doors"].values()) == {"locked"} and levels.coils == [False] * 30
    finally:
        await daemon.stop()


async def test_forced_door_in_real_mode(client, daemon, fakes):
    core, levels = fakes
    core.inputs[0] = False  # the door opens while idle
    body = await wait_for_state(client, daemon, "door_forced")
    assert body["hardware"]["door_open"] is True and body["countdown_s"] is None
    core.inputs[0] = True
    body = await wait_for_state(client, daemon, "idle")
    assert levels.coils == [False] * 30 and set(body["doors"].values()) == {"locked"}


async def test_turn_button_in_real_mode(client, daemon, fakes):
    core, levels = fakes
    code, body = await command(client, daemon, command="motor_press")
    assert code == 200 and core.coils[:2] == [True, True]
    assert body["hardware"]["motor"] == {"running": True, "spindle_open": True}
    code, body = await command(client, daemon, command="motor_release")
    assert code == 200 and core.coils[:2] == [False, False]
    assert body["hardware"]["motor"] == {"running": False, "spindle_open": False}


# -- with the HTTP purchase fake as well: all three fakes ------------------------------

def requests_of(fake, path: str) -> list[dict]:
    return [r for r in fake.requests if r["path"] == path]


async def test_full_vend_against_the_purchase_fake(client, real_config, fakes, purchase_fake):
    core, levels = fakes
    daemon = make_real_daemon(real_config, purchase_fake)
    await daemon.start()
    try:
        await command(client, daemon, command="select_level", level=3)
        await wait_until(lambda: purchase_fake.requests, "first poll")
        assert purchase_fake.requests[0] == {"method": "GET", "path": "/api/vending/permission",
                                             "token_ok": True, "body": b""}
        purchase_fake.pay(3)
        body = await wait_for_state(client, daemon, "door_unlocked")
        assert body["selected_level"] == 3 and levels.coils[2] is True
        core.inputs[0] = False
        await wait_for_state(client, daemon, "door_opened")
        await wait_until(lambda: purchase_fake.report_kinds() == ["complete"], "complete")
        await wait_until(lambda: levels.coils[2] is False, "relock while the door is open")
        assert core.inputs[0] is False  # the DI still reads open
        body = await status(client, daemon)
        assert body["state"] == "door_opened" and body["doors"]["3"] == "locked"
        core.inputs[0] = True
        body = await wait_for_state(client, daemon, "idle")
        await wait_until(lambda: purchase_fake.report_kinds() == ["complete", "close"], "close")
        for path in ("/api/vending/complete", "/api/vending/close"):
            (req,) = requests_of(purchase_fake, path)
            assert req == {"method": "GET", "path": path, "token_ok": True, "body": b""}
        await wait_until(lambda: daemon.outbox.pending_count == 0, "outbox empty")
    finally:
        await daemon.stop()


async def test_reports_survive_an_outage_before_the_door_opens_and_a_restart(
        client, real_config, fakes, purchase_fake):
    core, levels = fakes
    port = purchase_fake.port
    daemon = make_real_daemon(real_config, purchase_fake)
    await daemon.start()
    try:
        await command(client, daemon, command="select_level", level=2)
        purchase_fake.pay(2)
        await wait_for_state(client, daemon, "door_unlocked")
        await purchase_fake.stop()  # the server goes away before the door opens
        core.inputs[0] = False
        await wait_for_state(client, daemon, "door_opened")
        core.inputs[0] = True
        body = await wait_for_state(client, daemon, "idle")
        await wait_until(lambda: daemon.outbox.pending_count == 2, "both reports queued")
        await wait_until(lambda: daemon.purchase.reachable is False, "unreachable noticed")
        body = await status(client, daemon)
        assert body["purchase_server"]["outbox_pending"] == 2
        assert purchase_fake.reports == []
    finally:
        await daemon.stop()

    daemon = make_real_daemon(real_config, purchase_fake)  # restart with the same database
    await daemon.start()
    try:
        assert daemon.outbox.pending_count == 2
        await asyncio.sleep(0.2)
        assert purchase_fake.reports == [] and daemon.outbox.pending_count == 2
        await purchase_fake.start(port=port)
        await wait_until(lambda: purchase_fake.report_kinds() == ["complete", "close"],
                         "delivery after the restart")
        await wait_until(lambda: daemon.outbox.pending_count == 0, "outbox empty")
    finally:
        await daemon.stop()


async def test_expired_permission_is_not_honoured(client, real_config, fakes, purchase_fake):
    daemon = make_real_daemon(real_config, purchase_fake)
    await daemon.start()
    try:
        purchase_fake.pay(4, ttl_s=0.05)
        await asyncio.sleep(0.1)  # the customer took too long: the permission is gone
        await command(client, daemon, command="select_level", level=4)
        await asyncio.sleep(0.1)
        body = await status(client, daemon)
        assert body["state"] == "checking_purchase" and body["purchase_server"]["reachable"]
        assert set(body["doors"].values()) == {"locked"}
        assert len(requests_of(purchase_fake, "/api/vending/permission")) >= 3
    finally:
        await daemon.stop()


async def test_wrong_token_is_unreachable_and_unlocks_nothing(client, real_config, fakes,
                                                             purchase_fake):
    daemon = Daemon(real_config, RealHardware(real_config),
                    http_purchase(real_config, purchase_fake, token="not-the-token"))
    daemon.recovery_check_s, daemon.recovery_dwell_s = 0.02, 0.05
    await daemon.start()
    try:
        purchase_fake.pay(1)
        await command(client, daemon, command="select_level", level=1)
        await asyncio.sleep(0.1)
        body = await status(client, daemon)
        assert body["state"] == "checking_purchase"
        assert body["purchase_server"]["reachable"] is False
        assert body["purchase_server"]["last_error"] == "HTTP 401 from /api/vending/permission"
        assert set(body["doors"].values()) == {"locked"} and fakes[1].coils == [False] * 30
        assert all(not r["token_ok"] for r in purchase_fake.requests)
    finally:
        await daemon.stop()


async def test_full_vend_lights_the_strip_through_the_fake_receiver(client, real_config, fakes):
    from monitoni.leds import ArtnetLeds, scale
    from tests.fake_artnet import FakeArtnet, parse_zones

    core, levels = fakes
    strip = FakeArtnet(parse_zones("10x12"))
    await strip.start()
    seen: list[list] = []  # the zone colours after every frame
    strip.on_frame = lambda: seen.append(strip.zone_colours())
    wled = real_config.hardware.wled
    wled.ip_address, wled.port, wled.pixel_count = "127.0.0.1", strip.port, 120
    wled.health_poll_s = 0.05
    real_config.vending.timings.door_alarm_delay_s = 0.3
    daemon = make_real_daemon(real_config, leds=ArtnetLeds(
        real_config, health_url=f"http://127.0.0.1:{strip.port}/json/info"))
    await daemon.start()
    try:
        idle, amber, cream, red = (scale(c, 0.6) for c in
                                   ((60, 40, 20), (233, 162, 59), (239, 228, 210), (239, 90, 106)))
        await wait_until(lambda: strip.zone_colours() == [idle] * 10, "idle on the strip")
        body = await status(client, daemon)
        assert body["leds"]["reachable"] is True and body["audio"]["available"] is True

        await to_door_unlocked(client, daemon, level=3)
        await wait_until(lambda: strip.zone_colour(3) != amber and strip.zone_colour(3) != idle,
                         "breathing")
        breathing = set()
        for _ in range(30):
            breathing.add(strip.zone_colour(3))
            await asyncio.sleep(0.02)
        assert len(breathing) > 5 and all(c[1:] != (0, 0) for c in breathing)  # amber, varying
        assert strip.zone_colours()[:2] == [(0, 0, 0)] * 2  # the other zones are off

        core.inputs[0] = False  # door opened
        await wait_for_state(client, daemon, "door_opened")
        await wait_until(lambda: strip.zone_colour(3) == cream, "open colour")
        assert strip.zone_colours() == [(0, 0, 0)] * 2 + [cream] + [(0, 0, 0)] * 7

        await wait_for_state(client, daemon, "door_alarm")
        flashing = set()
        for _ in range(30):
            flashing.add(tuple(strip.zone_colours()))
            await asyncio.sleep(0.02)
        assert flashing == {tuple([red] * 10), tuple([(0, 0, 0)] * 10)}  # all zones, on and off

        core.inputs[0] = True
        await wait_for_state(client, daemon, "idle")
        await wait_until(lambda: strip.zone_colours() == [idle] * 10, "back to idle", timeout=3)
        selected_frames = [z for z in seen if z[2] == amber and z[0] == idle]
        assert selected_frames, "the selected pattern was shown for zone 3"
    finally:
        await daemon.stop()
        await strip.stop()


async def test_spindle_opened_in_settings_reads_closed_after_core_reconnects(client, daemon,
                                                                            fakes):
    core, levels = fakes
    async with client.post(daemon.url + "/api/settings/enter", json={"pin": "0000"}) as resp:
        assert resp.status == 200
    async with client.post(daemon.url + "/api/settings/spindle", json={"open": True}) as resp:
        body = await resp.json()
    assert body["motor"]["spindle_open"] and body["hardware"]["motor"]["spindle_open"]
    assert core.coils[1] is True
    await core.drop_connections()
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "hardware"
    await wait_until(lambda: daemon.hardware.healthy(), "core back with a known state")
    await wait_until(lambda: not daemon.motor.spindle_open, "the owner reset")
    body = await status(client, daemon)
    assert body["motor"] == {"pressed": False, "running": False, "spindle_open": False}
    assert body["hardware"]["motor"] == {"running": False, "spindle_open": False}
    assert core.coils[:2] == [False, False]
    rows = [r["details"] for r in await events(client, daemon) if r["kind"] == "hardware"]
    assert {"event": "known_state", "module": "relay_core"} in rows
    body = await wait_for_state(client, daemon, "idle")
    assert set(body["doors"].values()) == {"locked"}
