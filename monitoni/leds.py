"""LED feedback on the WLED strip over ArtNet. Not safety-relevant: nothing here reaches the flow.

Patterns are a table: name -> how to render a frame at time t for a level. A frame is one RGB
triple per pixel, before `led.brightness` is applied. The zones (one pixel range per level) and
the colours come from the config; the mapping state -> pattern lives in feedback.py.

ArtnetLeds builds the ArtDMX packets itself (`artdmx_packet`) and sends them from one UDP
socket, 170 pixels per universe starting at hardware.wled.universe. It renders at `fps` only
while the pattern animates (breathing, flash, fade); a steady pattern is sent once on change and
then once a second as a keepalive, because WLED falls back to its own preset after a few seconds
without ArtNet. A clean stop sends one dark frame. ArtNet is UDP and says nothing back, so
reachability is a GET /json/info every health_poll_s; it is status only, as is a failing send
(logged once when it starts failing and once when it works again, never per frame).
"""

import asyncio
import contextlib
import logging
import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from monitoni.config import Config
from monitoni.stamp import local_time

log = logging.getLogger(__name__)

Rgb = tuple[int, int, int]
Frame = list[Rgb]

OFF: Rgb = (0, 0, 0)
PIXELS_PER_UNIVERSE = 170  # 510 of the 512 DMX channels
OPCODE_DMX = 0x5000
PROTOCOL_VERSION = 14
KEEPALIVE_S = 1.0  # resend a steady frame this often (WLED's realtime timeout is 2.5 s)
BREATH_PERIOD_S = 1.5
BREATH_FLOOR = 0.2  # the breathing zone never goes darker than this fraction of its colour
FLASH_HZ = 2.0
THANKS_S = 1.0  # the fade from `open` to `idle`


def artdmx_packet(universe: int, sequence: int, data: bytes) -> bytes:
    """One ArtDMX packet: the 18-byte header, then `data` padded to an even length.

    "Art-Net\\0", OpDmx 0x5000 low byte first, protocol version 14 high byte first, sequence
    (1..255; 0 would mean "no sequence"), physical 0, the 15-bit universe low byte first, the
    data length high byte first (even, 2..512), the channel data.
    """
    if len(data) % 2:
        data += b"\0"
    if not 1 <= sequence <= 255 or not 2 <= len(data) <= 512:
        raise ValueError(f"ArtDMX: sequence {sequence}, {len(data)} channels")
    return (b"Art-Net\0" + OPCODE_DMX.to_bytes(2, "little") + PROTOCOL_VERSION.to_bytes(2, "big")
            + bytes((sequence, 0)) + (universe & 0x7FFF).to_bytes(2, "little")
            + len(data).to_bytes(2, "big") + data)


def scale(rgb: Rgb, factor: float) -> Rgb:
    return tuple(round(c * factor) for c in rgb)  # type: ignore[return-value]


def mix(a: Rgb, b: Rgb, k: float) -> Rgb:
    """`a` at k=0, `b` at k=1."""
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b, strict=True))  # type: ignore


class Layout:
    """One machine's strip: pixel count, the zone of every level, the configured colours."""

    def __init__(self, config: Config) -> None:
        self.pixel_count = config.hardware.wled.pixel_count
        self.zones: list[tuple[int, int]] = [(a, b) for a, b in config.led.zones]  # index = level-1
        self.colours: dict[str, Rgb] = {name: tuple(rgb) for name, rgb
                                        in config.led.colours.model_dump().items()}

    def paint(self, others: Rgb, level: int | None = None, colour: Rgb = OFF) -> Frame:
        """Every zone in `others`, the zone of `level` in `colour`; pixels outside the zones off."""
        frame = [OFF] * self.pixel_count
        for n, (start, end) in enumerate(self.zones, start=1):
            rgb = colour if n == level else others
            for i in range(start, end + 1):
                frame[i] = rgb
        return frame

    def off(self) -> Frame:
        return [OFF] * self.pixel_count

    @staticmethod
    def scaled(frame: Frame, brightness: float) -> Frame:
        return [scale(rgb, brightness) for rgb in frame]


# -- the pattern table ---------------------------------------------------------------

def _off(lay: Layout, t: float, level: int | None) -> Frame:
    return lay.off()


def _idle(lay: Layout, t: float, level: int | None) -> Frame:
    return lay.paint(lay.colours["idle"])


def _selected(lay: Layout, t: float, level: int | None) -> Frame:
    return lay.paint(lay.colours["idle"], level, lay.colours["selected"])


def _unlocked(lay: Layout, t: float, level: int | None) -> Frame:
    wave = 0.5 + 0.5 * math.cos(2 * math.pi * t / BREATH_PERIOD_S)  # 1 at t=0, 0 at half period
    factor = BREATH_FLOOR + (1 - BREATH_FLOOR) * wave
    return lay.paint(OFF, level, scale(lay.colours["unlocked"], factor))


def _open(lay: Layout, t: float, level: int | None) -> Frame:
    return lay.paint(OFF, level, lay.colours["open"])


