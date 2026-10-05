"""Mock hardware for development on a laptop. Logs every call, does no I/O."""

import logging

log = logging.getLogger(__name__)


class MockHardware:
    def __init__(self) -> None:
        self._running = False

    async def start(self) -> None:
        log.info("mock hardware: start")
        self._running = True

    async def stop(self) -> None:
        log.info("mock hardware: stop")
        self._running = False

    def status(self) -> dict:
        return {"mode": "mock", "running": self._running}
