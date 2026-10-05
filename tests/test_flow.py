import asyncio

import pytest

from monitoni.eventlog import EventLog
from monitoni.flow import TRANSITIONS, Event, Flow, IllegalTransition, State
from monitoni.hardware.mock import MockHardware
from monitoni.purchase import MockPurchaseServer

ALLOWED = sorted(TRANSITIONS.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value))
REJECTED = [
    (State.IDLE, Event.DOOR_OPENED),
    (State.IDLE, Event.COMPLETE),
    (State.IDLE, Event.PURCHASE_VALID),
    (State.IDLE, Event.CANCEL),
    (State.SLEEP, Event.SELECT_LEVEL),
    (State.DOOR_UNLOCKED, Event.SELECT_LEVEL),
    (State.DOOR_UNLOCKED, Event.CANCEL),
    (State.DOOR_OPENED, Event.CANCEL),
    (State.OUT_OF_ORDER, Event.SELECT_LEVEL),
    (State.OUT_OF_ORDER, Event.RESET),
]


@pytest.fixture
async def make_flow(make_config):
    started = []

    async def _make(maintenance: bool = False, **timings: float) -> Flow:
        config = make_config(**timings)
        config.system.maintenance_mode = maintenance
        events = EventLog(config.database.path)
        await events.start()
        hardware = MockHardware(config.vending.levels)
        await hardware.start()
        flow = Flow(config, hardware, MockPurchaseServer(), events)
        await flow.start()
        started.append((flow, events))
        return flow

    yield _make
    for flow, events in started:
        await flow.stop()
        await events.stop()


async def wait_until(predicate, what: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def wait_for(flow: Flow, state: State) -> None:
    """Wait until the flow is in `state` and its entry hooks have run (lock released)."""
    await wait_until(lambda: flow.state is state and not flow._lock.locked(), state.value)


async def force(flow: Flow, state: State) -> None:
    """Put the flow into a state without walking there (test only)."""
    flow.selected_level = 3
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
    complete = [r for r in rows if r["kind"] == "purchase_complete"]
    assert complete and complete[0]["purchase_id"] == server_id and complete[0]["details"]["ok"]


async def test_out_of_order_at_startup(make_flow):
    flow = await make_flow(maintenance=True)
    assert flow.state is State.OUT_OF_ORDER
    assert_all_locked(flow)
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.SELECT_LEVEL, level=1)
    await flow.dispatch(Event.TOUCH)
    assert flow.state is State.OUT_OF_ORDER


async def test_rejected_event_is_logged(make_flow):
    flow = await make_flow()
    with pytest.raises(IllegalTransition):
        await flow.dispatch(Event.DOOR_OPENED)
    rows = await flow.events.recent(1)
    assert rows[0]["kind"] == "rejected" and rows[0]["details"] == {"event": "door_opened"}
