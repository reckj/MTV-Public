"""The customer flow state machine. Talks to Hardware, PurchaseServer and EventLog only.

Transitions are a table. One timeout at a time, cancelled on every transition.
All hardware side effects live in `_enter` and `_leave`; `unlock_door` is called
from exactly one place, every lock goes through the idle/out_of_order hook.
A hardware error inside a hook ends in out_of_order with reason "hardware".
A door opened while none should be open (idle, sleep, checking_purchase) is
door_forced: alarm on until the door is closed, no timeout, no purchase completion.
Completions go through the outbox (durable, retried there); a paid purchase that
leaves door_unlocked/door_opened/door_alarm without completing is reported with
success=false and the cause.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from enum import StrEnum

from monitoni.config import Config
from monitoni.eventlog import EventLog
from monitoni.hardware.base import Hardware, HardwareError
from monitoni.outbox import Outbox
from monitoni.purchase import Invalid, Paid, PurchaseServer, PurchaseServerError

log = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "idle"
    SLEEP = "sleep"
    CHECKING_PURCHASE = "checking_purchase"
    DOOR_UNLOCKED = "door_unlocked"
    DOOR_OPENED = "door_opened"
    DOOR_ALARM = "door_alarm"
    DOOR_FORCED = "door_forced"
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
    HARDWARE_FAULT = "hardware_fault"
    HARDWARE_OK = "hardware_ok"


# why the machine is out_of_order
REASON_MAINTENANCE = "maintenance"  # config flag, later the settings area; never clears itself
REASON_HARDWARE = "hardware"  # automatic; clears itself once the hardware is healthy again

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
    # a door opened without a purchase: alarm until it is closed; out_of_order keeps rejecting it
    (State.IDLE, Event.DOOR_OPENED): State.DOOR_FORCED,
    (State.SLEEP, Event.DOOR_OPENED): State.DOOR_FORCED,
    (State.CHECKING_PURCHASE, Event.DOOR_OPENED): State.DOOR_FORCED,
    (State.DOOR_FORCED, Event.DOOR_CLOSED): State.IDLE,
    # recovery from a hardware fault; dispatch() rejects it while the reason is maintenance
    (State.OUT_OF_ORDER, Event.HARDWARE_OK): State.IDLE,
}
# reset aborts whatever is going on; out_of_order stays until the settings area lifts it
TRANSITIONS.update({(s, Event.RESET): State.IDLE for s in State if s is not State.OUT_OF_ORDER})
# a hardware fault ends in out_of_order from everywhere (in out_of_order it is a no-op)
TRANSITIONS.update({(s, Event.HARDWARE_FAULT): State.OUT_OF_ORDER
                    for s in State if s is not State.OUT_OF_ORDER})

# a paid purchase is being handed out; leaving these for idle/out_of_order reports success=false
VEND_STATES = frozenset({State.DOOR_UNLOCKED, State.DOOR_OPENED, State.DOOR_ALARM})
ABANDON_REASONS = {Event.TIMEOUT: "unlock_timeout", Event.HARDWARE_FAULT: "hardware_fault",
                   Event.RESET: "reset"}

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
                 outbox: Outbox, events: EventLog,
                 on_change: Callable[[], None] | None = None) -> None:
        self.config = config
        self.hardware = hardware
        self.purchase = purchase
        self.outbox = outbox
        self.events = events
        self.on_change = on_change or (lambda: None)

        self.state = State.IDLE
        self.reason: str | None = None  # while out_of_order: maintenance | hardware
        self.selected_level: int | None = None
        self.purchase_id: str | None = None
        self.last_result: str | None = None  # "invalid" until the transition after a rejection
        self._stopped = False

        self._lock = asyncio.Lock()
        self._timeout_task: asyncio.Task | None = None
        self._timeout_deadline: float | None = None
        self._poll_task: asyncio.Task | None = None
        self._complete_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            if self.config.system.maintenance_mode:
                initial, self.reason = State.OUT_OF_ORDER, REASON_MAINTENANCE
            else:
                initial, self.reason = State.IDLE, None
            log.info("flow starting in %s", initial.value)
            try:
                await self._enter(initial)
            except Exception as exc:
                await self._fault(initial, exc)
                initial = State.OUT_OF_ORDER
            self.state = initial
            await self.events.write("daemon", initial.value,
                                    details={"event": "start", "reason": self.reason})
        self.on_change()

    async def stop(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._cancel_tasks()
            if self.state in VEND_STATES:
                await self._abandon("daemon_stop")
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
            "reason": self.reason,
            "last_result": self.last_result,
            "selected_level": self.selected_level,
            "purchase_id": self.purchase_id,
            "countdown_s": None if countdown is None else round(countdown, 1),
        }

    # -- events in -----------------------------------------------------------

    async def dispatch(self, event: Event, level: int | None = None,
                       purchase_id: str | None = None, error: str | None = None) -> None:
        """Apply an event. Raises IllegalTransition (logged) if the table has no entry."""
        async with self._lock:
            if event is Event.TOUCH and self.state is not State.SLEEP:
                # accepted everywhere; only idle has a sleep timer to reset
                if self.state is State.IDLE:
                    self._start_timeout(self.state)
                await self.events.write("command", self.state.value, details={"event": "touch"})
                return
            if event is Event.HARDWARE_FAULT and self.state is State.OUT_OF_ORDER:
                # already there; a maintenance reason is kept
                log.warning("hardware fault while out_of_order (%s): %s", self.reason, error)
                return

            new_state = TRANSITIONS.get((self.state, event))
            if new_state is None or (event is Event.HARDWARE_OK
                                     and self.reason != REASON_HARDWARE):
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
            if event is Event.HARDWARE_FAULT:
                self.reason = REASON_HARDWARE
            if event is Event.PURCHASE_INVALID:
                self.last_result = "invalid"

            await self._transition(new_state, event, error)
        self.on_change()

    # -- transition machinery ------------------------------------------------

    async def _transition(self, new_state: State, event: Event, error: str | None = None) -> None:
        """Leave the old state, enter the new one, then make it visible.

        Invariant: status never shows a state whose entry hook has not completed.
        `self.state` is assigned right after `_enter` returns, with no await in
        between, so the tasks `_enter` starts never observe the old state. The
        transition row is written after that; `dispatch` notifies once the lock is
        released. A hook that raises ends in out_of_order (hardware) instead.
        """
        old_state = self.state
        level, purchase_id = self.selected_level, self.purchase_id  # before the hooks change them
        log.info("%s --%s--> %s", old_state.value, event.value, new_state.value)
        self._cancel_tasks()
        if event is not Event.PURCHASE_INVALID:
            self.last_result = None
        abandoned = False
        if old_state in VEND_STATES and new_state in (State.IDLE, State.OUT_OF_ORDER):
            await self._abandon(ABANDON_REASONS.get(event, event.value))
            abandoned = True
        try:
            await self._leave(old_state)
            await self._enter(new_state)
        except Exception as exc:
            if not abandoned and (old_state in VEND_STATES or new_state in VEND_STATES):
                await self._abandon("hardware_fault")  # paid, and the door will not open now
            await self._fault(new_state, exc)
            self.state = State.OUT_OF_ORDER
            await self.events.write("transition", self.state.value, level=level,
                                    purchase_id=purchase_id,
                                    details={"from": old_state.value, "to": self.state.value,
                                             "event": Event.HARDWARE_FAULT.value,
                                             "attempted": new_state.value, "error": str(exc)})
            return
        self.state = new_state
        details = {"from": old_state.value, "to": new_state.value, "event": event.value}
        if error is not None:
            details["error"] = error
        await self.events.write("transition", new_state.value, level=level,
                                purchase_id=purchase_id, details=details)

    async def _fault(self, attempted: State, exc: Exception) -> None:
        """Entering `attempted` failed: become out_of_order (hardware). Called under the lock."""
        if isinstance(exc, HardwareError):
            log.error("hardware error entering %s: %s", attempted.value, exc)
        else:
            log.error("bug: entering %s raised %r", attempted.value, exc, exc_info=exc)
        self.reason = REASON_HARDWARE
        self._cancel_tasks()
        await self._enter(State.OUT_OF_ORDER)  # never raises

    async def _abandon(self, reason: str) -> None:
        """A paid purchase that will not be handed out: tell the server, durably."""
        if self.purchase_id is None or self.selected_level is None:
            return
        log.warning("purchase %s (level %d) not completed: %s",
                    self.purchase_id, self.selected_level, reason)
        try:
            await self.outbox.enqueue(self.purchase_id, self.selected_level, success=False,
                                      reason=reason)
        except Exception:
            log.exception("outbox refused the completion of %s; it is lost", self.purchase_id)

    async def _leave(self, state: State) -> None:
        if state in (State.DOOR_ALARM, State.DOOR_FORCED):
            await self.hardware.alarm(False)

    async def _enter(self, state: State) -> None:
        if state is State.OUT_OF_ORDER:
            try:
                await self.hardware.lock_all_doors()
            except Exception as exc:
                log.error("cannot lock doors while entering out_of_order: %s", exc,
                          exc_info=not isinstance(exc, HardwareError))
            self.selected_level = None
            self.purchase_id = None
        elif state is State.IDLE:
            await self.hardware.lock_all_doors()
            self.selected_level = None
            self.purchase_id = None
            self.reason = None
            # hook: LED idle animation (later milestone)
        elif state is State.CHECKING_PURCHASE:
            self.purchase_id = uuid.uuid4().hex
            self._poll_task = asyncio.create_task(
                self._poll_purchase(self.selected_level), name="purchase-poll")
        elif state is State.DOOR_UNLOCKED:
            await self.hardware.unlock_door(self.selected_level)
            # hook: LED level highlight + success sound (later milestone)
        elif state in (State.DOOR_ALARM, State.DOOR_FORCED):
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
        """Ask the purchase server every poll_interval_s; the poll itself is the retry."""
        interval = self.config.purchase_server.poll_interval_s
        warned = False
        # negative polls are not logged: that would be two rows a second for up to 120 s
        while True:
            try:
                result = await self.purchase.check(level)
            except PurchaseServerError as exc:
                if not warned:  # once per checking_purchase, not per poll
                    log.warning("purchase check failed, polling continues: %s", exc)
                    warned = True
                await asyncio.sleep(interval)
                continue
            if isinstance(result, Paid):
                await self.events.write("purchase_check", self.state.value, level=level,
                                        purchase_id=result.purchase_id,
                                        details={"valid": True, "local_id": self.purchase_id})
                await self.dispatch(Event.PURCHASE_VALID, purchase_id=result.purchase_id)
                return
            if isinstance(result, Invalid):
                await self.events.write("purchase_check", self.state.value, level=level,
                                        details={"valid": False, "local_id": self.purchase_id})
                await self.dispatch(Event.PURCHASE_INVALID)
                return
            await asyncio.sleep(interval)

    async def _complete(self) -> None:
        """Queue the completion (the outbox delivers it) and finish; never waits on the network."""
        level, purchase_id = self.selected_level, self.purchase_id
        try:
            await self.outbox.enqueue(purchase_id, level, success=True)
        except Exception:
            log.exception("outbox refused the completion of %s; it is lost", purchase_id)
        await self.dispatch(Event.COMPLETE)
