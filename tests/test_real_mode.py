"""The daemon in real mode against two fake Waveshare modules, end to end over HTTP."""

import pytest

from monitoni.daemon import Daemon
from monitoni.hardware.real import RealHardware
from monitoni.purchase import MockPurchaseServer
from tests.helpers import command, events, status, wait_for_state


def make_real_daemon(config) -> Daemon:
    daemon = Daemon(config, RealHardware(config), MockPurchaseServer())
    daemon.recovery_check_s = 0.02
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
    daemon.purchase.simulate_payment(level)
    return await wait_for_state(client, daemon, "door_unlocked")


async def test_happy_path_with_the_fake_door_sensor(client, daemon, fakes):
    core, levels = fakes
    body = await status(client, daemon)
    assert body["state"] == "idle" and body["hardware_mode"] == "real" and body["reason"] is None
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
    door_rows = [r["details"]["event"] for r in reversed(rows) if r["kind"] == "hardware"]
    assert door_rows == ["door_opened", "door_closed"]

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


async def test_turn_button_in_real_mode(client, daemon, fakes):
    core, levels = fakes
    code, body = await command(client, daemon, command="motor_press")
    assert code == 200 and core.coils[:2] == [True, True]
    assert body["hardware"]["motor"] == {"running": True, "spindle_open": True}
    code, body = await command(client, daemon, command="motor_release")
    assert code == 200 and core.coils[:2] == [False, False]
    assert body["hardware"]["motor"] == {"running": False, "spindle_open": False}
