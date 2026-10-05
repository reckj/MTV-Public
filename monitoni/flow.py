"""The customer flow state machine. Talks to Hardware, PurchaseServer and EventLog only.

Transitions are a table. One timeout at a time, cancelled on every transition.
All hardware side effects live in `_enter` and `_leave`; `unlock_door` is called
from exactly one place, every lock goes through the idle/out_of_order hook.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from enum import StrEnum

from monitoni.config import Config
from monitoni.eventlog import EventLog
from monitoni.hardware.base import Hardware
from monitoni.purchase import PurchaseServer

log = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "idle"
    SLEEP = "sleep"
    CHECKING_PURCHASE = "checking_purchase"
    DOOR_UNLOCKED = "door_unlocked"
    DOOR_OPENED = "door_opened"
    DOOR_ALARM = "door_alarm"
    COMPLETING = "completing"
    OUT_OF_ORDER = "out_of_order"


class Event(StrEnum):
    SELECT_LEVEL = "select_level"
    CANCEL = "cancel"
    TOUCH = "touch"
    PURCHASE_VALID = "purchase_valid"
    PURCHASE_INVALID = "purchase_invalid"
    DOOR_OPENED = "door_opened"
    DOOR_CLOSED = "door_closed"
    COMPLETE = "complete"
    TIMEOUT = "timeout"
    RESET = "reset"


TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.IDLE, Event.SELECT_LEVEL): State.CHECKING_PURCHASE,
    (State.IDLE, Event.TIMEOUT): State.SLEEP,
    (State.SLEEP, Event.TOUCH): State.IDLE,
    (State.CHECKING_PURCHASE, Event.PURCHASE_VALID): State.DOOR_UNLOCKED,
    # purchase_invalid: only the real purchase client emits it; its meaning is defined there
    (State.CHECKING_PURCHASE, Event.PURCHASE_INVALID): State.IDLE,
    (State.CHECKING_PURCHASE, Event.CANCEL): State.IDLE,
    (State.CHECKING_PURCHASE, Event.TIMEOUT): State.IDLE,
    (State.DOOR_UNLOCKED, Event.DOOR_OPENED): State.DOOR_OPENED,
    (State.DOOR_UNLOCKED, Event.TIMEOUT): State.IDLE,
    (State.DOOR_OPENED, Event.DOOR_CLOSED): State.COMPLETING,
    (State.DOOR_OPENED, Event.TIMEOUT): State.DOOR_ALARM,
    (State.DOOR_ALARM, Event.DOOR_CLOSED): State.COMPLETING,
    (State.COMPLETING, Event.COMPLETE): State.IDLE,
}
# reset aborts whatever is going on; out_of_order stays until the settings area lifts it
TRANSITIONS.update({(s, Event.RESET): State.IDLE for s in State if s is not State.OUT_OF_ORDER})

# which config timing applies when a state is entered
TIMEOUTS: dict[State, str] = {
    State.IDLE: "sleep_timeout_s",
    State.CHECKING_PURCHASE: "purchase_timeout_s",
    State.DOOR_UNLOCKED: "door_unlock_timeout_s",
    State.DOOR_OPENED: "door_alarm_delay_s",
}


class IllegalTransition(Exception):
    def __init__(self, state: State, event: Event) -> None:
        super().__init__(f"{event.value} is not allowed in state {state.value}")
        self.state = state
        self.event = event


class Flow:
    def __init__(self, config: Config, hardware: Hardware, purchase: PurchaseServer,
                 events: EventLog, on_change: Callable[[], None] | None = None) -> None:
        self.config = config
        self.hardware = hardware
        self.purchase = purchase
        self.events = events
        self.on_change = on_change or (lambda: None)

        self.state = State.IDLE
        self.selected_level: int | None = None
        self.purchase_id: str | None = None

        self._lock = asyncio.Lock()
        self._timeout_task: asyncio.Task | None = None
        self._timeout_deadline: float | None = None
        self._poll_task: asyncio.Task | None = None
        self._complete_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            initial = State.OUT_OF_ORDER if self.config.system.maintenance_mode else State.IDLE
            log.info("flow starting in %s", initial.value)
            await self._enter(initial)
            self.state = initial
            await self.events.write("daemon", initial.value, details={"event": "start"})
        self.on_change()

    async def stop(self) -> None:
        async with self._lock:
            self._cancel_tasks()
            await self.events.write("daemon", self.state.value, details={"event": "stop"})

    # -- status --------------------------------------------------------------

    @property
    def countdown_s(self) -> float | None:
        """Seconds left in the current state's timeout, or None if it has none."""
        if self._timeout_task is None or self._timeout_task.done():
            return None
        return max(0.0, self._timeout_deadline - asyncio.get_running_loop().time())

    def status(self) -> dict:
        countdown = self.countdown_s
        return {
            "state": self.state.value,
            "selected_level": self.selected_level,
            "purchase_id": self.purchase_id,
            "countdown_s": None if countdown is None else round(countdown, 1),
        }

    # -- events in -----------------------------------------------------------

    async def dispatch(self, event: Event, level: int | None = None,
                       purchase_id: str | None = None) -> None:
        """Apply an event. Raises IllegalTransition (logged) if the table has no entry."""
        async with self._lock:
            if event is Event.TOUCH and self.state is not State.SLEEP:
                # accepted everywhere; only idle has a sleep timer to reset
                if self.state is State.IDLE:
                    self._start_timeout(self.state)
                await self.events.write("command", self.state.value, details={"event": "touch"})
                return

            new_state = TRANSITIONS.get((self.state, event))
            if new_state is None:
                log.warning("rejected %s in state %s", event.value, self.state.value)
                await self.events.write("rejected", self.state.value, level=self.selected_level,
                                        purchase_id=self.purchase_id,
                                        details={"event": event.value})
                raise IllegalTransition(self.state, event)

            if event is Event.SELECT_LEVEL:
                if level is None or not 1 <= level <= self.config.vending.levels:
                    raise ValueError(f"level must be 1..{self.config.vending.levels}")
                self.selected_level = level
            if event is Event.PURCHASE_VALID and purchase_id is not None:
                self.purchase_id = purchase_id

            await self._transition(new_state, event)
        self.on_change()

    # -- transition machinery ------------------------------------------------

    async def _transition(self, new_state: State, event: Event) -> None:
        """Leave the old state, enter the new one, then make it visible.

        Invariant: status never shows a state whose entry hook has not completed.
        `self.state` is assigned right after `_enter` returns, with no await in
        between, so the tasks `_enter` starts never observe the old state. The
        transition row is written after that; `dispatch` notifies once the lock is
        released. A hook that raises leaves the state unchanged (hardware errors in
        hooks are an open decision, see CLAUDE.md).
        """
        old_state = self.state
        level, purchase_id = self.selected_level, self.purchase_id  # before the hooks change them
        log.info("%s --%s--> %s", old_state.value, event.value, new_state.value)
        self._cancel_tasks()
        await self._leave(old_state)
        await self._enter(new_state)
        self.state = new_state
        await self.events.write("transition", new_state.value, level=level,
                                purchase_id=purchase_id,
                                details={"from": old_state.value, "to": new_state.value,
                                         "event": event.value})

    async def _leave(self, state: State) -> None:
        if state is State.DOOR_ALARM:
            await self.hardware.alarm(False)

    async def _enter(self, state: State) -> None:
        if state in (State.IDLE, State.OUT_OF_ORDER):
            await self.hardware.lock_all_doors()
            self.selected_level = None
            self.purchase_id = None
            # hook: LED idle animation (later milestone)
        elif state is State.CHECKING_PURCHASE:
            self.purchase_id = uuid.uuid4().hex
            self._poll_task = asyncio.create_task(
                self._poll_purchase(self.selected_level), name="purchase-poll")
        elif state is State.DOOR_UNLOCKED:
            await self.hardware.unlock_door(self.selected_level)
            # hook: LED level highlight + success sound (later milestone)
        elif state is State.DOOR_ALARM:
            await self.hardware.alarm(True)
            # hook: alarm sound + LED flash (later milestone)
        elif state is State.COMPLETING:
            self._complete_task = asyncio.create_task(self._complete(), name="purchase-complete")
        self._start_timeout(state)

    def _start_timeout(self, state: State) -> None:
        self._cancel(self._timeout_task)
        self._timeout_task = None
        self._timeout_deadline = None
        key = TIMEOUTS.get(state)
        if key is None:
            return
        seconds = getattr(self.config.vending.timings, key)
        self._timeout_deadline = asyncio.get_running_loop().time() + seconds
        self._timeout_task = asyncio.create_task(self._timeout(seconds), name=f"timeout-{key}")

    def _cancel_tasks(self) -> None:
        for task in (self._timeout_task, self._poll_task, self._complete_task):
            self._cancel(task)
        self._timeout_task = self._poll_task = self._complete_task = None
        self._timeout_deadline = None

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        # a task that dispatches the event causing its own cancellation finishes on its own
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    # -- background tasks (each ends by dispatching one event) ---------------

    async def _timeout(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if asyncio.current_task() is not self._timeout_task:
            return  # superseded while waiting; a cancelled task normally never gets here
        log.info("timeout after %.1fs in %s", seconds, self.state.value)
        await self.events.write("timeout", self.state.value, level=self.selected_level,
                                purchase_id=self.purchase_id, details={"seconds": seconds})
        await self.dispatch(Event.TIMEOUT)

    async def _poll_purchase(self, level: int) -> None:
        machine_id = self.config.system.machine_id
        interval = self.config.purchase_server.poll_interval_s
        # negative polls are not logged: that would be two rows a second for up to 120 s
        while True:
            server_id = await self.purchase.check(machine_id, level)
            if server_id is not None:
                await self.events.write("purchase_check", self.state.value, level=level,
                                        purchase_id=server_id,
                                        details={"valid": True, "local_id": self.purchase_id})
                await self.dispatch(Event.PURCHASE_VALID, purchase_id=server_id)
                return
            await asyncio.sleep(interval)

    async def _complete(self) -> None:
        level, purchase_id = self.selected_level, self.purchase_id
        ok = await self.purchase.complete(purchase_id, self.config.system.machine_id,
                                          level, success=True)
        if not ok:
            log.error("purchase completion not acknowledged for %s", purchase_id)
        await self.events.write("purchase_complete", self.state.value, level=level,
                                purchase_id=purchase_id, details={"ok": ok})
        await self.dispatch(Event.COMPLETE)
