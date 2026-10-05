"""Start/stop failure safety: hardware is always stopped, bind errors exit cleanly."""

import asyncio
import logging

import pytest

from monitoni.__main__ import run
from monitoni.daemon import Daemon
from monitoni.hardware.mock import MockHardware
from monitoni.purchase import MockPurchaseServer


class BrokenRunner:
    async def cleanup(self) -> None:
        raise RuntimeError("web cleanup failed")


@pytest.fixture
async def occupied_port():
    """A port on 127.0.0.1 held open by another server for the test's duration."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def lifecycle(hardware: MockHardware) -> list[str]:
    return [c for c in hardware.calls if c in ("start", "stop")]


async def test_start_stops_hardware_when_bind_fails(make_config, occupied_port):
    config = make_config()
    config.web.port = occupied_port
    hardware = MockHardware(config.vending.levels)
    daemon = Daemon(config, hardware, MockPurchaseServer())
    with pytest.raises(OSError):
        await daemon.start()
    assert lifecycle(hardware) == ["start", "stop"]
    # a later stop() must not stop hardware a second time
    await daemon.stop()
    assert lifecycle(hardware) == ["start", "stop"]


async def test_stop_stops_hardware_even_if_web_cleanup_fails(make_config):
    config = make_config()
    hardware = MockHardware(config.vending.levels)
    daemon = Daemon(config, hardware, MockPurchaseServer())
    await daemon.start()
    daemon._runner = BrokenRunner()
    with pytest.raises(RuntimeError, match="web cleanup failed"):
        await daemon.stop()
    assert lifecycle(hardware) == ["start", "stop"]


async def test_run_returns_1_on_bind_error(make_config, occupied_port, caplog):
    config = make_config()
    config.web.port = occupied_port
    hardware = MockHardware(config.vending.levels)
    with caplog.at_level(logging.ERROR, logger="monitoni"):
        code = await run(config, hardware, MockPurchaseServer())
    assert code == 1
    assert lifecycle(hardware) == ["start", "stop"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith(f"cannot bind http://127.0.0.1:{occupied_port}: ")
    assert errors[0].exc_info is None
