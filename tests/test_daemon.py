"""Start/stop failure safety: hardware is always stopped, bind errors exit cleanly."""

import asyncio
import logging
from pathlib import Path

import pytest

from monitoni.__main__ import run
from monitoni.config import load_config
from monitoni.daemon import Daemon

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


class StubHardware:
    """Records start/stop calls, nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    def status(self) -> dict:
        return {}


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


def mock_config(port: int):
    config = load_config(DEFAULT_PATH)
    config.hardware.mode = "mock"
    config.web.port = port
    return config


async def test_start_stops_hardware_when_bind_fails(occupied_port):
    hardware = StubHardware()
    daemon = Daemon(mock_config(occupied_port), hardware)
    with pytest.raises(OSError):
        await daemon.start()
    assert hardware.calls == ["start", "stop"]
    # a later stop() must not stop hardware a second time
    await daemon.stop()
    assert hardware.calls == ["start", "stop"]


async def test_stop_stops_hardware_even_if_web_cleanup_fails():
    hardware = StubHardware()
    daemon = Daemon(mock_config(0), hardware)
    await daemon.start()
    daemon._runner = BrokenRunner()
    with pytest.raises(RuntimeError, match="web cleanup failed"):
        await daemon.stop()
    assert hardware.calls == ["start", "stop"]


async def test_run_returns_1_on_bind_error(occupied_port, caplog):
    hardware = StubHardware()
    with caplog.at_level(logging.ERROR, logger="monitoni"):
        code = await run(mock_config(occupied_port), hardware)
    assert code == 1
    assert hardware.calls == ["start", "stop"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith(f"cannot bind http://127.0.0.1:{occupied_port}: ")
    assert errors[0].exc_info is None
