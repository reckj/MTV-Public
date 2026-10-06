import asyncio
import logging

import pytest

from monitoni.eventlog import EventLog
from monitoni.flow import (
    REASON_DATABASE,
    REASON_HARDWARE,
    REASON_MAINTENANCE,
    TRANSITIONS,
    DoorOpen,
    Event,
    Flow,
    IllegalTransition,
    State,
)
from monitoni.hardware.base import HardwareError
from monitoni.hardware.mock import MockHardware
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer, PurchaseServerError
from monitoni.runtime import Runtime
from tests.helpers import wait_until, wait_until_async

ALLOWED = sorted(TRANSITIONS.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value))
REJECTED = [
    (State.IDLE, Event.DOOR_CLOSED),
    (State.IDLE, Event.COMPLETE),
    (State.IDLE, Event.PURCHASE_VALID),
    (State.IDLE, Event.CANCEL),
    (State.SLEEP, Event.SELECT_LEVEL),
    (State.DOOR_UNLOCKED, Event.SELECT_LEVEL),
    (State.DOOR_UNLOCKED, Event.CANCEL),
    (State.DOOR_OPENED, Event.CANCEL),
    (State.DOOR_FORCED, Event.SELECT_LEVEL),
    (State.DOOR_FORCED, Event.DOOR_OPENED),
    (State.OUT_OF_ORDER, Event.SELECT_LEVEL),
    (State.OUT_OF_ORDER, Event.DOOR_OPENED),
    (State.OUT_OF_ORDER, Event.RESET),
    (State.SETTINGS, Event.SELECT_LEVEL),
    (State.SETTINGS, Event.CANCEL),
    (State.SETTINGS, Event.PURCHASE_VALID),
    (State.SETTINGS, Event.ENTER_SETTINGS),
    (State.SLEEP, Event.ENTER_SETTINGS),
    (State.DOOR_UNLOCKED, Event.ENTER_SETTINGS),
    (State.IDLE, Event.EXIT_SETTINGS),
]


@pytest.fixture
async def make_flow(make_config):
    started = []

    async def _make(maintenance: bool = False, hardware_cls=MockHardware, purchase=None,
                    **timings: float) -> Flow:
        config = make_config(**timings)
        events = EventLog(config.database.path)
        await events.start()
        hardware = hardware_cls(config.vending.levels)
        await hardware.start()
        purchase = purchase or MockPurchaseServer()
        holder: dict = {}
        outbox = Outbox(config.database.path, purchase, events,
                        config.purchase_server.outbox_backoff_s,
                        lambda: holder["flow"].state.value)
        await outbox.start()
        flow = holder["flow"] = Flow(config, hardware, purchase, outbox, events,
                                     runtime=Runtime(out_of_order=maintenance))
        await flow.start()
        started.append((flow, outbox, events))
        return flow

    yield _make
    for flow, outbox, events in started:
        await flow.stop()
        await outbox.stop()
        await events.stop()


async def wait_for(flow: Flow, state: State) -> None:
    """Wait until the flow reports `state`; by then its entry hook has completed."""
    await wait_until(lambda: flow.state is state, state.value)


async def force(flow: Flow, state: State) -> None:
    """Put the flow into a state without walking there (test only)."""
    flow.selected_level = 3
    if state is State.OUT_OF_ORDER:
        flow.reason = REASON_HARDWARE
    async with flow._lock:
        await flow._transition(state, Event.RESET)
        flow._cancel_tasks()  # the state stays where the test put it


def assert_all_locked(flow: Flow) -> None:
    hardware = flow.hardware
    assert all(hardware.door_locked(n) for n in range(1, hardware.levels + 1))
    assert hardware.calls[-1] == "lock_all_doors"


async def to_door_unlocked(flow: Flow, level: int = 3) -> None:
    await flow.dispatch(Event.SELECT_LEVEL, level=level)
    flow.purchase.simulate_payment(level)
    await wait_for(flow, State.DOOR_UNLOCKED)


async def outbox_rows(flow: Flow, count: int) -> list[dict]:
    """Wait for `count` outbox rows; return them oldest first."""
    async def fetch():
        rows = [r for r in reversed(await flow.events.recent(100)) if r["kind"] == "outbox"]
        return rows if len(rows) >= count else None
    return await wait_until_async(fetch, f"{count} outbox rows")


