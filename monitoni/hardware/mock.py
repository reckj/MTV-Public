"""Mock hardware for development on a laptop. Logs every call, does no I/O."""

import asyncio
import logging

from monitoni.hardware.base import DoorEvent

log = logging.getLogger(__name__)


class MockHardware:
    def __init__(self, levels: int) -> None:
        self.levels = levels
        self.events: asyncio.Queue = asyncio.Queue()
        self.calls: list[str] = []  # every call, oldest first; tests read this
        self._locked = {level: True for level in range(1, levels + 1)}
        self._alarm = False
        self._running = False

    async def start(self) -> None:
        self._record("start")
        self._running = True

    async def stop(self) -> None:
        self._record("stop")
        self._running = False

    def status(self) -> dict:
        return {
            "mode": "mock",
            "running": self._running,
            "alarm": self._alarm,
            "doors": {level: "locked" if locked else "unlocked"
                      for level, locked in self._locked.items()},
        }

    async def lock_door(self, level: int) -> None:
        self._record(f"lock_door({level})")
        self._locked[level] = True

    async def unlock_door(self, level: int) -> None:
        self._record(f"unlock_door({level})")
        self._locked[level] = False

    async def lock_all_doors(self) -> None:
        self._record("lock_all_doors")
        for level in self._locked:
            self._locked[level] = True

    async def alarm(self, on: bool) -> None:
        self._record(f"alarm({on})")
        self._alarm = on

    def door_locked(self, level: int) -> bool:
        return self._locked.get(level, True)

    def simulate_door(self, open: bool) -> None:
        """Dev-only: pretend the door sensor changed."""
        event = DoorEvent.OPENED if open else DoorEvent.CLOSED
        self._record(f"simulate_door({event.value})")
        self.events.put_nowait(event)

    def _record(self, call: str) -> None:
        log.info("mock hardware: %s", call)
        self.calls.append(call)
