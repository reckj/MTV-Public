"""Purchase server interface. Milestone 1 ships the mock only; the HTTP client comes later."""

import logging
import uuid
from typing import Protocol

log = logging.getLogger(__name__)


class PurchaseServer(Protocol):
    async def check(self, machine_id: str, level: int) -> str | None:
        """Return the purchase id if a valid, unredeemed purchase exists for this level."""
        ...

    async def complete(self, purchase_id: str, machine_id: str, level: int,
                       success: bool) -> bool:
        """Tell the server the purchase was handed out (or not). True if acknowledged."""
        ...


class MockPurchaseServer:
    """`check` returns None until `simulate_payment(level)` was called, then an id once."""

    def __init__(self) -> None:
        self._paid: set[int] = set()

    def simulate_payment(self, level: int) -> None:
        log.info("mock purchase server: payment simulated for level %d", level)
        self._paid.add(level)

    async def check(self, machine_id: str, level: int) -> str | None:
        if level not in self._paid:
            return None
        self._paid.discard(level)
        purchase_id = uuid.uuid4().hex
        log.info("mock purchase server: valid purchase %s for %s level %d",
                 purchase_id, machine_id, level)
        return purchase_id

    async def complete(self, purchase_id: str, machine_id: str, level: int,
                       success: bool) -> bool:
        log.info("mock purchase server: complete %s for %s level %d success=%s",
                 purchase_id, machine_id, level, success)
        return True
