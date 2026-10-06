"""The daemon: owns config, hardware, purchase server, event log, flow, motor, LEDs, audio and
the web server."""

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from monitoni import __version__
from monitoni.audio import Audio, MockAudio
from monitoni.config import Config
from monitoni.eventlog import EventLog
from monitoni.feedback import Feedback
from monitoni.flow import REASON_HARDWARE, Event, Flow, IllegalTransition, State
from monitoni.hardware.base import DoorEvent, Hardware, HardwareError, HardwareFault
from monitoni.leds import Leds, MockLeds
from monitoni.motor import Motor
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer, PurchaseServer
from monitoni.runtime import Runtime
from monitoni.web.server import create_app
from monitoni.web.settings import DEFAULT_PIN

log = logging.getLogger(__name__)


class Daemon:
    recovery_check_s = 1.0  # how often the drainer asks whether a hardware fault has cleared
    recovery_dwell_s = 10.0  # the hardware must be healthy this long, without a break, first
    recovery_holdoff_s = 30.0  # pause after a recovery attempt that ended in out_of_order again
    identity_refresh_s = 60.0  # how often the machine's own IP is looked up again

    def __init__(self, config: Config, hardware: Hardware, purchase: PurchaseServer,
                 leds: Leds | None = None, audio: Audio | None = None,
                 runtime: Runtime | None = None) -> None:
        self.config = config
        self.hardware = hardware
        self.purchase = purchase
        if runtime is None:  # __main__ loads it before building the LEDs; tests mostly do not
            runtime = Runtime.load(config.database.path.parent / "runtime.json", config)
            runtime.apply(config)
        self.runtime = runtime
        self.leds = leds or MockLeds(config)
        self.audio = audio or MockAudio(config.hardware.audio.volume, config.hardware.audio.enabled)
        self.events = EventLog(config.database.path)
        self.changed = asyncio.Event()  # set by the flow on every state change
        self.outbox = Outbox(config.database.path, purchase, self.events,
                             config.purchase_server.outbox_backoff_s,
                             lambda: self.flow.state.value, on_change=self.changed.set)
        self.flow = Flow(config, hardware, purchase, self.outbox, self.events,
                         on_change=self._flow_changed, runtime=runtime)
        self.flow.on_transition.append(self._after_transition)
        self.feedback = Feedback(self.flow, self.leds, self.audio)
        purchase.on_reachability = self._purchase_reachability
        self.leds.on_reachability = self._wled_reachability
        self._row_tasks: set[asyncio.Task] = set()
        self.motor = Motor(config.hardware.motor, hardware, self.events, hardware.events,
                           lambda: self.flow.state.value, on_change=self.changed.set)
        self._started_at: float | None = None
        self.hostname = socket.gethostname()
        self.ip: str | None = None  # the address of the interface with the default route
        self._identity_task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None
        self._drain_task: asyncio.Task | None = None
        self._stops: list[Callable[[], Awaitable[None]]] = []  # what to undo, in start order

    async def start(self) -> None:
        """Start everything in order. If any step fails, undo the earlier ones and re-raise."""
        self._started_at = time.monotonic()
        if self.config.settings.pin == DEFAULT_PIN:
            log.warning("settings.pin is still the default %s: set it in config/local.yaml "
                        "(docs/SETUP.md section 6)", DEFAULT_PIN)
        try:
            await self.events.start()
            self._stops.append(self.events.stop)
            await self.leds.start()
            self._stops.append(self.leds.stop)
            await self.audio.start()
            self._stops.append(self.audio.stop)
            await self.purchase.start()
            self._stops.append(self.purchase.stop)
            await self.outbox.start()
            self._stops.append(self.outbox.stop)
            await self.hardware.start()
            self._stops.append(self.hardware.stop)
            self._stops.append(lambda: self.stop_motor("daemon_stop"))
            await self.flow.start()
            self._stops.append(self.flow.stop)
            self._drain_task = asyncio.create_task(self._drain_hardware_events(), name="hw-events")
            self._stops.append(self._stop_drain)
            self.ip = default_route_ip()
            self._identity_task = asyncio.create_task(self._refresh_identity(), name="identity")
            self._stops.append(self._stop_identity)
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

    async def _stop_identity(self) -> None:
        task, self._identity_task = self._identity_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _refresh_identity(self) -> None:
        """The machine's IP can change (DHCP, cable); look it up on a timer, not per status."""
        while True:
            await asyncio.sleep(self.identity_refresh_s)
            ip = default_route_ip()
            if ip != self.ip:
                log.info("machine IP %s -> %s", self.ip, ip)
                self.ip = ip
                self.changed.set()

    # -- motor stop rule -----------------------------------------------------

    def _flow_changed(self) -> None:
        self.changed.set()

    async def _after_transition(self, old: State | None, new: State) -> None:
        """Every transition stops the motor and closes the spindle lock, explicitly, the way the
        entry hook locks all doors: a TURN held across Exit or a spindle opened by the settings
        tool ends here. Nothing to switch when the motor module says nothing is on."""
        if old is not None:
            await self.stop_motor(f"leave_{old.value}")

    async def stop_motor(self, reason: str) -> None:
        """Hard stop; a hardware error is logged and queued as a fault by the motor itself."""
        with contextlib.suppress(HardwareError):
            await self.motor.stop(reason)

    # -- purchase server -----------------------------------------------------

    def _purchase_reachability(self, ok: bool) -> None:
        """One `network` row per change of reachability, and a status push."""
        self._network_row({"purchase_server": "reachable" if ok else "unreachable",
                           "error": self.purchase.status().get("last_error")})

    def _wled_reachability(self, ok: bool) -> None:
        """The LED controller answered (or stopped answering) its health poll. Status only."""
        self._network_row({"component": "wled", "reachable": ok})

    def _network_row(self, details: dict) -> None:
        task = asyncio.get_running_loop().create_task(
            self.events.write("network", self.flow.state.value, details=details),
            name="network-row")
        self._row_tasks.add(task)
        task.add_done_callback(self._row_tasks.discard)
        self.changed.set()

    # -- hardware events -----------------------------------------------------

    async def _drain_hardware_events(self) -> None:
        """Forward door events and faults to the flow; end a hardware fault once it has cleared.

        Recovery needs `hardware.healthy()` to hold for `recovery_dwell_s` without a break, so a
        flapping link does not flap the machine; a check that finds it unhealthy restarts the
        streak. A recovery attempt that ends in out_of_order again waits `recovery_holdoff_s`.
        """
        loop = asyncio.get_running_loop()
        healthy_since: float | None = None
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
            now = loop.time()
            if not self.hardware.healthy():
                healthy_since = None
            elif healthy_since is None:
                healthy_since = now
            if (self.flow.state is State.OUT_OF_ORDER and self.flow.reason == REASON_HARDWARE
                    and healthy_since is not None and now - healthy_since >= self.recovery_dwell_s
                    and now >= next_recovery_at):
                log.info("hardware healthy for %.0fs, leaving out_of_order", now - healthy_since)
                with contextlib.suppress(IllegalTransition):
                    await self.flow.dispatch(Event.HARDWARE_OK)
                if self.flow.state is State.OUT_OF_ORDER:
                    next_recovery_at = loop.time() + self.recovery_holdoff_s

    # -- status ----------------------------------------------------------------

    @property
    def purchase_is_mock(self) -> bool:
        """Payments can be simulated while the purchase server is the mock (every mode so far)."""
        return isinstance(self.purchase, MockPurchaseServer)

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
        motor_cfg = self.config.hardware.motor
        return {
            "machine_id": self.config.system.machine_id,
            "app_version": __version__,
            "hostname": self.hostname,
            "ip": self.ip,
            "hardware_mode": self.config.hardware.mode,
            "purchase_mode": "mock" if self.purchase_is_mock else "real",
            "uptime_s": round(uptime, 1),
            **flow,
            "levels": levels,
            "doors": {str(n): hardware["doors"].get(n, "unknown") for n in range(1, levels + 1)},
            "qr_url": None if level is None else f"/api/qr/{level}.png",
            "maintenance_message": self.config.system.maintenance_message,
            "hardware": hardware,
            "motor": self.motor.status(),
            "leds": self.leds.status(),
            "audio": self.audio.status(),
            "purchase_server": {**self.purchase.status(),
                                "outbox_pending": self.outbox.pending_count,
                                "base_url": urlsplit(self.config.purchase_server.base_url).netloc},
            "settings": {"pin_is_default": self.config.settings.pin == DEFAULT_PIN,
                         "out_of_order": self.runtime.out_of_order},
            # read-only installation values the settings screens show; no secrets
            "config_view": {
                "motor": {"spindle_pre_delay_ms": motor_cfg.spindle_pre_delay_ms,
                          "spin_after_release_ms": motor_cfg.spin_after_release_ms,
                          "spindle_post_delay_ms": motor_cfg.spindle_post_delay_ms,
                          "max_run_s": motor_cfg.max_run_s},
                "led": {"zones": self.config.led.zones},
                "door_locks": {"channels": self.config.hardware.door_locks.channels},
                "wled": {"ip_address": self.config.hardware.wled.ip_address},
            },
        }


def default_route_ip() -> str | None:
    """The address of the interface the default route uses, or None without one. Connecting a
    UDP socket sends nothing; it only makes the kernel pick the route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            return sock.getsockname()[0]
        except OSError:
            return None
