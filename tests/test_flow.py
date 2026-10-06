import asyncio
import logging

import pytest

from monitoni.eventlog import EventLog
from monitoni.flow import (
    REASON_HARDWARE,
    REASON_MAINTENANCE,
    TRANSITIONS,
    VEND_STATES,
    Event,
    Flow,
    IllegalTransition,
    State,
)
from monitoni.hardware.base import HardwareError
from monitoni.hardware.mock import MockHardware
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer, PurchaseServerError
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
]


@pytest.fixture
async def make_flow(make_config):
    started = []

    async def _make(maintenance: bool = False, hardware_cls=MockHardware, purchase=None,
                    **timings: float) -> Flow:
        config = make_config(**timings)
        config.system.maintenance_mode = maintenance
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
        flow = holder["flow"] = Flow(config, hardware, purchase, outbox, events)
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


def assert_all_locked(flow: Flow) -> None:
    hardware = flow.hardware
    assert all(hardware.door_locked(n) for n in range(1, hardware.levels + 1))
    assert hardware.calls[-1] == "lock_all_doors"


async def to_door_unlocked(flow: Flow, level: int = 3) -> None:
    await flow.dispatch(Event.SELECT_LEVEL, level=level)
    flow.purchase.simulate_payment(level)
    await wait_for(flow, State.DOOR_UNLOCKED)


async def completion_rows(flow: Flow, count: int) -> list[dict]:
    """Wait for `count` purchase_complete rows; return them oldest first."""
    async def fetch():
        rows = [r for r in reversed(await flow.events.recent(100))
                if r["kind"] == "purchase_complete"]
        return rows if len(rows) >= count else None
    return await wait_until_async(fetch, f"{count} purchase_complete rows")


# -- transition table ---------------------------------------------------------

@pytest.mark.parametrize("state,event,expected", [(s, e, t) for (s, e), t in ALLOWED],
                         ids=[f"{s.value}-{e.value}" for (s, e), _ in ALLOWED])
async def test_allowed_transition(make_flow, state, event, expected):
    flow = await make_flow()
    await force(flow, state)
    await flow.dispatch(event, level=2)
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


async def test_touch_outside_sleep_is_accepted(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow)
    await flow.dispatch(Event.TOUCH)
    assert flow.state is State.DOOR_UNLOCKED


async def test_touch_in_idle_resets_sleep_timer(make_flow):
    flow = await make_flow(sleep_timeout_s=0.1)
    await asyncio.sleep(0.06)
    await flow.dispatch(Event.TOUCH)
    await asyncio.sleep(0.06)
    assert flow.state is State.IDLE  # would have slept at 0.1 without the touch
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
    server_id = flow.purchase_id
    assert server_id is not None
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
    await wait_until(lambda: flow.purchase.completions, "delivery by the outbox")
    assert flow.purchase.completions == [{"purchase_id": server_id, "level": 5, "success": True}]
    queued, delivered = await completion_rows(flow, 2)
    assert queued["purchase_id"] == server_id and queued["state"] == "completing"
    assert queued["details"] == {"delivered": False, "attempts": 0, "success": True}
    assert delivered["details"] == {"delivered": True, "attempts": 1, "success": True}


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
    assert not [r for r in rows if r["kind"] == "purchase_complete"]
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
    dispatching = asyncio.create_task(flow.dispatch(Event.PURCHASE_VALID, purchase_id="srv-1"))
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
    await flow.dispatch(Event.PURCHASE_VALID, purchase_id="srv-1")
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
        await flow.dispatch(Event.PURCHASE_VALID, purchase_id="srv-1")
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


# -- the purchase server: completions, abandoned vends, rejected and failing checks ----

