"""The one interface every hardware implementation provides."""

import asyncio
from enum import StrEnum
from typing import Protocol


class DoorEvent(StrEnum):
    """Put on Hardware.events by the implementation; the daemon forwards them to the flow."""

    OPENED = "door_opened"
    CLOSED = "door_closed"


class Hardware(Protocol):
    """Owns all physical I/O. The daemon talks to hardware only through this.

    Hardware never calls into the state machine. Door sensor changes go on
    `events`; the daemon drains the queue and dispatches them.
    """

    events: asyncio.Queue

    async def start(self) -> None:
        """Connect to devices. Raise if a required device is unreachable."""
        ...

    async def stop(self) -> None:
        """Leave devices in a safe state and release connections. Must not raise."""
        ...

    def status(self) -> dict:
        """Current state of every component, JSON-serialisable."""
        ...

    async def lock_door(self, level: int) -> None: ...

    async def unlock_door(self, level: int) -> None: ...

    async def lock_all_doors(self) -> None: ...

    async def alarm(self, on: bool) -> None: ...

    def door_locked(self, level: int) -> bool: ...
