"""The outbox: durable completions, delivered oldest first with backoff, surviving restarts."""

import asyncio

import pytest

from monitoni.eventlog import EventLog
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer
from tests.helpers import wait_until

BACKOFF = (0.05, 0.1, 0.2)


class CountingPurchase(MockPurchaseServer):
    """`complete` fails the first `failures` times (or while `blocked`); records call times."""

    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures
        self.blocked = False
        self.calls: list[float] = []

    def status(self) -> dict:
        return {"reachable": not self.blocked, "last_ok": None, "last_error": "refused"}

    async def complete(self, purchase_id, level, success) -> bool:
        self.calls.append(asyncio.get_running_loop().time())
        if self.blocked or self.failures > 0:
            self.failures = max(0, self.failures - 1)
            return False
        return await super().complete(purchase_id, level, success)


@pytest.fixture
async def make_outbox(tmp_path):
    started = []

    async def _make(purchase=None, backoff=BACKOFF) -> Outbox:
        events = EventLog(tmp_path / "events.db")
        await events.start()
        outbox = Outbox(tmp_path / "events.db", purchase or CountingPurchase(), events, backoff,
                        lambda: "idle")
        await outbox.start()
        started.append((outbox, events))
        return outbox

    yield _make
    for outbox, events in started:
        await outbox.stop()
        await events.stop()


async def completion_rows(outbox: Outbox) -> list[dict]:
    return [r["details"] for r in reversed(await outbox.events.recent(50))
            if r["kind"] == "purchase_complete"]


async def test_delivered_on_the_first_try(make_outbox):
    outbox = await make_outbox()
    await outbox.enqueue("p-1", 3, True)
    await wait_until(lambda: outbox.purchase.completions, "delivery")
    assert outbox.purchase.completions == [{"purchase_id": "p-1", "level": 3, "success": True}]
    await wait_until(lambda: outbox.pending_count == 0, "row deleted")
    assert await outbox.pending() == 0
    await asyncio.sleep(0.02)
    assert await completion_rows(outbox) == [
        {"delivered": False, "attempts": 0, "success": True},
        {"delivered": True, "attempts": 1, "success": True}]


async def test_reason_travels_with_the_rows(make_outbox):
    outbox = await make_outbox()
    await outbox.enqueue("p-2", 4, False, reason="unlock_timeout")
    await wait_until(lambda: outbox.pending_count == 0, "delivery")
    await asyncio.sleep(0.02)
    rows = await completion_rows(outbox)
    assert rows[0] == {"delivered": False, "attempts": 0, "success": False,
                       "reason": "unlock_timeout"}
    assert rows[1] == {"delivered": True, "attempts": 1, "success": False,
                       "reason": "unlock_timeout"}


async def test_survives_a_restart(make_outbox, tmp_path):
    blocked = CountingPurchase()
    blocked.blocked = True
    outbox = await make_outbox(blocked)
    await outbox.enqueue("p-3", 5, True)
    await wait_until(lambda: len(blocked.calls) >= 1, "first failed attempt")
    await outbox.stop()
    assert blocked.completions == []

    working = CountingPurchase()
    outbox2 = await make_outbox(working)  # same database file
    assert outbox2.pending_count == 1
    await wait_until(lambda: working.completions, "delivered after the restart")
    assert working.completions == [{"purchase_id": "p-3", "level": 5, "success": True}]
    rows = await completion_rows(outbox2)
    assert rows[-1]["delivered"] is True and rows[-1]["attempts"] >= 2
    assert any(r == {"delivered": False, "attempts": 1, "success": True, "error": "refused"}
               for r in rows)


async def test_backoff_sequence_then_delivery(make_outbox):
    purchase = CountingPurchase(failures=3)
    outbox = await make_outbox(purchase)
    await outbox.enqueue("p-4", 1, True)
    await wait_until(lambda: purchase.completions, "delivery after three failures", timeout=3)
    assert len(purchase.calls) == 4
    gaps = [b - a for a, b in zip(purchase.calls, purchase.calls[1:], strict=False)]
    for gap, expected in zip(gaps, BACKOFF, strict=True):
        assert expected <= gap < expected + 0.08, (gaps, BACKOFF)


async def test_a_new_row_wakes_the_sender_at_once(make_outbox):
    outbox = await make_outbox()
    await asyncio.sleep(0.05)  # the sender is idle, waiting for work
    t0 = asyncio.get_running_loop().time()
    await outbox.enqueue("p-5", 2, True)
    await wait_until(lambda: outbox.purchase.completions, "delivery")
    assert asyncio.get_running_loop().time() - t0 < 0.05


async def test_oldest_first(make_outbox):
    purchase = CountingPurchase()
    purchase.blocked = True
    outbox = await make_outbox(purchase)
    await outbox.enqueue("p-a", 1, True)
    await outbox.enqueue("p-b", 2, False, reason="unlock_timeout")
    await outbox.enqueue("p-c", 3, True)
    await asyncio.sleep(0.02)
    assert outbox.pending_count == 3 and purchase.completions == []
    purchase.blocked = False
    await wait_until(lambda: len(purchase.completions) == 3, "all delivered", timeout=3)
    assert [c["purchase_id"] for c in purchase.completions] == ["p-a", "p-b", "p-c"]
    assert outbox.pending_count == 0