# -- transition table ---------------------------------------------------------

@pytest.mark.parametrize("state,event,expected", [(s, e, t) for (s, e), t in ALLOWED],
                         ids=[f"{s.value}-{e.value}" for (s, e), _ in ALLOWED])
async def test_allowed_transition(make_flow, state, event, expected):
    flow = await make_flow()
    await force(flow, state)
    await flow.dispatch(event, level=2, item=2)
    assert flow.state is expected


@pytest.mark.parametrize("state,event", REJECTED, ids=[f"{s.value}-{e.value}" for s, e in REJECTED])
async def test_rejected_transition(make_flow, state, event):
    flow = await make_flow()
    await force(flow, state)
    with pytest.raises(IllegalTransition):
        await flow.dispatch(event, level=2)
    assert flow.state is state


async def test_select_level_out_of_range(make_flow):
    flow = await make_flow()
    for level in (0, 11, None):
        with pytest.raises(ValueError, match="level must be 1..10"):
            await flow.dispatch(Event.SELECT_LEVEL, level=level)
    assert flow.state is State.IDLE


async def test_touch_outside_sleep_is_accepted_and_never_logged(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow)
    before = len(await flow.events.recent(100))
    await flow.dispatch(Event.TOUCH)
    assert flow.state is State.DOOR_UNLOCKED
    assert len(await flow.events.recent(100)) == before  # a keepalive, not a command


async def test_touch_in_idle_resets_sleep_timer(make_flow):
    flow = await make_flow(sleep_timeout_s=0.1)
    await asyncio.sleep(0.06)
    await flow.dispatch(Event.TOUCH)
    await asyncio.sleep(0.06)
    assert flow.state is State.IDLE  # would have slept at 0.1 without the touch
    assert not [r for r in await flow.events.recent(20) if r["kind"] == "command"]
    await wait_for(flow, State.SLEEP)


# -- timeouts -----------------------------------------------------------------

async def test_sleep_timeout(make_flow):
    flow = await make_flow(sleep_timeout_s=0.05)
    await wait_for(flow, State.SLEEP)
    assert flow.countdown_s is None


async def test_purchase_timeout_locks_and_returns_to_idle(make_flow):
    flow = await make_flow(purchase_timeout_s=0.05)
    await flow.dispatch(Event.SELECT_LEVEL, level=4)
    await wait_for(flow, State.IDLE)
    assert flow.selected_level is None and flow.purchase_id is None
    assert_all_locked(flow)


async def test_door_unlock_timeout_locks_again(make_flow):
    flow = await make_flow(door_unlock_timeout_s=0.05)
    await to_door_unlocked(flow)
    assert not flow.hardware.door_locked(3)
    await wait_for(flow, State.IDLE)
    assert_all_locked(flow)


async def test_door_alarm_timeout(make_flow):
    flow = await make_flow(door_alarm_delay_s=0.05)
    await to_door_unlocked(flow)
    await flow.dispatch(Event.DOOR_OPENED)
    await wait_for(flow, State.DOOR_ALARM)
    assert "alarm(True)" in flow.hardware.calls
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.hardware.calls[-1] == "alarm(False)"
    await wait_for(flow, State.IDLE)
    assert_all_locked(flow)


async def test_transition_cancels_pending_timeout(make_flow):
    flow = await make_flow(sleep_timeout_s=0.05)
    await flow.dispatch(Event.SELECT_LEVEL, level=1)
    await asyncio.sleep(0.15)
    assert flow.state is State.CHECKING_PURCHASE  # the sleep timer did not fire into it


async def test_countdown(make_flow):
    flow = await make_flow(sleep_timeout_s=5.0)
    assert 4.5 < flow.countdown_s <= 5.0
    assert 4.5 < flow.status()["countdown_s"] <= 5.0
    await force(flow, State.COMPLETING)
    assert flow.countdown_s is None


# -- door lock rule -------------------------------------------------------------

