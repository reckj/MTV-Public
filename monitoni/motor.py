"""Hold-to-turn motor sequence. The browser only sends press and release; the rest is here.

press   -> spindle lock ON -> spindle_pre_delay_ms -> motor ON
release -> spin_after_release_ms -> motor OFF -> spindle_post_delay_ms -> spindle lock OFF

The motor always stops: on release, after max_run_s without a release (watchdog), and via
`stop()`, which the daemon calls on every flow transition (a TURN held across Exit ends there),
when the last WebSocket closes and on daemon stop. `stop()` runs the release sequence at once,
without spin_after_release_ms, and closes the spindle lock whatever opened it: this module is
the one owner of the spindle state, the settings tool goes through `set_spindle`.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine

from monitoni.config import MotorConfig
from monitoni.eventlog import EventLog
from monitoni.hardware.base import Hardware, HardwareError, HardwareFault

log = logging.getLogger(__name__)


class MotorBusy(Exception):
    """set_spindle while a TURN sequence is pressed or running."""


class Motor:
    def __init__(self, config: MotorConfig, hardware: Hardware, events: EventLog,
                 faults: asyncio.Queue, state_name: Callable[[], str],
                 on_change: Callable[[], None] | None = None) -> None:
        self.config = config
        self.hardware = hardware
        self.events = events
        self.faults = faults  # the hardware event queue; a failed sequence puts a fault there
        self.state_name = state_name  # for the event-log rows
        self.on_change = on_change or (lambda: None)  # the daemon pushes a status on each call
        self.pressed = False
        self.running = False
        self.spindle_open = False
        self._lock = asyncio.Lock()  # one sequence at a time; a release waits for the press
        self._stop_now = asyncio.Event()  # set by stop(): cuts every wait short
        self._watchdog: asyncio.Task | None = None

    def status(self) -> dict:
        return {"pressed": self.pressed, "running": self.running,
                "spindle_open": self.spindle_open}

    @property
    def active(self) -> bool:
        return self.pressed or self.running or self.spindle_open

    async def press(self) -> None:
        """Start the sequence. A second press while pressed does nothing."""
        if self.pressed:
            return
        self.pressed = True
        self._stop_now.clear()
        async with self._lock:
            if not self.pressed:
                return  # released while waiting for the previous sequence to finish
            await self._guarded(self._start_sequence())

    async def release(self, reason: str = "release") -> None:
        """Stop after spin_after_release_ms. A release without a press does nothing."""
        await self._end(reason, immediate=False)

    async def stop(self, reason: str) -> None:
        """Stop now, without spin_after_release_ms, and close the spindle. Idempotent."""
        self._stop_now.set()
        await self._end(reason, immediate=True)

    def reset(self) -> None:
        """relay_core was (re)connected and the hardware wrote motor off and spindle closed:
        forget whatever a sequence thought it was doing, so the owner and the relay agree."""
        self.pressed = self.running = self.spindle_open = False
        self._cancel_watchdog()
        self.on_change()

    async def set_spindle(self, open_: bool) -> None:
        """The settings tool: open or close the spindle lock on its own. Refused while a TURN
        sequence is pressed or running; a HardwareError ends in the emergency stop like a
        failed sequence."""
        if self.pressed or self.running:
            raise MotorBusy("the motor sequence is running; release TURN first")
        async with self._lock:
            if self.pressed or self.running:
                raise MotorBusy("the motor sequence is running; release TURN first")
            await self._guarded(self._spindle_only(open_))

    async def _spindle_only(self, open_: bool) -> None:
        await self.hardware.set_spindle(open_)
        self.spindle_open = open_
        self.on_change()
        await self.events.write("motor", self.state_name(),
                                details={"event": "spindle", "open": open_})

    async def _end(self, reason: str, *, immediate: bool) -> None:
        was_pressed, self.pressed = self.pressed, False
        self._cancel_watchdog()
        if not was_pressed and not (self.running or self.spindle_open):
            return
        async with self._lock:
            if not (self.running or self.spindle_open):
                return  # the sequence we waited for has already switched everything off
            await self._guarded(self._stop_sequence(reason, immediate))

    # -- the sequences (run under the lock) ----------------------------------

    async def _start_sequence(self) -> None:
        await self.hardware.set_spindle(True)
        self.spindle_open = True
        self.on_change()
        await self._wait(self.config.spindle_pre_delay_ms)
        if not self.pressed:
            return  # released during the pre-delay; the release sequence closes the spindle
        await self.hardware.set_motor(True)
        self.running = True
        self.on_change()
        await self.events.write("motor", self.state_name(), details={"event": "start"})
        self._watchdog = asyncio.create_task(self._watchdog_run(), name="motor-watchdog")

    async def _stop_sequence(self, reason: str, immediate: bool) -> None:
        if not immediate:
            await self._wait(self.config.spin_after_release_ms)
        await self.hardware.set_motor(False)
        was_running, self.running = self.running, False
        self.on_change()
        if was_running:
            await self.events.write("motor", self.state_name(),
                                    details={"event": "stop", "reason": reason})
        await asyncio.sleep(self.config.spindle_post_delay_ms / 1000)
        await self.hardware.set_spindle(False)
        self.spindle_open = False
        self.on_change()

    async def _wait(self, ms: int) -> None:
        """Sleep, unless stop() cuts it short."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(ms / 1000):
                await self._stop_now.wait()

    async def _guarded(self, sequence: Coroutine) -> None:
        try:
            await sequence
        except HardwareError as exc:
            self.pressed = False
            self._cancel_watchdog()
            log.error("motor sequence failed: %s", exc)
            await self._emergency_off()
            self.faults.put_nowait(HardwareFault(f"motor: {exc}"))
            raise

    async def _emergency_off(self) -> None:
        """The one place a second write is allowed: it is a stop, not a retry of a start."""
        try:
            await self.hardware.set_motor(False)
            self.running = False
        except HardwareError as exc:
            log.error("emergency motor off failed: %s", exc)
        try:
            await self.hardware.set_spindle(False)
            self.spindle_open = False
        except HardwareError as exc:
            log.error("emergency spindle off failed: %s", exc)
        self.on_change()

    # -- watchdog --------------------------------------------------------------

    async def _watchdog_run(self) -> None:
        await asyncio.sleep(self.config.max_run_s)
        log.warning("motor running for %ss without a release, stopping", self.config.max_run_s)
        with contextlib.suppress(HardwareError):  # logged and queued as a fault by _guarded
            await self.stop("watchdog")

    def _cancel_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
