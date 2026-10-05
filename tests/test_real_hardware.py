"""RealHardware against two fake Waveshare modules: read-back, polling, loss and recovery."""

import asyncio
import logging

import pytest

from monitoni.hardware.base import DoorEvent, HardwareError, HardwareFault
from monitoni.hardware.real import RealHardware
from tests.helpers import wait_until


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.fixture
async def hardware(real_config):
    hardware = RealHardware(real_config)
    await hardware.start()
    yield hardware
    await hardware.stop()


async def test_start_connects_reads_the_door_and_locks_everything(hardware, fakes):
    core, levels = fakes
    assert hardware.core.connected and hardware.levels.connected and hardware.healthy()
    assert core.requests[0][:6] == hx("010200000001")  # initial door read
    assert levels.requests[0][:6] == hx("010500FF0000")  # all coils off
    assert levels.requests[1][:6] == hx("01010000001E")  # read back all 30
    status = hardware.status()
    assert status["mode"] == "real" and status["door_open"] is False
    assert status["doors"] == dict.fromkeys(range(1, 11), "locked")
    assert status["relay_core"]["connected"] and status["relay_core"]["last_error"] is None
    assert status["motor"] == {"running": None, "spindle_open": None}


async def test_unlock_and_lock_with_read_back(hardware, fakes):
    core, levels = fakes
    await hardware.unlock_door(3)
    assert levels.coils[2] is True and not hardware.door_locked(3)
    assert hardware.status()["doors"][3] == "unlocked"
    assert levels.requests[-2][:6] == hx("01050002FF00")  # write coil 3 on
    assert levels.requests[-1][:6] == hx("010100020001")  # read it back
    await hardware.lock_door(3)
    assert levels.coils[2] is False and hardware.door_locked(3)
    assert hardware.status()["doors"][3] == "locked"


async def test_channel_mapping_is_used(real_config, fakes):
    real_config.hardware.door_locks.channels = list(range(11, 21))
    hardware = RealHardware(real_config)
    await hardware.start()
    try:
        await hardware.unlock_door(1)
        assert fakes[1].coils[10] is True and fakes[1].coils[0] is False
        with pytest.raises(ValueError, match="no door lock channel for level 11"):
            await hardware.unlock_door(11)
    finally:
        await hardware.stop()


async def test_read_back_mismatch_is_a_hardware_error(hardware, fakes):
    core, levels = fakes
    levels.ignore_coil_writes = True
    with pytest.raises(HardwareError, match=r"channel 3 \(level 3\) reads off after writing on"):
        await hardware.unlock_door(3)
    assert hardware.status()["doors"][3] == "unknown" and not hardware.door_locked(3)
    assert hardware.levels.connected  # a mismatch is not a transport failure
    assert hardware.events.empty()


async def test_lock_all_doors_checks_every_coil(hardware, fakes):
    core, levels = fakes
    levels.coils[4] = True
    levels.ignore_coil_writes = True
    with pytest.raises(HardwareError, match=r"channels \[5\] still on"):
        await hardware.lock_all_doors()
    assert hardware.status()["doors"][1] == "unknown"
    levels.ignore_coil_writes = False
    await hardware.lock_all_doors()
    assert levels.coils == [False] * 30
    assert all(hardware.door_locked(n) for n in range(1, 11))


async def test_door_poll_emits_debounced_events(hardware, fakes):
    core, levels = fakes
    core.inputs[0] = False  # low = open
    event = await asyncio.wait_for(hardware.events.get(), 1.0)
    assert event is DoorEvent.OPENED and hardware.status()["door_open"] is True
    core.inputs[0] = True
    event = await asyncio.wait_for(hardware.events.get(), 1.0)
    assert event is DoorEvent.CLOSED and hardware.status()["door_open"] is False


async def test_door_poll_filters_a_single_glitch(real_config, fakes):
    real_config.hardware.door_sensor.debounce_count = 5
    hardware = RealHardware(real_config)
    await hardware.start()
    try:
        fakes[0].inputs[0] = False
        await asyncio.sleep(0.015)  # one or two polls
        fakes[0].inputs[0] = True
        await asyncio.sleep(0.1)
        assert hardware.events.empty() and hardware.status()["door_open"] is False
    finally:
        await hardware.stop()


async def test_door_polarity_high(real_config, fakes):
    real_config.hardware.door_sensor.di_active = "high"
    fakes[0].inputs[0] = False
    hardware = RealHardware(real_config)
    await hardware.start()
    try:
        assert hardware.status()["door_open"] is False
        fakes[0].inputs[0] = True
        assert await asyncio.wait_for(hardware.events.get(), 1.0) is DoorEvent.OPENED
    finally:
        await hardware.stop()


async def test_start_with_levels_module_down(real_config, fakes):
    core, levels = fakes
    port = levels.port
    await levels.stop()
    hardware = RealHardware(real_config)
    await hardware.start()
    try:
        assert hardware.core.connected and not hardware.levels.connected
        assert not hardware.healthy()
        status = hardware.status()
        assert status["relay_levels"]["last_error"].startswith("connect failed")
        assert status["doors"][1] == "unknown" and status["door_open"] is False
        with pytest.raises(HardwareError, match="not connected"):
            await hardware.unlock_door(1)
        await levels.start(port=port)
        await wait_until(hardware.healthy, "reconnect")
        await hardware.lock_all_doors()
        assert hardware.door_locked(1)
    finally:
        await hardware.stop()


async def test_lost_levels_connection_queues_one_fault(hardware, fakes):
    core, levels = fakes
    await hardware.unlock_door(2)
    await levels.drop_connections()
    fault = await asyncio.wait_for(hardware.events.get(), 1.0)
    assert isinstance(fault, HardwareFault) and fault.message.startswith("relay_levels:")
    assert hardware.status()["doors"][2] == "unknown" and not hardware.door_locked(2)
    await wait_until(lambda: hardware.levels.connected, "reconnect")
    assert hardware.status()["doors"][2] == "unknown"  # until something is read back
    assert hardware.events.empty()


async def test_lost_core_connection_marks_door_and_poll_unknown(hardware, fakes):
    core, levels = fakes
    await core.drop_connections()
    fault = await asyncio.wait_for(hardware.events.get(), 1.0)
    assert isinstance(fault, HardwareFault) and fault.message.startswith("relay_core:")
    assert not hardware.healthy() and hardware.status()["door_open"] is None
    await wait_until(hardware.healthy, "recovery")
    assert hardware.status()["door_open"] is False
    assert hardware.events.empty()


async def test_motor_primitives_with_read_back(hardware, fakes):
    core, levels = fakes
    await hardware.set_spindle(True)
    await hardware.set_motor(True)
    assert core.coils[:2] == [True, True]
    assert hardware.status()["motor"] == {"running": True, "spindle_open": True}
    core.ignore_coil_writes = True
    with pytest.raises(HardwareError, match=r"channel 1 \(motor\) reads on after writing off"):
        await hardware.set_motor(False)
    assert hardware.status()["motor"]["running"] is None


async def test_alarm_only_logs(hardware, fakes, caplog):
    before = len(fakes[0].requests) + len(fakes[1].requests)
    with caplog.at_level(logging.INFO, logger="monitoni.hardware.real"):
        await hardware.alarm(True)
    assert "alarm on" in caplog.text
    assert len(fakes[0].requests) + len(fakes[1].requests) == before