async def test_cancel_locks_all_doors(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.SELECT_LEVEL, level=2)
    await flow.dispatch(Event.CANCEL)
    assert flow.state is State.IDLE
    assert_all_locked(flow)


async def test_reset_from_unlocked_locks_all_doors(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow)
    await flow.dispatch(Event.RESET)
    assert flow.state is State.IDLE
    assert_all_locked(flow)


async def test_happy_path(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow, level=5)
    local_id = flow.purchase_id
    assert local_id is not None
    assert not flow.hardware.door_locked(5)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.state is State.COMPLETING
    await wait_for(flow, State.IDLE)
    assert_all_locked(flow)
    assert "unlock_door(5)" in flow.hardware.calls
    assert flow.hardware.calls.count("unlock_door(5)") == 1

    rows = await flow.events.recent(100)
    transitions = [r["details"]["to"] for r in reversed(rows) if r["kind"] == "transition"]
    assert transitions == ["checking_purchase", "door_unlocked", "door_opened",
                           "completing", "idle"]
    await wait_until(lambda: len(flow.purchase.reports) == 2, "delivery by the outbox")
    assert flow.purchase.reports == ["complete", "close"]
    rows = await outbox_rows(flow, 4)
    assert all(r["level"] == 5 for r in rows)
    assert [r["details"] for r in rows if not r["details"]["delivered"]] == [
        {"kind": "complete", "delivered": False, "attempts": 0},
        {"kind": "close", "delivered": False, "attempts": 0}]
    assert [r["details"] for r in rows if r["details"]["delivered"]] == [
        {"kind": "complete", "delivered": True, "attempts": 1},
        {"kind": "close", "delivered": True, "attempts": 1}]


async def test_out_of_order_at_startup(make_flow):
    flow = await make_flow(maintenance=True)
    assert flow.state is State.OUT_OF_ORDER
    assert_all_locked(flow)
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.SELECT_LEVEL, level=1)
    with pytest.raises(IllegalTransition):  # a door event stays rejected and logged here
        await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.TOUCH)
    assert flow.state is State.OUT_OF_ORDER


async def test_rejected_event_is_logged(make_flow):
    flow = await make_flow()
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.COMPLETE)
    rows = await flow.events.recent(1)
    assert rows[0]["kind"] == "rejected" and rows[0]["details"] == {"event": "complete"}


# -- forced door: opened without a purchase ----------------------------------------

FORCED_FROM = [State.IDLE, State.SLEEP, State.CHECKING_PURCHASE]


@pytest.mark.parametrize("state", FORCED_FROM, ids=[s.value for s in FORCED_FROM])
async def test_door_opened_without_a_purchase_raises_the_alarm_until_closed(make_flow, state):
    flow = await make_flow()
    await force(flow, state)
    await flow.dispatch(Event.DOOR_OPENED)
    assert flow.state is State.DOOR_FORCED and flow.countdown_s is None  # no timeout
    assert flow.hardware.calls[-1] == "alarm(True)" and flow.hardware.status()["alarm"]
    assert flow._poll_task is None  # purchase polling, if there was any, is gone
    await asyncio.sleep(0.05)
    assert flow.state is State.DOOR_FORCED
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.state is State.IDLE and flow.selected_level is None and flow.purchase_id is None
    assert flow.hardware.calls[-2:] == ["alarm(False)", "lock_all_doors"]
    assert_all_locked(flow)
    rows = await flow.events.recent(50)
    assert not [r for r in rows if r["kind"] == "outbox"]
    transitions = [r["details"]["to"] for r in reversed(rows) if r["kind"] == "transition"]
    assert transitions[-2:] == ["door_forced", "idle"]


# -- entry hooks complete before the state is visible -----------------------------

class BlockingHardware(MockHardware):
    """unlock_door waits for `release`, so a test can look at the flow mid-hook."""

    def __init__(self, levels: int) -> None:
        super().__init__(levels)
        self.release = asyncio.Event()

    async def unlock_door(self, level: int) -> None:
        await self.release.wait()
        await super().unlock_door(level)


