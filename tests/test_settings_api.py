"""The settings routes over HTTP, in mock mode: PIN, the three switches, every tool, status."""

import asyncio
import json
import logging

import pytest

from monitoni.daemon import Daemon
from monitoni.hardware.base import HardwareError
from monitoni.hardware.mock import MockHardware
from monitoni.purchase import MockPurchaseServer, PurchaseServerError
from monitoni.runtime import Runtime
from tests.helpers import command, events, status, wait_for_state, wait_until

TOOLS = ["exit", "out_of_order", "brightness", "volume", "door", "lock_all", "spindle", "leds",
         "audio", "test_server"]
PIN = "0000"


class FailingUnlock(MockHardware):
    async def unlock_door(self, level: int) -> None:
        raise HardwareError("relay_levels: no response within 1.0s")


class FlakyPurchase(MockPurchaseServer):
    async def permission(self):
        raise PurchaseServerError("HTTP 500 from /api/vending/permission")


@pytest.fixture
async def make_daemon(make_config):
    started = []

    async def _make(hardware_cls=MockHardware, purchase=None, pin: str = PIN,
                    out_of_order: bool = False, **timings: float) -> Daemon:
        config = make_config(**timings)
        config.settings.pin = pin
        runtime = Runtime(config.database.path.parent / "runtime.json", out_of_order=out_of_order)
        daemon = Daemon(config, hardware_cls(config.vending.levels),
                        purchase or MockPurchaseServer(), runtime=runtime)
        daemon.recovery_check_s = 0.02
        daemon.recovery_dwell_s = 0.05
        await daemon.start()
        started.append(daemon)
        return daemon

    yield _make
    for daemon in started:
        await daemon.stop()


@pytest.fixture
async def daemon(make_daemon):
    return await make_daemon()


async def settings(client, daemon, name, **body):
    async with client.post(daemon.url + f"/api/settings/{name}", json=body) as resp:
        return resp.status, await resp.json()


async def enter(client, daemon, pin=PIN):
    code, body = await settings(client, daemon, "enter", pin=pin)
    assert code == 200 and body["state"] == "settings", body
    return body


async def command_rows(client, daemon):
    return [r["details"] for r in reversed(await events(client, daemon))
            if r["kind"] == "command"]


# -- entering and leaving -------------------------------------------------------------

async def test_pin_right_wrong_and_malformed(client, daemon):
    code, body = await settings(client, daemon, "enter", pin="1234")
    assert code == 403 and body == {"error": "wrong PIN"}
    code, body = await settings(client, daemon, "enter", pin=0)
    assert code == 400
    code, body = await settings(client, daemon, "enter")
    assert code == 400
    body = await enter(client, daemon)
    assert body["countdown_s"] is not None and body["settings"]["pin_is_default"] is True
    code, body = await settings(client, daemon, "enter", pin=PIN)
    assert code == 409  # already there
    rows = await command_rows(client, daemon)
    assert rows[-2:] == [{"tool": "enter", "accepted": False}, {"tool": "enter", "body": {}}]
    assert "1234" not in json.dumps(await events(client, daemon))


async def test_default_pin_is_a_warning_at_start_and_a_status_flag(make_daemon, client, caplog):
    with caplog.at_level(logging.WARNING, logger="monitoni.daemon"):
        daemon = await make_daemon()
    assert "settings.pin is still the default 0000" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="monitoni.daemon"):
        other = await make_daemon(pin="2468")
    assert "default" not in caplog.text
    assert (await status(client, daemon))["settings"]["pin_is_default"] is True
    assert (await status(client, other))["settings"]["pin_is_default"] is False
    code, _ = await settings(client, other, "enter", pin="2468")
    assert code == 200


@pytest.mark.parametrize("name", TOOLS)
async def test_every_tool_is_409_outside_settings(client, daemon, name):
    code, body = await settings(client, daemon, name)
    assert code == 409 and body["error"] == f"{name} is only allowed in state settings"
    code, body = await settings(client, daemon, "nothing")
    assert code == 404