class FlakyPurchase(MockPurchaseServer):
    """`check` raises PurchaseServerError the first `failures` times."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.errors = 0

    async def check(self, level):
        if self.failures > 0:
            self.failures -= 1
            self.errors += 1
            raise PurchaseServerError("HTTP 500 from /api/purchase/check")
        return await super().check(level)


async def test_unlock_timeout_reports_success_false(make_flow):
    flow = await make_flow(door_unlock_timeout_s=0.05)
    await to_door_unlocked(flow, level=4)
    purchase_id = flow.purchase_id
    await wait_for(flow, State.IDLE)
    await wait_until(lambda: flow.purchase.completions, "delivery")
    assert flow.purchase.completions == [{"purchase_id": purchase_id, "level": 4, "success": False}]
    queued = (await completion_rows(flow, 1))[0]
    assert queued["details"] == {"delivered": False, "attempts": 0, "success": False,
                                 "reason": "unlock_timeout"}
    assert queued["state"] == "door_unlocked"  # queued while leaving, before idle is visible


@pytest.mark.parametrize("state", sorted(VEND_STATES, key=lambda s: s.value),
                         ids=[s.value for s in sorted(VEND_STATES, key=lambda s: s.value)])
async def test_hardware_fault_during_a_vend_reports_success_false(make_flow, state):
    flow = await make_flow()
    await force(flow, state)
    flow.purchase_id = "srv-9"
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_levels: gone")
    assert flow.state is State.OUT_OF_ORDER
    await wait_until(lambda: flow.purchase.completions, "delivery")
    assert flow.purchase.completions == [{"purchase_id": "srv-9", "level": 3, "success": False}]
    assert (await completion_rows(flow, 1))[0]["details"]["reason"] == "hardware_fault"


async def test_daemon_stop_during_a_vend_reports_success_false(make_flow):
    flow = await make_flow()
    await to_door_unlocked(flow, level=2)
    purchase_id = flow.purchase_id
    await flow.stop()
    await flow.stop()  # idempotent: one completion, one stop row
    await wait_until(lambda: flow.purchase.completions, "delivery")
    assert flow.purchase.completions == [{"purchase_id": purchase_id, "level": 2, "success": False}]
    assert (await completion_rows(flow, 1))[0]["details"]["reason"] == "daemon_stop"
    rows = await flow.events.recent(100)
    assert len([r for r in rows if r["kind"] == "daemon" and r["details"]["event"] == "stop"]) == 1


async def test_unlock_failure_after_payment_reports_success_false(make_flow):
    flow = await make_flow(hardware_cls=FailingUnlock)
    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    await flow.dispatch(Event.PURCHASE_VALID, purchase_id="srv-1")
    assert flow.state is State.OUT_OF_ORDER
    await wait_until(lambda: flow.purchase.completions, "delivery")
    assert flow.purchase.completions == [{"purchase_id": "srv-1", "level": 3, "success": False}]
    assert (await completion_rows(flow, 1))[0]["details"]["reason"] == "hardware_fault"


async def test_normal_vend_and_door_alarm_are_not_abandoned(make_flow):
    flow = await make_flow(door_alarm_delay_s=0.05)
    await to_door_unlocked(flow)
    await flow.dispatch(Event.DOOR_OPENED)
    await wait_for(flow, State.DOOR_ALARM)  # the timeout here is not an abandonment
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.IDLE)
    await wait_until(lambda: flow.purchase.completions, "delivery")
    assert [c["success"] for c in flow.purchase.completions] == [True]


async def test_rejected_purchase_returns_to_idle_with_last_result(make_flow):
    flow = await make_flow()
    flow.purchase.simulate_invalid(3)
    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    await wait_for(flow, State.IDLE)
    assert flow.last_result == "invalid" and flow.status()["last_result"] == "invalid"
    check = [r for r in await flow.events.recent(20) if r["kind"] == "purchase_check"][0]
    assert check["details"]["valid"] is False and check["level"] == 3
    assert flow.purchase.completions == []
    await flow.dispatch(Event.SELECT_LEVEL, level=2)  # the next transition clears it
    assert flow.last_result is None


async def test_purchase_server_errors_keep_the_poll_going(make_flow, caplog):
    flow = await make_flow(purchase=FlakyPurchase(3))
    flow.purchase.simulate_payment(2)
    with caplog.at_level(logging.WARNING, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=2)
        await wait_for(flow, State.DOOR_UNLOCKED)
    assert flow.purchase.errors == 3
    warnings = [r for r in caplog.records if "purchase check failed" in r.getMessage()]
    assert len(warnings) == 1  # once per checking_purchase, not per poll