async def test_state_changes_only_after_entry_hook_completed(make_flow):
    flow = await make_flow(hardware_cls=BlockingHardware)
    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    dispatching = asyncio.create_task(flow.dispatch(Event.PURCHASE_VALID, item=3))
    await asyncio.sleep(0.02)  # dispatch is now inside unlock_door, waiting for release
    assert flow.state is State.CHECKING_PURCHASE and flow.hardware.door_locked(3)
    flow.hardware.release.set()
    await dispatching
    assert flow.state is State.DOOR_UNLOCKED and not flow.hardware.door_locked(3)


# -- hardware fault and recovery ------------------------------------------------

class FailingUnlock(MockHardware):
    async def unlock_door(self, level: int) -> None:
        self._record(f"unlock_door({level}) failed")
        raise HardwareError("relay_levels: no response within 1.0s")


class BuggyUnlock(MockHardware):
    async def unlock_door(self, level: int) -> None:
        raise RuntimeError("oops")


class NoLock(MockHardware):
    async def lock_all_doors(self) -> None:
        self._record("lock_all_doors failed")
        raise HardwareError("relay_levels: not connected")


@pytest.mark.parametrize("state", [s for s in State if s is not State.OUT_OF_ORDER],
                         ids=[s.value for s in State if s is not State.OUT_OF_ORDER])
async def test_hardware_fault_from_every_state(make_flow, state):
    flow = await make_flow()
    await force(flow, state)
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_core: connection closed")
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    assert flow.selected_level is None and flow.purchase_id is None and flow.countdown_s is None
    assert_all_locked(flow)
    row = (await flow.events.recent(1))[0]
    assert row["kind"] == "transition" and row["details"]["event"] == "hardware_fault"
    assert row["details"]["error"] == "relay_core: connection closed"
    assert flow.status()["reason"] == "hardware"


async def test_hardware_ok_returns_to_idle(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.HARDWARE_FAULT, error="x")
    await flow.dispatch(Event.HARDWARE_OK)
    assert flow.state is State.IDLE and flow.reason is None and flow.countdown_s is not None
    assert_all_locked(flow)


async def test_hardware_ok_does_not_clear_maintenance(make_flow):
    flow = await make_flow(maintenance=True)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.HARDWARE_OK)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE


async def test_fault_while_out_of_order_keeps_the_reason(make_flow):
    flow = await make_flow(maintenance=True)
    rows_before = len(await flow.events.recent(100))
    await flow.dispatch(Event.HARDWARE_FAULT, error="x")  # accepted, no transition
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE
    assert len(await flow.events.recent(100)) == rows_before


async def test_entry_hook_hardware_error_goes_out_of_order(make_flow):
    flow = await make_flow(hardware_cls=FailingUnlock)
    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    await flow.dispatch(Event.PURCHASE_VALID, item=3)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    assert flow.countdown_s is None
    assert_all_locked(flow)
    details = (await flow.events.recent(1))[0]["details"]
    assert details["from"] == "checking_purchase" and details["to"] == "out_of_order"
    assert details["event"] == "hardware_fault" and details["attempted"] == "door_unlocked"
    assert "no response" in details["error"]


async def test_entry_hook_bug_goes_out_of_order_with_traceback(make_flow, caplog):
    flow = await make_flow(hardware_cls=BuggyUnlock)
    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    with caplog.at_level(logging.ERROR, logger="monitoni.flow"):
        await flow.dispatch(Event.PURCHASE_VALID, item=3)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    bug = [r for r in caplog.records if r.getMessage().startswith("bug: entering door_unlocked")]
    assert bug and bug[0].exc_info is not None


async def test_lock_failure_entering_out_of_order_is_logged_not_raised(make_flow, caplog):
    with caplog.at_level(logging.ERROR, logger="monitoni.flow"):
        flow = await make_flow(hardware_cls=NoLock)  # idle entry fails at start
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    assert "cannot lock doors while entering out_of_order" in caplog.text
    start_row = (await flow.events.recent(1))[0]
    assert start_row["kind"] == "daemon" and start_row["state"] == "out_of_order"
    assert start_row["details"] == {"event": "start", "reason": "hardware"}
    await flow.dispatch(Event.HARDWARE_OK)  # recovery attempt fails the same way, stays put
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    assert flow.hardware.calls.count("lock_all_doors failed") == 4  # idle, ooo, idle, ooo


