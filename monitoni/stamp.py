"""When did a component flag flip? Local-time stamps for the status object."""

from datetime import datetime


def local_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Flag:
    """A boolean with the local time it last flipped. `since` stays None while the flag has
    been true since the start; the first false and every later change set it."""

    def __init__(self) -> None:
        self.value: bool | None = None  # None: not determined yet
        self.since: str | None = None

    def set(self, value: bool) -> bool:
        """Record the value; True if it flipped (or was first determined as false)."""
        if self.value is None and value:
            self.value = True
            return False
        if value != self.value:
            self.value = value
            self.since = local_time()
            return True
        return False
