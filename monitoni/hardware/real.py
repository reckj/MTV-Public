"""Real hardware: two Waveshare Modbus-TCP modules.

Door locks live on `relay_levels`; motor, spindle lock and the door sensor on `relay_core`.
Every coil write is read back and the cached state comes from the read-back only. Module
failures close the module's socket, queue a HardwareFault and are repaired by the reconnect
loops; nothing here retries a command.
"""

import asyncio
import contextlib
import logging

from monitoni.config import Config, RelayModuleConfig
from monitoni.hardware.base import DoorEvent, HardwareError, HardwareFault
from monitoni.hardware.modbus import ModbusTcpModule
from monitoni.stamp import Flag

log = logging.getLogger(__name__)


class RealHardware:
    def __init__(self, config: Config) -> None:
        self.config = config.hardware
        self.events: asyncio.Queue = asyncio.Queue()
        self.core = self._module("relay_core", self.config.relay_core)
        self.levels = self._module("relay_levels", self.config.relay_levels)
        self._channels = dict(enumerate(self.config.door_locks.channels, start=1))  # level -> ch
        self._door_locked: dict[int, bool | None] = dict.fromkeys(self._channels)  # None = unknown
        self._door_open: bool | None = None
        self._poll_ok = Flag()  # the last door-sensor read succeeded, with the flip time
        self._motor_on: bool | None = None
        self._spindle_on: bool | None = None
        self._tasks: list[asyncio.Task] = []

    def _module(self, name: str, cfg: RelayModuleConfig) -> ModbusTcpModule:
        return ModbusTcpModule(name, cfg.host, cfg.port, cfg.slave_address, cfg.timeout,
                               cfg.max_channels, on_lost=self._lost)

    def _lost(self, name: str, error: str) -> None:
        """A module's connection went away: its cached states are unknown now; tell the flow."""
        if name == "relay_levels":
            self._door_locked = dict.fromkeys(self._channels)
        else:
            self._motor_on = self._spindle_on = None
            self._door_open = None
            self._poll_ok.set(False)
        self.events.put_nowait(HardwareFault(f"{name}: {error}"))

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect what can be connected; unreachable modules are retried in the background."""
        for module in (self.core, self.levels):
            try:
                await module.connect()
            except HardwareError as exc:
                log.error("%s; retrying in the background", exc)
        if self.core.connected:
            with contextlib.suppress(HardwareError):  # the module queued the fault itself
                self._door_open = await self._read_door_input()
        if self.levels.connected:
            try:
                await self.lock_all_doors()
            except HardwareError as exc:
                log.error("%s", exc)
        self._tasks = [
            asyncio.create_task(self.core.reconnect_loop(), name="reconnect-relay_core"),
            asyncio.create_task(self.levels.reconnect_loop(), name="reconnect-relay_levels"),
            asyncio.create_task(self._door_poll(), name="door-poll"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2.0)  # shutdown must never hang
            for task in pending:
                log.error("%s did not stop within 2s", task.get_name())
        await self.core.close()
        await self.levels.close()

    # -- status --------------------------------------------------------------

    def healthy(self) -> bool:
        return self.core.connected and self.levels.connected and self._poll_ok.value is True

    def status(self) -> dict:
        return {
            "mode": "real",
            "relay_core": self.core.status(),
            "relay_levels": self.levels.status(),
            "door_open": self._door_open,
            "door_poll_ok": self._poll_ok.value is True,
            "door_poll_since": self._poll_ok.since,
            "doors": {level: self._door_word(level) for level in self._channels},
            "motor": {"running": self._motor_on, "spindle_open": self._spindle_on},
        }

    def _door_word(self, level: int) -> str:
        state = self._door_locked[level] if self.levels.connected else None
        return "unknown" if state is None else ("locked" if state else "unlocked")

    def door_locked(self, level: int) -> bool:
        return self.levels.connected and self._door_locked.get(level) is True

    # -- door locks (relay_levels) -------------------------------------------

    async def lock_door(self, level: int) -> None:
        await self._set_door(level, locked=True)

    async def unlock_door(self, level: int) -> None:
        await self._set_door(level, locked=False)

    async def _set_door(self, level: int, *, locked: bool) -> None:
        channel = self._channel(level)
        self._door_locked[level] = None
        await self.levels.write_coil(channel, not locked)
        (actual,) = await self.levels.read_coils(channel - 1, 1)
        if actual != (not locked):
            raise HardwareError(f"relay_levels channel {channel} (level {level}) reads "
                                f"{'on' if actual else 'off'} after writing "
                                f"{'off' if locked else 'on'}")
        self._door_locked[level] = locked

    def _channel(self, level: int) -> int:
        try:
            return self._channels[level]
        except KeyError:
            raise ValueError(f"no door lock channel for level {level}") from None

    async def lock_all_doors(self) -> None:
        self._door_locked = dict.fromkeys(self._channels)
        await self.levels.write_all_coils(False)
        coils = await self.levels.read_coils(0, self.levels.max_channels)
        still_on = [i + 1 for i, on in enumerate(coils) if on]
        if still_on:
            raise HardwareError(f"relay_levels channels {still_on} still on after lock_all_doors")
        self._door_locked = dict.fromkeys(self._channels, True)

    async def alarm(self, on: bool) -> None:
        log.info("alarm %s (LED and audio come in a later milestone)", "on" if on else "off")

    # -- motor and spindle lock (relay_core) ---------------------------------

    async def set_motor(self, on: bool) -> None:
        self._motor_on = None
        self._motor_on = await self._set_core(self.config.motor.motor_channel, on, "motor")

    async def set_spindle(self, on: bool) -> None:
        self._spindle_on = None
        self._spindle_on = await self._set_core(self.config.motor.spindle_channel, on,
                                                "spindle lock")

    async def _set_core(self, channel: int, on: bool, what: str) -> bool:
        await self.core.write_coil(channel, on)
        (actual,) = await self.core.read_coils(channel - 1, 1)
        if actual != on:
            raise HardwareError(f"relay_core channel {channel} ({what}) reads "
                                f"{'on' if actual else 'off'} after writing "
                                f"{'on' if on else 'off'}")
        return on

    # -- door sensor (relay_core DI) -----------------------------------------

    async def _read_door_input(self) -> bool:
        """One FC02 read of the door input, wiring polarity applied."""
        cfg = self.config.door_sensor
        (raw,) = await self.core.read_discrete_inputs(cfg.di_index, 1)
        self._poll_ok.set(True)
        return raw if cfg.di_active == "high" else not raw

    async def _door_poll(self) -> None:
        """Poll the door input; a change confirmed `debounce_count` times becomes a DoorEvent."""
        cfg = self.config.door_sensor
        pending: bool | None = None
        count = 0
        while True:
            await asyncio.sleep(cfg.poll_interval_ms / 1000)
            if not self.core.connected:
                self._poll_ok.set(False)
                continue
            try:
                is_open = await self._read_door_input()
            except HardwareError:
                continue  # the module closed its socket and queued the fault
            if self._door_open is None or is_open == self._door_open:
                if self._door_open is None:
                    log.info("door sensor reads %s", "open" if is_open else "closed")
                self._door_open = is_open
                pending, count = None, 0
                continue
            if is_open is pending:
                count += 1
            else:
                pending, count = is_open, 1
            if count >= cfg.debounce_count:
                self._door_open = is_open
                pending, count = None, 0
                log.info("door %s", "opened" if is_open else "closed")
                self.events.put_nowait(DoorEvent.OPENED if is_open else DoorEvent.CLOSED)
