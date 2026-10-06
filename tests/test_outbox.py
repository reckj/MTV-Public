"""The outbox: durable complete/close reports, delivered oldest first with backoff."""

import asyncio

import pytest

from monitoni.eventlog import EventLog
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer
from tests.helpers import wait_until

BACKOFF = (0.05, 0.1, 0.2)


class CountingPurchase(MockPurchaseServer):
    """Reports fail the first `failures` times (or while `blocked`); call times are recorded."""

    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures
        self.blocked = False
        self.calls: list[float] = []

    def status(self) -> dict:
        return {"reachable": not self.blocked, "last_ok": None, "last_error": "refused"}

    async def _attempt(self, report) -> bool:
        self.calls.append(asyncio.get_running_loop().time())
        if self.blocked or self.failures > 0:
            self.failures = max(0, self.failures - 1)
            return False
        return await report()

    async def complete(self) -> bool:
        return await self._attempt(super().complete)

    async def close(self) -> bool:
        return await self._attempt(super().close)


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


async def rows(outbox: Outbox) -> list[dict]:
    return [r["details"] for r in reversed(await outbox.events.recent(50)) if r["kind"] == "outbox"]


async def test_complete_then_close_delivered_in_order(make_outbox):
    outbox = await make_outbox()
    await outbox.enqueue("complete", 3)
    await outbox.enqueue("close", 3)
    await wait_until(lambda: outbox.purchase.reports == ["complete", "close"], "delivery")
    await wait_until(lambda: outbox.pending_count == 0, "rows deleted")
    assert await outbox.pending() == 0
    await asyncio.sleep(0.02)
    all_rows = await rows(outbox)
    assert [r for r in all_rows if not r["delivered"]] == [
        {"kind": "complete", "delivered": False, "attempts": 0},
        {"kind": "close", "delivered": False, "attempts": 0}]
    assert [r for r in all_rows if r["delivered"]] == [
        {"kind": "complete", "delivered": True, "attempts": 1},
        {"kind": "close", "delivered": True, "attempts": 1}]


async def test_unknown_kind_is_refused(make_outbox):
    outbox = await make_outbox()
    with pytest.raises(ValueError, match="unknown report kind"):
        await outbox.enqueue("refund", 3)


async def test_survives_a_restart(make_outbox):
    blocked = CountingPurchase()
    blocked.blocked = True
    outbox = await make_outbox(blocked)
    await outbox.enqueue("complete", 5)
    await wait_until(lambda: len(blocked.calls) >= 1, "first failed attempt")
    await outbox.stop()
    assert blocked.reports == []

    working = CountingPurchase()
    outbox2 = await make_outbox(working)  # same database file
    assert outbox2.pending_count == 1
    await wait_until(lambda: working.reports == ["complete"], "delivered after the restart")
    delivered = [r for r in await rows(outbox2) if r["delivered"]]
    assert delivered == [{"kind": "complete", "delivered": True, "attempts": 2}]


async def test_backoff_sequence_then_delivery_with_only_queued_and_delivered_rows(make_outbox):
    purchase = CountingPurchase(failures=3)
    outbox = await make_outbox(purchase)
    await outbox.enqueue("close", 1)
    await wait_until(lambda: purchase.reports, "delivery after three failures", timeout=3)
    assert len(purchase.calls) == 4
    gaps = [b - a for a, b in zip(purchase.calls, purchase.calls[1:], strict=False)]
    for gap, expected in zip(gaps, BACKOFF, strict=True):
        assert expected <= gap < expected + 0.08, (gaps, BACKOFF)
    await asyncio.sleep(0.02)
    assert await rows(outbox) == [
        {"kind": "close", "delivered": False, "attempts": 0},
        {"kind": "close", "delivered": True, "attempts": 4}]  # no row per failed attempt


async def test_a_new_row_wakes_the_sender_at_once(make_outbox):
    outbox = await make_outbox()
    await asyncio.sleep(0.05)  # the sender is idle, waiting for work
    t0 = asyncio.get_running_loop().time()
    await outbox.enqueue("complete", 2)
    await wait_until(lambda: outbox.purchase.reports, "delivery")
    assert asyncio.get_running_loop().time() - t0 < 0.05


async def test_the_sender_survives_a_failing_event_write(make_outbox, monkeypatch):
    outbox = await make_outbox(backoff=(0.05,))
    original = outbox.events.write
    calls = {"n": 0}

    async def broken_write(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the "delivered" row of the first report
            raise RuntimeError("disk full")
        await original(*args, **kwargs)

    monkeypatch.setattr(outbox.events, "write", broken_write)
    await outbox.enqueue("complete", 3)
    await wait_until(lambda: outbox.purchase.reports == ["complete"], "first delivery")
    await outbox.enqueue("close", 3)
    await wait_until(lambda: outbox.purchase.reports == ["complete", "close"],
                     "the sender is still alive", timeout=3)
    await wait_until(lambda: outbox.pending_count == 0, "row deleted")