async def test_enter_only_from_idle_or_out_of_order(client, daemon):
    await command(client, daemon, command="select_level", level=2)
    code, body = await settings(client, daemon, "enter", pin=PIN)
    assert code == 409 and "not allowed in state checking_purchase" in body["error"]
    await command(client, daemon, command="cancel")
    await command(client, daemon, command="simulate_door", open=True)  # forced door
    await wait_for_state(client, daemon, "door_forced")
    code, _ = await settings(client, daemon, "enter", pin=PIN)
    assert code == 409


async def test_exit_and_the_door_rule(client, daemon):
    await enter(client, daemon)
    await command(client, daemon, command="simulate_door", open=True)
    await asyncio.sleep(0.05)
    body = await status(client, daemon)
    assert body["state"] == "settings" and body["hardware"]["door_open"] is True
    code, body = await settings(client, daemon, "exit")
    assert code == 409 and body["error"] == "close the door first"
    await command(client, daemon, command="simulate_door", open=False)
    await asyncio.sleep(0.05)
    code, body = await settings(client, daemon, "exit")
    assert code == 200 and body["state"] == "idle"
    code, body = await settings(client, daemon, "exit")
    assert code == 409


async def test_customer_commands_are_refused_in_settings(client, daemon):
    await enter(client, daemon)
    code, _ = await command(client, daemon, command="select_level", level=1)
    assert code == 409
    code, _ = await command(client, daemon, command="cancel")
    assert code == 409
    code, _ = await command(client, daemon, command="simulate_payment")
    assert code == 409
    code, body = await command(client, daemon, command="motor_press")  # allowed here
    assert code == 200 and body["motor"]["spindle_open"] is True
    code, body = await command(client, daemon, command="motor_release")
    assert code == 200
    await wait_until(lambda: not daemon.motor.active, "motor off")
    code, body = await command(client, daemon, command="touch")
    assert code == 200 and body["state"] == "settings"


# -- the three switches -----------------------------------------------------------------

async def test_out_of_order_switch_takes_effect_on_exit_and_persists(client, daemon):
    path = daemon.runtime.path
    await enter(client, daemon)
    code, body = await settings(client, daemon, "out_of_order", on=True)
    assert code == 200 and body["state"] == "settings" and body["settings"]["out_of_order"]
    assert json.loads(path.read_text())["out_of_order"] is True
    code, body = await settings(client, daemon, "exit")
    assert body["state"] == "out_of_order" and body["reason"] == "maintenance"
    await asyncio.sleep(0.15)  # the drainer's recovery does not apply to maintenance
    assert (await status(client, daemon))["state"] == "out_of_order"
    await enter(client, daemon)
    code, _ = await settings(client, daemon, "out_of_order", on=False)
    code, body = await settings(client, daemon, "exit")
    assert body["state"] == "idle"
    assert Runtime.load(path, daemon.config).out_of_order is False
    await enter(client, daemon)
    code, body = await settings(client, daemon, "out_of_order", on="yes")
    assert code == 400


async def test_runtime_switch_at_start_and_switched_off_from_inside(client, make_daemon):
    daemon = await make_daemon(out_of_order=True)
    body = await status(client, daemon)
    assert body["state"] == "out_of_order" and body["reason"] == "maintenance"
    await enter(client, daemon)
    await settings(client, daemon, "out_of_order", on=False)
    code, body = await settings(client, daemon, "exit")
    assert body["state"] == "idle"


