"""A fake ArtNet receiver standing in for the WLED controller.

Keeps the last ArtDMX frame per universe and answers GET /json/info over HTTP on the same port
number (TCP), so the daemon's health poll has something to ask. Used by the tests and, standalone,
to watch the colours on a laptop:

  python -m tests.fake_artnet --port 6454 --zones 10x12

prints one line whenever a zone's colour changes, e.g. `14:31:05 zone 3 -> (233,162,59)`.
Animated patterns (breathing, flashing, the fade) print a line per frame.

ArtDMX packet, as monitoni.leds.artdmx_packet builds it: "Art-Net\\0", opcode 0x5000 low byte
first, protocol version 14 high byte first, sequence, physical, universe low byte first, length
high byte first, then `length` channel bytes: three per pixel, R G B.
"""

import argparse
import asyncio
import logging
import time
from collections.abc import Callable

from aiohttp import web

log = logging.getLogger(__name__)

PIXELS_PER_UNIVERSE = 170
Rgb = tuple[int, int, int]


class FakeArtnet(asyncio.DatagramProtocol):
    def __init__(self, zones: list[tuple[int, int]], first_universe: int = 0) -> None:
        self.zones = zones  # index = level - 1
        self.first_universe = first_universe
        self.frames: dict[int, bytes] = {}  # universe -> channel data of the last packet
        self.frames_received = 0
        self.last_frame_at: float | None = None
        self.on_frame: Callable[[], None] = lambda: None  # the standalone printer hooks in here
        self._transport: asyncio.DatagramTransport | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        self.http_up = True  # False: /json/info answers 503 (the controller is "down")

    @property
    def port(self) -> int:
        return self._port

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self,
                                                                 local_addr=(host, port))
        self._port = self._transport.get_extra_info("sockname")[1]
        app = web.Application()
        app.router.add_get("/json/info", self._json_info)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, self._port).start()

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- UDP in --------------------------------------------------------------------

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 18 or data[:8] != b"Art-Net\0" or data[8] | data[9] << 8 != 0x5000:
            return
        universe = data[14] | data[15] << 8
        length = data[16] << 8 | data[17]
        self.frames[universe] = data[18:18 + length]
        self.frames_received += 1
        self.last_frame_at = time.monotonic()
        self.on_frame()

    def pixel(self, index: int) -> Rgb | None:
        """Colour of one pixel from the last frame of its universe; None if nothing arrived."""
        universe = self.first_universe + index // PIXELS_PER_UNIVERSE
        offset = (index % PIXELS_PER_UNIVERSE) * 3
        data = self.frames.get(universe)
        if data is None or len(data) < offset + 3:
            return None
        return tuple(data[offset:offset + 3])

    def zone_colour(self, level: int) -> Rgb | None:
        """Colour of the zone's first pixel (every pixel of a zone is the same colour)."""
        start, _ = self.zones[level - 1]
        return self.pixel(start)

    def zone_colours(self) -> list[Rgb | None]:
        return [self.zone_colour(level) for level in range(1, len(self.zones) + 1)]

    # -- HTTP: what the daemon's health poll asks -----------------------------------------

    async def _json_info(self, request: web.Request) -> web.Response:
        if not self.http_up:
            return web.Response(status=503, text="down")
        return web.json_response({"ver": "fake", "name": "fake WLED", "arch": "test",
                                  "leds": {"count": sum(b - a + 1 for a, b in self.zones)}})


def parse_zones(spec: str) -> list[tuple[int, int]]:
    """'10x12' -> ten zones of twelve pixels, back to back from pixel 0."""
    count, size = (int(n) for n in spec.lower().split("x"))
    return [(i * size, i * size + size - 1) for i in range(count)]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fake ArtNet receiver (WLED stand-in)")
    parser.add_argument("--port", type=int, default=6454)
    parser.add_argument("--zones", default="10x12", help="COUNTxSIZE, e.g. 10x12 (default)")
    parser.add_argument("--universe", type=int, default=0, help="first universe (default 0)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    zones = parse_zones(args.zones)

    async def run() -> None:
        fake = FakeArtnet(zones, args.universe)
        shown: list[Rgb | None] = [None] * len(zones)

        def report() -> None:
            now = time.strftime("%H:%M:%S")
            for level, colour in enumerate(fake.zone_colours(), start=1):
                if colour != shown[level - 1]:
                    shown[level - 1] = colour
                    r, g, b = colour
                    log.info("%s zone %d -> (%d,%d,%d)", now, level, r, g, b)

        fake.on_frame = report
        await fake.start(port=args.port)
        log.info("fake ArtNet: %d zones over %d pixels, universe %d, UDP and HTTP on 127.0.0.1:%d",
                 len(zones), zones[-1][1] + 1, args.universe, fake.port)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