# -- the purchase server: complete on open, relock, close on close, server item ----------

class FlakyPurchase(MockPurchaseServer):
    """`permission` raises PurchaseServerError the first `failures` times."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.errors = 0

    async def permission(self):
        if self.failures > 0:
            self.failures -= 1
            self.errors += 1
            raise PurchaseServerError("HTTP 500 from /api/vending/permission")
        return await super().permission()


class BuggyPurchase(MockPurchaseServer):
    """`permission` raises a plain exception the first `failures` times (a bug in the client)."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def permission(self):
        if self.failures > 0:
            self.failures -= 1
            raise KeyError("Item")
        return await super().permission()


class FailingLock(MockHardware):
    async def lock_door(self, level: int) -> None:
        self._record(f"lock_door({level}) failed")
        raise HardwareError("relay_levels: no response within 1.0s")


class FailingAlarmOff(MockHardware):
    async def alarm(self, on: bool) -> None:
        if not on:
            raise HardwareError("relay_core: not connected")
        await super().alarm(on)


async def test_complete_is_queued_on_entering_door_opened_before_anything_else(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow, level=3)
    await flow.dispatch(Event.DOOR_OPENED)
    assert flow.state is State.DOOR_OPENED
    assert "lock_door(3)" not in flow.hardware.calls  # the relock comes later
    row = (await outbox_rows(flow, 1))[0]
    assert row["details"] == {"kind": "complete", "delivered": False, "attempts": 0}
    assert row["level"] == 3 and row["state"] == "door_unlocked"  # queued while entering
    await wait_until(lambda: flow.purchase.reports == ["complete"], "delivery")


async def test_relock_after_the_delay_while_the_door_is_still_open(make_flow):
    flow = await make_flow()
    flow.config.vending.timings.relock_delay_s = 0.1
    await to_door_unlocked(flow, level=3)
    t0 = asyncio.get_running_loop().time()
    await flow.dispatch(Event.DOOR_OPENED)
    await asyncio.sleep(0.05)
    assert not flow.hardware.door_locked(3)  # not yet
    await wait_until(lambda: flow.hardware.door_locked(3), "relock")
    elapsed = asyncio.get_running_loop().time() - t0
    assert 0.1 <= elapsed < 0.3
    assert flow.hardware.calls[-1] == "lock_door(3)" and flow.state is State.DOOR_OPENED
    rows = await flow.events.recent(5)
    assert rows[0]["kind"] == "hardware" and rows[0]["details"] == {"event": "relocked"}


async def test_relock_failure_is_a_hardware_fault_with_complete_already_queued(make_flow):
    flow = await make_flow(hardware_cls=FailingLock)
    await to_door_unlocked(flow, level=3)
    await flow.dispatch(Event.DOOR_OPENED)
    await wait_for(flow, State.OUT_OF_ORDER)
    assert flow.reason == "hardware"
    await wait_until(lambda: flow.purchase.reports == ["complete"], "complete still delivered")
    row = [r for r in await flow.events.recent(20) if r["kind"] == "transition"][0]
    assert row["details"]["event"] == "hardware_fault"
    assert "relock of level 3" in row["details"]["error"]


async def test_door_closed_within_the_relock_delay_cancels_it_and_idle_locks_all(make_flow):
    flow = await make_flow()
    flow.config.vending.timings.relock_delay_s = 0.2
    await to_door_unlocked(flow, level=3)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.IDLE)
    await asyncio.sleep(0.25)
    assert "lock_door(3)" not in flow.hardware.calls
    assert_all_locked(flow)


@pytest.mark.parametrize("via_alarm", [False, True], ids=["from-door_opened", "from-door_alarm"])
async def test_close_is_queued_when_the_door_closes(make_flow, via_alarm):
    flow = await make_flow(door_alarm_delay_s=0.05)
    await to_door_unlocked(flow, level=4)
    await flow.dispatch(Event.DOOR_OPENED)
    if via_alarm:
        await wait_for(flow, State.DOOR_ALARM)
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.IDLE)
    await wait_until(lambda: len(flow.purchase.reports) == 2, "delivery")
    assert flow.purchase.reports == ["complete", "close"]
    queued = [r for r in await outbox_rows(flow, 4) if not r["details"]["delivered"]]
    assert [r["details"]["kind"] for r in queued] == ["complete", "close"]
    assert queued[1]["state"] == ("door_alarm" if via_alarm else "door_opened")