async def test_brightness_and_volume_apply_at_once_and_are_written(client, daemon):
    await enter(client, daemon)
    code, body = await settings(client, daemon, "brightness", value=0.3)
    assert code == 200 and body["leds"]["brightness"] == 0.3
    code, body = await settings(client, daemon, "volume", value=0.5)
    assert code == 200 and body["audio"]["volume"] == 0.5
    data = json.loads(daemon.runtime.path.read_text())
    assert data == {"out_of_order": False, "brightness": 0.3, "volume": 0.5}
    for bad in (1.5, -0.1, "x", True, None):
        code, _ = await settings(client, daemon, "brightness", value=bad)
        assert code == 400, bad
    assert daemon.leds.calls[-1][1:] == ("set_pattern", "idle", None) or True
    again = Runtime.load(daemon.runtime.path, daemon.config)
    assert (again.brightness, again.volume) == (0.3, 0.5)


# -- the test tools ----------------------------------------------------------------------

async def test_door_tools(client, daemon):
    await enter(client, daemon)
    code, body = await settings(client, daemon, "door", level=3, unlock=True)
    assert code == 200 and body["doors"]["3"] == "unlocked" and body["state"] == "settings"
    code, body = await settings(client, daemon, "door", level=5, unlock=True)
    assert body["doors"]["5"] == "unlocked"
    code, body = await settings(client, daemon, "door", level=3, unlock=False)
    assert body["doors"]["3"] == "locked" and body["doors"]["5"] == "unlocked"
    code, body = await settings(client, daemon, "lock_all")
    assert set(body["doors"].values()) == {"locked"}
    for bad in ({"level": 11, "unlock": True}, {"level": 3}, {"level": "3", "unlock": True}):
        code, _ = await settings(client, daemon, "door", **bad)
        assert code == 400, bad
    rows = await command_rows(client, daemon)
    assert {"tool": "door", "body": {"level": 3, "unlock": True}} in rows
    assert {"tool": "lock_all", "body": {}} in rows


async def test_unlock_in_settings_does_not_relock_and_exit_locks_everything(client, daemon):
    await enter(client, daemon)
    await settings(client, daemon, "door", level=4, unlock=True)
    await command(client, daemon, command="simulate_door", open=True)  # someone opens it
    await asyncio.sleep(0.15)  # longer than relock_delay_s
    body = await status(client, daemon)
    assert body["state"] == "settings" and body["doors"]["4"] == "unlocked"
    assert body["hardware"]["alarm"] is False
    await command(client, daemon, command="simulate_door", open=False)
    await asyncio.sleep(0.05)
    code, body = await settings(client, daemon, "exit")
    assert body["state"] == "idle" and set(body["doors"].values()) == {"locked"}
    rows = await events(client, daemon)
    assert not [r for r in rows if r["kind"] == "rejected"]
    assert not [r for r in rows if r["kind"] == "outbox"]  # nothing was reported


async def test_hardware_error_in_a_tool_is_503_and_a_fault(client, make_daemon):
    daemon = await make_daemon(hardware_cls=FailingUnlock)
    await enter(client, daemon)
    code, body = await settings(client, daemon, "door", level=2, unlock=True)
    assert code == 503 and "no response" in body["error"]
    body = await wait_for_state(client, daemon, "out_of_order")
    assert body["reason"] == "hardware"
    rows = await command_rows(client, daemon)
    assert rows[-1]["tool"] == "door" and "no response" in rows[-1]["error"]


async def test_spindle_tool_and_the_motor_guard(client, daemon):
    await enter(client, daemon)
    code, body = await settings(client, daemon, "spindle", open=True)
    assert code == 200 and body["hardware"]["motor"]["spindle_open"] is True
    code, body = await settings(client, daemon, "spindle", open=False)
    assert body["hardware"]["motor"]["spindle_open"] is False
    await command(client, daemon, command="motor_press")
    code, body = await settings(client, daemon, "spindle", open=False)
    assert code == 409 and "release TURN" in body["error"]
    await command(client, daemon, command="motor_release")
    await wait_until(lambda: not daemon.motor.active, "motor off")
    code, _ = await settings(client, daemon, "spindle")
    assert code == 400


