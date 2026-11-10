"""The hold-to-turn sequence with mock hardware and short delays (pre 20 ms, run-on 40)."""

import asyncio

import pytest

from monitoni.eventlog import EventLog
from monitoni.hardware.base import HardwareError, HardwareFault
from monitoni.hardware.mock import MockHardware
from monitoni.motor import Motor


class FailingMotor(MockHardware):
    async def set_motor(self, on: bool) -> None:
        if on:
            self._record("set_motor(True) failed")
            raise HardwareError("relay_core: no response")
        await super().set_motor(on)


class FailingLock(MockHardware):
    """The first lock write (the one at release) fails; the emergency path's close succeeds."""

    failed = False

    async def set_spindle(self, on: bool) -> None:
        if not on and not self.failed:
            self.failed = True
            self._record("set_spindle(False) failed")
            raise HardwareError("relay_core: no response")
        await super().set_spindle(on)


class StampedMock(MockHardware):
    """Records when each switch was written, so a test can read the gap between two."""

    def __init__(self, levels: int) -> None:
        super().__init__(levels)
        self.stamps: dict[str, float] = {}

    def _record(self, call: str) -> None:
        super()._record(call)
        self.stamps[call] = now()


@pytest.fixture
async def make_motor(make_config, tmp_path):
    started = []

    async def _make(hardware_cls=MockHardware, max_run_s: float = 10.0) -> Motor:
        cfg = make_config().hardware.motor
        cfg.spindle_pre_delay_ms, cfg.spin_after_release_ms = 20, 40
        cfg.max_run_s = max_run_s
        events = EventLog(tmp_path / "events.db")
        await events.start()
        hardware = hardware_cls(10)
        motor = Motor(cfg, hardware, events, hardware.events, lambda: "idle")
        started.append((motor, events))
        return motor

    yield _make
    for motor, events in started:
        await motor.stop("test teardown")
        await events.stop()


@pytest.fixture
async def motor(make_motor):
    return await make_motor()


def switches(motor: Motor) -> list[str]:
    return [c for c in motor.hardware.calls if c.startswith("set_")]


async def motor_rows(motor: Motor) -> list[dict]:
    return [r["details"] for r in reversed(await motor.events.recent(20)) if r["kind"] == "motor"]


def now() -> float:
    return asyncio.get_running_loop().time()


async def test_press_then_release_runs_the_full_sequence(make_motor):
    motor = await make_motor(hardware_cls=StampedMock)
    t0 = now()
    await motor.press()
    assert now() - t0 >= 0.02  # spindle pre-delay
    assert switches(motor) == ["set_spindle(True)", "set_motor(True)"]
    assert motor.running and motor.spindle_open and motor.pressed and motor.active
    t1 = now()
    await motor.release()
    assert now() - t1 >= 0.04  # the run-on
    assert switches(motor) == ["set_spindle(True)", "set_motor(True)",
                               "set_spindle(False)", "set_motor(False)"]  # lock first
    assert not motor.active
    stamps = motor.hardware.stamps
    gap = stamps["set_motor(False)"] - stamps["set_spindle(False)"]
    assert 0.04 <= gap < 0.1
    rows = await motor_rows(motor)
    assert rows[0] == {"event": "start"}
    assert rows[1]["event"] == "stop" and rows[1]["reason"] == "release"
    assert 40 <= rows[1]["run_on_ms"] < 100 and abs(rows[1]["run_on_ms"] - gap * 1000) < 5
    assert (await motor.events.recent(1))[0]["state"] == "idle"


async def test_release_during_pre_delay_never_starts_the_motor(motor):
    pressing = asyncio.create_task(motor.press())
    await asyncio.sleep(0.005)
    t0 = now()
    await motor.release()
    await pressing
    assert now() - t0 < 0.035  # the rest of the pre-delay (press holds the lock), no run-on
    assert switches(motor) == ["set_spindle(True)", "set_spindle(False)"]
    assert not motor.active and await motor_rows(motor) == []


async def test_press_and_release_are_idempotent(motor):
    await motor.release()
    assert switches(motor) == []
    await motor.press()
    await motor.press()
    assert switches(motor) == ["set_spindle(True)", "set_motor(True)"]
    await motor.release()
    await motor.release()
    assert len(switches(motor)) == 4