def _alarm(lay: Layout, t: float, level: int | None) -> Frame:
    on = (t * FLASH_HZ) % 1.0 < 0.5
    return lay.paint(lay.colours["alarm"] if on else OFF)


def _fault(lay: Layout, t: float, level: int | None) -> Frame:
    return lay.paint(scale(lay.colours["fault"], 0.5))


def _thanks(lay: Layout, t: float, level: int | None) -> Frame:
    k = min(t / THANKS_S, 1.0)
    return [mix(a, b, k) for a, b in zip(_open(lay, t, level), _idle(lay, t, level), strict=True)]


@dataclass(frozen=True)
class Pattern:
    render: Callable[[Layout, float, int | None], Frame]
    animated: bool = False  # True: frames at fps; False: one frame on change plus keepalives
    ends_after_s: float | None = None  # a timed pattern turns into `then` when its time is up
    then: str | None = None


PATTERNS: dict[str, Pattern] = {
    "off": Pattern(_off),
    "idle": Pattern(_idle),
    "selected": Pattern(_selected),
    "unlocked": Pattern(_unlocked, animated=True),
    "open": Pattern(_open),
    "alarm": Pattern(_alarm, animated=True),
    "fault": Pattern(_fault),
    "thanks": Pattern(_thanks, animated=True, ends_after_s=THANKS_S, then="idle"),
}


# -- the interface -------------------------------------------------------------------

class Leds(Protocol):
    on_reachability: Callable[[bool], None]  # called when `reachable` changes

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def set_pattern(self, pattern: str, level: int | None = None) -> None:
        """Show a pattern from PATTERNS; `level` is the zone it concerns. Raises ValueError."""
        ...

    def set_brightness(self, brightness: float) -> None: ...

    def fill(self, rgb: Rgb) -> None:
        """Settings tool: every pixel in one colour."""
        ...

    def light_level(self, level: int, rgb: Rgb) -> None:
        """Settings tool: one zone in a colour, everything else off."""
        ...

    def off(self) -> None: ...

    def status(self) -> dict:
        """{enabled, reachable, since, pattern, level, brightness}"""
        ...


@dataclass
class _Showing:
    """What the strip is showing right now."""

    name: str
    level: int | None
    render: Callable[[float], Frame]  # of the seconds since `started`
    animated: bool
    started: float  # time.monotonic()
    ends_after_s: float | None = None
    then: str | None = None


def _showing(layout: Layout, name: str, level: int | None) -> _Showing:
    pattern = PATTERNS[name]
    return _Showing(name, level, lambda t: pattern.render(layout, t, level), pattern.animated,
                    time.monotonic(), pattern.ends_after_s, pattern.then)


class _Base:
    """What ArtnetLeds and MockLeds share: the current pattern, brightness, the manual frames."""

    def __init__(self, config: Config) -> None:
        self.layout = Layout(config)
        self.brightness = config.led.brightness
        self.enabled = config.hardware.wled.enabled  # the config flag, for the settings screens
        self.on_reachability: Callable[[bool], None] = lambda ok: None
        self.reachable: bool | None = None
        self.since: str | None = None  # when `reachable` last flipped; None while ok since start
        self._showing = _showing(self.layout, "off", None)

    def set_pattern(self, pattern: str, level: int | None = None) -> None:
        if pattern not in PATTERNS:
            raise ValueError(f"unknown LED pattern {pattern!r}")
        self._show(_showing(self.layout, pattern, level))

    def fill(self, rgb: Rgb) -> None:
        colour, frame = tuple(rgb), [tuple(rgb)] * self.layout.pixel_count
        self._show(_Showing("fill", None, lambda t: list(frame), False, time.monotonic()))
        log.info("LEDs: fill %s", colour)

    def light_level(self, level: int, rgb: Rgb) -> None:
        colour = tuple(rgb)
        self._show(_Showing("light_level", level, lambda t: self.layout.paint(OFF, level, colour),
                            False, time.monotonic()))

    def off(self) -> None:
        self.set_pattern("off")

    def set_brightness(self, brightness: float) -> None:
        self.brightness = min(1.0, max(0.0, float(brightness)))
        self._changed()

    def status(self) -> dict:
        return {"enabled": self.enabled, "reachable": self.reachable, "since": self.since,
                "pattern": self._showing.name, "level": self._showing.level,
                "brightness": self.brightness}

    def _show(self, showing: _Showing) -> None:
        self._showing = showing
        self._changed()

    def _changed(self) -> None:  # the real one wakes its frame loop
        pass


# -- the real one --------------------------------------------------------------------