async def test_close_is_queued_even_when_alarm_off_fails(make_flow):
    flow = await make_flow(hardware_cls=FailingAlarmOff, door_alarm_delay_s=0.05)
    await to_door_unlocked(flow, level=4)
    await flow.dispatch(Event.DOOR_OPENED)
    await wait_for(flow, State.DOOR_ALARM)
    await flow.dispatch(Event.DOOR_CLOSED)  # leaving door_alarm raises -> out_of_order
    assert flow.state is State.OUT_OF_ORDER
    await wait_until(lambda: len(flow.purchase.reports) == 2, "delivery")
    assert flow.purchase.reports == ["complete", "close"]


async def test_server_item_overrides_the_selected_level(make_flow, caplog):
    flow = await make_flow()
    flow.purchase.simulate_payment(2, item=5)
    with caplog.at_level(logging.WARNING, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=2)
        await wait_for(flow, State.DOOR_UNLOCKED)
    assert flow.selected_level == 5 and "unlock_door(5)" in flow.hardware.calls
    assert flow.hardware.door_locked(2) and not flow.hardware.door_locked(5)
    check = [r for r in await flow.events.recent(20) if r["kind"] == "purchase_check"][0]
    assert check["level"] == 5
    assert check["details"] == {"permitted": True, "item": 5, "selected": 2}
    assert any("differs from the selected level 2" in r.getMessage() for r in caplog.records)


async def test_server_item_out_of_range_keeps_polling(make_flow, caplog):
    flow = await make_flow()
    flow.purchase.simulate_payment(2, item=11)
    with caplog.at_level(logging.WARNING, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=2)
        await asyncio.sleep(0.05)
        assert flow.state is State.CHECKING_PURCHASE
        flow.purchase.simulate_payment(2)
        await wait_for(flow, State.DOOR_UNLOCKED)
    assert flow.selected_level == 2
    assert len([r for r in caplog.records if "outside 1..10" in r.getMessage()]) == 1


async def test_unlock_timeout_sends_nothing(make_flow):
    flow = await make_flow(door_unlock_timeout_s=0.05)
    await to_door_unlocked(flow, level=4)
    await wait_for(flow, State.IDLE)
    await asyncio.sleep(0.05)
    assert flow.purchase.reports == [] and flow.outbox.pending_count == 0
    assert not [r for r in await flow.events.recent(50) if r["kind"] == "outbox"]
    assert_all_locked(flow)


async def test_purchase_server_errors_keep_the_poll_going(make_flow, caplog):
    flow = await make_flow(purchase=FlakyPurchase(3))
    flow.purchase.simulate_payment(2)
    with caplog.at_level(logging.WARNING, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=2)
        await wait_for(flow, State.DOOR_UNLOCKED)
    assert flow.purchase.errors == 3
    warnings = [r for r in caplog.records if "permission check failed" in r.getMessage()]
    assert len(warnings) == 1  # once per checking_purchase, not per poll


async def test_a_bug_in_the_purchase_client_keeps_the_poll_going(make_flow, caplog):
    flow = await make_flow(purchase=BuggyPurchase(2))
    flow.purchase.simulate_payment(2)
    with caplog.at_level(logging.ERROR, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=2)
        await wait_for(flow, State.DOOR_UNLOCKED)
    bugs = [r for r in caplog.records if "bug in the purchase client" in r.getMessage()]
    assert len(bugs) == 1 and bugs[0].exc_info is not None


# -- a report the outbox cannot store ---------------------------------------------------

async def test_thank_you_stays_thank_you_s_then_the_vend_is_done(make_flow):
    flow = await make_flow(thank_you_s=0.3)
    await to_door_unlocked(flow, level=2)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    started = asyncio.get_running_loop().time()
    assert flow.state is State.COMPLETING and flow.status()["countdown_s"] is None
    await wait_until(lambda: flow.purchase.reports == ["complete", "close"], "reports")
    await asyncio.sleep(0.15)
    assert flow.state is State.COMPLETING  # the reports are in; "Thank you" is still up
    await wait_for(flow, State.IDLE)
    assert asyncio.get_running_loop().time() - started >= 0.28
    assert_all_locked(flow)


