"""The daemon: owns hardware, config and the web server. One instance per process."""

import logging
import time

from aiohttp import web

from monitoni.config import Config
from monitoni.hardware.base import Hardware
from monitoni.web.server import create_app

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: Config, hardware: Hardware) -> None:
        self.config = config
        self.hardware = hardware
        self._started_at: float | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._started_at = time.monotonic()
        await self.hardware.start()
        self._runner = web.AppRunner(create_app(self))
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.web.host, self.config.web.port)
        await site.start()
        log.info("daemon started, machine %s, hardware %s, UI at %s",
                 self.config.system.machine_id, self.config.hardware.mode, self.url)

    async def stop(self) -> None:
        """Stop in reverse order: web server (closes WebSockets), then hardware."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        await self.hardware.stop()
        log.info("daemon stopped")

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
        return {
            "machine_id": self.config.system.machine_id,
            "hardware_mode": self.config.hardware.mode,
            "uptime_s": round(uptime, 1),
            "state": "idle",
        }