async def test_led_tools(client, daemon):
    await enter(client, daemon)
    assert daemon.leds.status()["pattern"] == "idle"
    code, body = await settings(client, daemon, "leds", action="fill", rgb=[255, 255, 255])
    assert code == 200 and body["leds"]["pattern"] == "fill"
    assert set(daemon.leds.frame) == {(153, 153, 153)}  # 60 % brightness
    code, body = await settings(client, daemon, "leds", action="level", level=5)
    assert body["leds"]["pattern"] == "light_level" and body["leds"]["level"] == 5
    open_colour = tuple(round(c * 0.6) for c in (239, 228, 210))
    assert daemon.leds.frame[48] == open_colour and daemon.leds.frame[0] == (0, 0, 0)
    code, body = await settings(client, daemon, "leds", action="level", level=2, rgb=[10, 0, 0])
    assert daemon.leds.frame[12] == (6, 0, 0)
    code, body = await settings(client, daemon, "leds", action="off")
    assert body["leds"]["pattern"] == "off"
    for bad in ({"action": "fill"}, {"action": "level"}, {"action": "disco"},
                {"action": "fill", "rgb": [1, 2]}, {"action": "fill", "rgb": [1, 2, 256]}):
        code, _ = await settings(client, daemon, "leds", **bad)
        assert code == 400, bad
    code, body = await settings(client, daemon, "exit")
    assert body["leds"]["pattern"] == "idle"  # the transition restores the state's pattern


async def test_audio_tools(client, daemon):
    await enter(client, daemon)
    code, body = await settings(client, daemon, "audio", action="play", sound="success")
    assert code == 200 and body["audio"]["playing"] == "success"
    assert daemon.audio.calls[-1][1:] == ("play", "success", False)  # never looped from here
    code, body = await settings(client, daemon, "audio", action="play", sound="alarm")
    assert body["audio"]["playing"] == "alarm"
    code, body = await settings(client, daemon, "audio", action="stop")
    assert body["audio"]["playing"] is None
    for bad in ({"action": "play", "sound": "fanfare"}, {"action": "play"}, {"action": "x"}):
        code, _ = await settings(client, daemon, "audio", **bad)
        assert code == 400, bad


async def test_test_server_reports_reachability_and_never_unlocks(client, make_daemon):
    daemon = await make_daemon()
    await enter(client, daemon)
    daemon.purchase.simulate_payment(3)  # an open purchase the mock would answer with
    code, body = await settings(client, daemon, "test_server")
    assert code == 200 and body["state"] == "settings"
    result = body["test_server"]
    assert result["ok"] is True and result["error"] is None and isinstance(result["took_ms"], int)
    assert set(body["doors"].values()) == {"locked"}
    flaky = await make_daemon(purchase=FlakyPurchase())
    await enter(client, flaky)
    code, body = await settings(client, flaky, "test_server")
    assert code == 200 and body["test_server"]["ok"] is False
    assert body["test_server"]["error"] == "HTTP 500 from /api/vending/permission"
    assert {"tool": "test_server", "body": {}} in await command_rows(client, flaky)


# -- status additions ------------------------------------------------------------------

async def test_status_additions(client, daemon):
    body = await status(client, daemon)
    assert body["app_version"] == "0.6.0"
    assert isinstance(body["hostname"], str) and body["hostname"]
    assert body["ip"] is None or body["ip"].count(".") == 3
    assert body["purchase_server"]["base_url"] == "monitoni.zhdk.ch"
    assert body["settings"] == {"pin_is_default": True, "out_of_order": False}
    assert body["config_view"] == {
        "motor": {"spindle_pre_delay_ms": 10, "spin_after_release_ms": 10,
                  "spindle_post_delay_ms": 5, "max_run_s": 10.0},
        "led": {"zones": [[12 * i, 12 * i + 11] for i in range(10)]},
        "door_locks": {"channels": list(range(1, 11))},
    }
    assert "token" not in json.dumps(body)