async def test_a_fault_while_thank_you_is_up_cancels_the_wait(make_flow):
    flow = await make_flow(thank_you_s=0.2)
    await to_door_unlocked(flow, level=2)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_core: gone")
    assert flow.state is State.OUT_OF_ORDER
    await asyncio.sleep(0.3)
    assert flow.state is State.OUT_OF_ORDER
    # no stray COMPLETE: the wait was cancelled with the state
    assert not [r for r in await flow.events.recent(50) if r["kind"] == "rejected"]


async def test_a_lost_report_ends_the_vend_out_of_order(make_flow, monkeypatch, caplog):
    flow = await make_flow(thank_you_s=10.0)  # the fault does not wait for "Thank you"

    async def refuse(kind, level):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(flow.outbox, "enqueue", refuse)
    await to_door_unlocked(flow, level=3)
    with caplog.at_level(logging.ERROR, logger="monitoni.flow"):
        await flow.dispatch(Event.DOOR_OPENED)
    assert flow.state is State.DOOR_OPENED  # the customer still gets the product
    assert "outbox refused the complete report for level 3" in caplog.text
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.OUT_OF_ORDER)
    assert flow.reason == REASON_DATABASE and flow.status()["reason"] == "database"
    assert_all_locked(flow)
    rows = await flow.events.recent(50)
    lost = [r["details"] for r in reversed(rows)
            if r["kind"] == "hardware" and r["details"]["event"] == "outbox_failed"]
    assert [(d["kind"], d["error"]) for d in lost] == [("complete", "database is locked"),
                                                       ("close", "database is locked")]
    last = [r for r in rows if r["kind"] == "transition"][0]["details"]
    assert (last["from"], last["to"], last["event"]) == ("completing", "out_of_order",
                                                          "database_fault")
    with pytest.raises(IllegalTransition):  # recovery is for hardware faults only
        await flow.dispatch(Event.HARDWARE_OK)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_DATABASE
    assert flow.purchase.reports == []


async def test_a_lost_report_does_not_outlive_out_of_order(make_flow, monkeypatch):
    flow = await make_flow()
    real_enqueue = flow.outbox.enqueue

    async def refuse(kind, level):
        raise RuntimeError("disk full")

    monkeypatch.setattr(flow.outbox, "enqueue", refuse)
    await to_door_unlocked(flow, level=2)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_levels: gone")  # before the close
    assert flow.reason == REASON_HARDWARE
    monkeypatch.setattr(flow.outbox, "enqueue", real_enqueue)
    await flow.dispatch(Event.HARDWARE_OK)  # the hardware recovered; the lost report is history
    await to_door_unlocked(flow, level=2)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.IDLE)
    await wait_until(lambda: flow.purchase.reports == ["complete", "close"], "the next vend")


# -- the settings area ----------------------------------------------------------------

async def test_enter_from_idle_and_exit_to_idle(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.ENTER_SETTINGS)
    assert flow.state is State.SETTINGS and flow.reason is None
    assert flow.countdown_s is not None  # the inactivity timer, not a sleep timer
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.SELECT_LEVEL, level=3)
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.IDLE
    assert_all_locked(flow)
    transitions = [r["details"] for r in reversed(await flow.events.recent(10))
                   if r["kind"] == "transition"]
    assert [(t["event"], t["to"]) for t in transitions[-2:]] == [
        ("enter_settings", "settings"), ("exit_settings", "idle")]


