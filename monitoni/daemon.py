"""The daemon: owns config, hardware, purchase server, event log, flow, motor and the web server."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from aiohttp import web

from monitoni.config import Config
from monitoni.eventlog import EventLog
from monitoni.flow import REASON_HARDWARE, Event, Flow, IllegalTransition, State
from monitoni.hardware.base import DoorEvent, Hardware, HardwareError, HardwareFault
from monitoni.motor import Motor
from monitoni.purchase import PurchaseServer
from monitoni.web.server import create_app

log = logging.getLogger(__name__)


class Daemon:
    recovery_check_s = 1.0  # how often the drainer asks whether a hardware fault has cleared
    recovery_holdoff_s = 30.0  # pause after a recovery attempt that ended in out_of_order again

    def __init__(self, config: Config, hardware: Hardware, purchase: PurchaseServer) -> None:
        self.config = config
        self.hardware = hardware
        self.purchase = purchase
        self.events = EventLog(config.database.path)
        self.changed = asyncio.Event()  # set by the flow on every state change
        self.flow = Flow(config, hardware, purchase, self.events, on_change=self._flow_changed)
        self.motor = Motor(config.hardware.motor, hardware, self.events, hardware.events,
                           lambda: self.flow.state.value, on_change=self.changed.set)
        self._started_at: float | None = None
        self._runner: web.AppRunner | None = None
        self._drain_task: asyncio.Task | None = None
        self._motor_stop_task: asyncio.Task | None = None
        self._stops: list[Callable[[], Awaitable[None]]] = []  # what to undo, in start order

    async def start(self) -> None:
        """Start everything in order. If any step fails, undo the earlier ones and re-raise."""
        self._started_at = time.monotonic()
        try:
            await self.events.start()
            self._stops.append(self.events.stop)
            await self.hardware.start()
            self._stops.append(self.hardware.stop)
            self._stops.append(lambda: self.stop_motor("daemon_stop"))
            await self.flow.start()
            self._stops.append(self.flow.stop)
            self._drain_task = asyncio.create_task(self._drain_hardware_events(), name="hw-events")
            self._stops.append(self._stop_drain)
            self._runner = web.AppRunner(create_app(self), access_log=None)
            self._stops.append(self._stop_web)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.config.web.host, self.config.web.port)
            await site.start()
        except BaseException:
            await self._stop_all()
            raise
        log.info("daemon started, machine %s, hardware %s, UI at %s",
                 self.config.system.machine_id, self.config.hardware.mode, self.url)

    async def stop(self) -> None:
        """Stop in reverse order. Every step runs even if an earlier one fails."""
        await self._stop_all()

    async def _stop_all(self) -> None:
        first_error: BaseException | None = None
        while self._stops:
            stop = self._stops.pop()
            try:
                await stop()
            except Exception as exc:
                log.exception("error while stopping")
                first_error = first_error or exc
        log.info("daemon stopped")
        if first_error is not None:
            raise first_error

    async def _stop_web(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    async def _stop_drain(self) -> None:
        task, self._drain_task = self._drain_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- motor stop rule -----------------------------------------------------

    def _flow_changed(self) -> None:
        self.changed.set()
        if self.flow.state is not State.IDLE and self.motor.active:
            self._motor_stop_task = asyncio.create_task(self.stop_motor("leave_idle"),
                                                        name="motor-stop")

    async def stop_motor(self, reason: str) -> None:
        """Hard stop; a hardware error is logged and queued as a fault by the motor itself."""
        with contextlib.suppress(HardwareError):
            await self.motor.stop(reason)

    # -- hardware events -----------------------------------------------------

    async def _drain_hardware_events(self) -> None:
        """Forward door events and faults to the flow; end a hardware fault once it has cleared."""
        loop = asyncio.get_running_loop()
        next_recovery_at = 0.0
        while True:
            try:
                async with asyncio.timeout(self.recovery_check_s):
                    item = await self.hardware.events.get()
            except TimeoutError:
                item = None
            if isinstance(item, DoorEvent):
                await self.events.write("hardware", self.flow.state.value,
                                        level=self.flow.selected_level,
                                        details={"event": item.value})
                with contextlib.suppress(IllegalTransition):  # the flow already logged it
                    await self.flow.dispatch(Event(item.value))
            elif isinstance(item, HardwareFault):
                await self.events.write("hardware", self.flow.state.value,
                                        level=self.flow.selected_level,
                                        details={"event": "fault", "error": item.message})
                await self.flow.dispatch(Event.HARDWARE_FAULT, error=item.message)
            if (self.flow.state is State.OUT_OF_ORDER and self.flow.reason == REASON_HARDWARE
                    and self.hardware.healthy() and loop.time() >= next_recovery_at):
                log.info("hardware is healthy again, leaving out_of_order")
                with contextlib.suppress(IllegalTransition):
                    await self.flow.dispatch(Event.HARDWARE_OK)
                if self.flow.state is State.OUT_OF_ORDER:
                    next_recovery_at = loop.time() + self.recovery_holdoff_s

    # -- status ----------------------------------------------------------------

    @property
    def url(self) -> str:
        """Actual bound address, useful when the configured port is 0."""
        if self._runner is None or not self._runner.addresses:
            raise RuntimeError("daemon is not running")
        host, port = self._runner.addresses[0][:2]
        return f"http://{host}:{port}"

    def status(self) -> dict:
        """The status object served by /api/status and pushed over /ws."""
        uptime = 0.0 if self._started_at is None else time.monotonic() - self._started_at
        levels = self.config.vending.levels
        flow = self.flow.status()
        level = flow["selected_level"]
        hardware = self.hardware.status()
        return {
            "machine_id": self.config.system.machine_id,
            "hardware_mode": self.config.hardware.mode,
            "uptime_s": round(uptime, 1),
            **flow,
            "levels": levels,
            "doors": {str(n): hardware["doors"].get(n, "unknown") for n in range(1, levels + 1)},
            "qr_url": None if level is None else f"/api/qr/{level}.png",
            "maintenance_message": self.config.system.maintenance_message,
            "hardware": hardware,
            "motor": self.motor.status(),
        }