async def test_watchdog_stops_a_motor_that_is_never_released(make_motor):
    motor = await make_motor(max_run_s=0.1)
    await motor.press()
    await asyncio.sleep(0.2)
    assert not motor.active
    assert switches(motor)[-2:] == ["set_motor(False)", "set_spindle(False)"]  # motor off first
    assert (await motor_rows(motor))[-1] == {"event": "stop", "reason": "watchdog"}  # no run_on_ms
    await motor.release()  # the late release does nothing
    assert len(switches(motor)) == 4


async def test_stop_is_motor_off_then_lock_with_no_wait(motor):
    await motor.press()
    t0 = now()
    await motor.stop("leave_idle")
    assert now() - t0 < 0.02  # no run-on
    assert switches(motor)[-2:] == ["set_motor(False)", "set_spindle(False)"]
    assert not motor.active
    assert (await motor_rows(motor))[-1] == {"event": "stop", "reason": "leave_idle"}


async def test_stop_cuts_a_running_release_short(motor):
    await motor.press()
    releasing = asyncio.create_task(motor.release())
    await asyncio.sleep(0.005)  # the lock is closed, release is in its 40 ms run-on
    assert not motor.spindle_open and motor.running
    t0 = now()
    await motor.stop("daemon_stop")
    await releasing
    assert now() - t0 < 0.02
    assert switches(motor) == ["set_spindle(True)", "set_motor(True)",
                               "set_spindle(False)", "set_motor(False)"]
    rows = await motor_rows(motor)
    assert rows[-1]["reason"] == "release" and rows[-1]["run_on_ms"] < 40  # cut short


async def test_stop_without_a_press_does_nothing(motor):
    await motor.stop("ws_closed")
    assert switches(motor) == []


async def test_hardware_error_switches_both_off_then_raises(make_motor):
    motor = await make_motor(hardware_cls=FailingMotor)
    with pytest.raises(HardwareError, match="no response"):
        await motor.press()
    assert switches(motor) == ["set_spindle(True)", "set_motor(True) failed",
                               "set_motor(False)", "set_spindle(False)"]
    assert not motor.active
    fault = motor.hardware.events.get_nowait()
    assert isinstance(fault, HardwareFault) and fault.message.startswith("motor: ")
    assert await motor_rows(motor) == []


async def test_failed_lock_write_at_release_switches_the_motor_off_and_raises(make_motor):
    motor = await make_motor(hardware_cls=FailingLock)
    await motor.press()
    t0 = now()
    with pytest.raises(HardwareError, match="no response"):
        await motor.release()
    assert now() - t0 < 0.02  # no run-on on the way to the emergency off
    assert switches(motor) == ["set_spindle(True)", "set_motor(True)", "set_spindle(False) failed",
                               "set_motor(False)", "set_spindle(False)"]  # the emergency off
    assert not motor.active
    fault = motor.hardware.events.get_nowait()
    assert isinstance(fault, HardwareFault) and fault.message.startswith("motor: ")
    assert await motor_rows(motor) == [{"event": "start"}]  # no stop row: the sequence failed


# -- the settings tool: set_spindle through the motor module -----------------------------

async def test_set_spindle_is_the_one_owner_of_the_spindle_state(make_motor):
    motor = await make_motor()
    await motor.set_spindle(True)
    assert motor.spindle_open and motor.active and motor.hardware.calls[-1] == "set_spindle(True)"
    assert (await motor_rows(motor))[-1] == {"event": "spindle", "open": True}
    await motor.set_spindle(False)
    assert not motor.spindle_open and not motor.active
    await motor.set_spindle(True)
    await motor.stop("leave_settings")  # what the daemon does on every transition
    assert not motor.spindle_open and motor.hardware.calls[-1] == "set_spindle(False)"


async def test_set_spindle_is_refused_while_turn_is_held(make_motor):
    from monitoni.motor import MotorBusy

    motor = await make_motor()
    await motor.press()
    with pytest.raises(MotorBusy, match="release TURN first"):
        await motor.set_spindle(False)
    assert motor.running and motor.spindle_open
    await motor.stop("test")


async def test_reset_forgets_the_sequence_state(make_motor):
    motor = await make_motor()
    await motor.press()
    assert motor.running and motor.spindle_open and motor._watchdog is not None
    motor.reset()  # relay_core came back with motor off and spindle closed
    assert not motor.active and motor.status() == {"pressed": False, "running": False,
                                                   "spindle_open": False}
    assert motor._watchdog is None
    await motor.stop("test")  # nothing on, nothing written
    assert motor.hardware.calls[-1] == "set_motor(True)"