async def test_enter_from_out_of_order_keeps_the_reason_until_the_exit_decides(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_core: gone")
    await flow.dispatch(Event.ENTER_SETTINGS)
    assert flow.state is State.SETTINGS and flow.reason == REASON_HARDWARE
    flow.hardware.is_healthy = False
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.hardware.is_healthy = True
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.IDLE and flow.reason is None


async def test_exit_follows_the_runtime_switch_first(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.runtime.out_of_order = True
    flow.hardware.is_healthy = False  # the switch wins over the hardware
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE
    assert_all_locked(flow)
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.HARDWARE_OK)  # maintenance never clears itself
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.runtime.out_of_order = False
    flow.hardware.is_healthy = True
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.IDLE


async def test_a_settings_visit_clears_the_database_reason(make_flow, monkeypatch):
    flow = await make_flow()

    async def refuse(kind, level):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(flow.outbox, "enqueue", refuse)
    await to_door_unlocked(flow, level=3)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.OUT_OF_ORDER)
    assert flow.reason == REASON_DATABASE
    await flow.dispatch(Event.ENTER_SETTINGS)
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.IDLE and flow.reason is None


async def test_settings_time_out_and_a_touch_restarts_the_timer(make_flow):
    flow = await make_flow(settings_timeout_s=0.1)
    await flow.dispatch(Event.ENTER_SETTINGS)
    await asyncio.sleep(0.06)
    await flow.dispatch(Event.TOUCH)
    await asyncio.sleep(0.06)
    assert flow.state is State.SETTINGS  # would have left at 0.1 without the touch
    await wait_for(flow, State.IDLE)
    rows = await flow.events.recent(10)
    assert [r["kind"] for r in rows if r["kind"] in ("timeout", "command")] == ["timeout"]
    # a touch writes no row anywhere (the page sends one per pointerdown)


async def test_door_events_in_settings_are_status_only(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.ENTER_SETTINGS)
    before = len(await flow.events.recent(100))
    flow.hardware.simulate_door(True)
    await flow.dispatch(Event.DOOR_OPENED)  # what the daemon's drainer does
    assert flow.state is State.SETTINGS
    assert "alarm(True)" not in flow.hardware.calls
    flow.hardware.simulate_door(False)
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.state is State.SETTINGS
    assert len(await flow.events.recent(100)) == before  # no transition, no rejected row


async def test_exit_is_refused_while_the_door_is_open(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.hardware.simulate_door(True)
    await flow.dispatch(Event.DOOR_OPENED)
    with pytest.raises(DoorOpen, match="close the door first"):
        await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.SETTINGS
    row = (await flow.events.recent(1))[0]
    assert row["kind"] == "rejected"
    assert row["details"] == {"event": "exit_settings", "reason": "door_open"}
    flow.hardware.simulate_door(False)
    await flow.dispatch(Event.DOOR_CLOSED)
    await flow.dispatch(Event.EXIT_SETTINGS)
    assert flow.state is State.IDLE


async def test_timeout_waits_for_the_door_to_close(make_flow):
    flow = await make_flow(settings_timeout_s=0.05)
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.hardware.simulate_door(True)
    await flow.dispatch(Event.DOOR_OPENED)
    await asyncio.sleep(0.15)
    assert flow.state is State.SETTINGS and flow.countdown_s is None  # fired, waiting
    flow.hardware.simulate_door(False)
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.state is State.IDLE
    assert_all_locked(flow)
    last = [r for r in await flow.events.recent(10) if r["kind"] == "transition"][0]
    assert last["details"]["event"] == "timeout"


async def test_a_touch_after_the_timeout_fired_keeps_the_visit_going(make_flow):
    flow = await make_flow(settings_timeout_s=0.05)
    await flow.dispatch(Event.ENTER_SETTINGS)
    flow.hardware.simulate_door(True)
    await flow.dispatch(Event.DOOR_OPENED)
    await asyncio.sleep(0.1)
    await flow.dispatch(Event.TOUCH)  # the timer restarts, the pending exit is forgotten
    flow.hardware.simulate_door(False)
    await flow.dispatch(Event.DOOR_CLOSED)
    assert flow.state is State.SETTINGS and flow.countdown_s is not None
    await wait_for(flow, State.IDLE)


async def test_hardware_fault_in_settings_goes_out_of_order(make_flow):
    flow = await make_flow()
    await flow.dispatch(Event.ENTER_SETTINGS)
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_levels: connection closed")
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_HARDWARE
    assert_all_locked(flow)


async def test_runtime_switch_at_start(make_flow):
    flow = await make_flow(maintenance=True)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE
    assert flow.runtime.out_of_order is True
