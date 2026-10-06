"""The one interface every hardware implementation provides, plus the error and queue item types."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class HardwareError(Exception):
    """A hardware command failed or could not be confirmed by read-back. Callers never retry."""


class DoorEvent(StrEnum):
    """Put on Hardware.events by the implementation; the daemon forwards them to the flow."""

    OPENED = "door_opened"
    CLOSED = "door_closed"


@dataclass(frozen=True)
class HardwareFault:
    """Put on Hardware.events when a module is lost or a background read fails.

    The daemon turns it into the flow's `hardware_fault` event.
    """

    message: str


@dataclass(frozen=True)
class KnownState:
    """Put on Hardware.events after relay_core was (re)connected and motor off / spindle closed
    were written and read back: the motor module forgets whatever it thought it was doing."""

    module: str


class Hardware(Protocol):
    """Owns all physical I/O. The daemon talks to hardware only through this.

    Hardware never calls into the state machine. Door sensor changes and faults
    go on `events` (DoorEvent | HardwareFault); the daemon drains the queue.
    Commands raise HardwareError on failure and are never retried by anyone.
    """

    events: asyncio.Queue

    async def start(self) -> None:
        """Connect to devices. Real hardware logs unreachable modules and keeps reconnecting."""
        ...

    async def stop(self) -> None:
        """Release connections. Must not raise."""
        ...

    def status(self) -> dict:
        """Current state of every component, JSON-serialisable."""
        ...

    def healthy(self) -> bool:
        """True when every module is connected and the last door sensor read succeeded."""
        ...

    async def lock_door(self, level: int) -> None: ...

    async def unlock_door(self, level: int) -> None: ...

    async def lock_all_doors(self) -> None: ...

    async def alarm(self, on: bool) -> None: ...

    def door_locked(self, level: int) -> bool:
        """Lock state as last read back from the module; unknown counts as not locked."""
        ...

    async def set_motor(self, on: bool) -> None: ...

    async def set_spindle(self, on: bool) -> None: ...
