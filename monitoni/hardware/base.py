"""The one interface every hardware implementation provides."""

from typing import Protocol


class Hardware(Protocol):
    """Owns all physical I/O. The daemon talks to hardware only through this."""

    async def start(self) -> None:
        """Connect to devices. Raise if a required device is unreachable."""
        ...

    async def stop(self) -> None:
        """Leave devices in a safe state and release connections. Must not raise."""
        ...

    def status(self) -> dict:
        """Current state of every component, JSON-serialisable."""
        ...