class ArtnetLeds(_Base):
    keepalive_s = KEEPALIVE_S  # class attribute so tests can shorten it
    health_timeout_s = 3.0

    def __init__(self, config: Config, health_url: str | None = None) -> None:
        super().__init__(config)
        self.config = config.hardware.wled
        self.health_url = health_url or f"http://{self.config.ip_address}/json/info"
        self.frames_sent = 0  # frames that left the socket
        self._universes = math.ceil(self.layout.pixel_count / PIXELS_PER_UNIVERSE)
        self._socket: socket.socket | None = None
        self._sequence = 0
        self._send_failing = False
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first, last = self.config.universe, self.config.universe + self._universes - 1
        log.info("ArtNet to %s:%d, universe%s, %d pixels", self.config.ip_address,
                 self.config.port, f" {first}" if first == last else f"s {first}-{last}",
                 self.layout.pixel_count)
        self._tasks = [asyncio.create_task(self._frame_loop(), name="led-frames"),
                       asyncio.create_task(self._health_loop(), name="led-health")]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sock, self._socket = self._socket, None
        if sock is not None:
            self._send(self.layout.off(), sock)  # a clean stop leaves the strip dark
            sock.close()

    def set_pattern(self, pattern: str, level: int | None = None) -> None:
        current = self._showing
        if (current.then == pattern and level is None
                and time.monotonic() - current.started < current.ends_after_s):
            return  # the fade ends in this pattern anyway: let it finish (completing -> idle)
        super().set_pattern(pattern, level)

    def _changed(self) -> None:
        self._wake.set()

    # -- sending -----------------------------------------------------------------

    def _send(self, frame: Frame, sock: socket.socket | None = None) -> None:
        """One frame as one ArtDMX packet per universe. A failing network is logged on the first
        failure and on the first success after it, never per frame; frames are simply lost."""
        sock = sock or self._socket
        if sock is None:
            return
        data = bytes(channel for rgb in Layout.scaled(frame, self.brightness) for channel in rgb)
        self._sequence = self._sequence % 255 + 1  # 1..255, never 0
        step = PIXELS_PER_UNIVERSE * 3
        target = (self.config.ip_address, self.config.port)
        try:
            for u in range(self._universes):
                packet = artdmx_packet(self.config.universe + u, self._sequence,
                                       data[u * step:(u + 1) * step])
                sock.sendto(packet, target)
        except OSError as exc:
            if not self._send_failing:
                self._send_failing = True
                log.warning("ArtNet to %s:%d failing: %s", *target, exc)
            return
        if self._send_failing:
            self._send_failing = False
            log.info("ArtNet to %s:%d works again", *target)
        self.frames_sent += 1

    async def _frame_loop(self) -> None:
        """Animated: a frame every 1/fps. Steady: one frame on change, then keepalives."""
        while True:
            showing = self._showing
            t = time.monotonic() - showing.started
            if showing.ends_after_s is not None and t >= showing.ends_after_s:
                self._showing = _showing(self.layout, showing.then, None)
                continue
            try:
                self._send(showing.render(t))
            except Exception:
                log.exception("LED pattern %s failed; strip off", showing.name)
                self._showing = _showing(self.layout, "off", None)
                continue
            self._wake.clear()
            wait = 1 / self.config.fps if showing.animated else self.keepalive_s
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await self._wake.wait()

    # -- reachability --------------------------------------------------------------

    async def _health_loop(self) -> None:
        async with httpx.AsyncClient(timeout=self.health_timeout_s) as client:
            while True:
                try:
                    response = await client.get(self.health_url)
                    ok = response.is_success
                    error = None if ok else f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    ok, error = False, f"{type(exc).__name__}: {exc or 'no detail'}"
                if ok != self.reachable:
                    if not (self.reachable is None and ok):
                        self.since = local_time()
                    self.reachable = ok
                    log.log(logging.INFO if ok else logging.WARNING, "WLED at %s %s%s",
                            self.config.ip_address, "reachable" if ok else "unreachable",
                            "" if ok else f": {error}")
                    try:
                        self.on_reachability(ok)
                    except Exception:
                        log.exception("on_reachability failed")
                await asyncio.sleep(self.config.health_poll_s)


# -- the mock, for mock mode and tests -------------------------------------------------

class MockLeds(_Base):
    """Renders every pattern once (at t=0) into `frames`; records every call; always reachable."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.reachable = True
        self.calls: list[tuple] = []  # (monotonic time, method, args...)
        self.frames: list[Frame] = []  # brightness applied, newest last

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    @property
    def frame(self) -> Frame | None:
        return self.frames[-1] if self.frames else None

    def set_pattern(self, pattern: str, level: int | None = None) -> None:
        self.calls.append((time.monotonic(), "set_pattern", pattern, level))
        super().set_pattern(pattern, level)

    def fill(self, rgb: Rgb) -> None:
        self.calls.append((time.monotonic(), "fill", tuple(rgb)))
        super().fill(rgb)

    def light_level(self, level: int, rgb: Rgb) -> None:
        self.calls.append((time.monotonic(), "light_level", level, tuple(rgb)))
        super().light_level(level, rgb)

    def _show(self, showing: _Showing) -> None:
        log.info("mock LEDs: %s%s", showing.name,
                 "" if showing.level is None else f" level {showing.level}")
        super()._show(showing)

    def _changed(self) -> None:
        self.frames.append(Layout.scaled(self._showing.render(0.0), self.brightness))
