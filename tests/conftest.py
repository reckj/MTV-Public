from pathlib import Path

import aiohttp
import pytest

from monitoni.config import Config, load_config
from monitoni.hardware import modbus
from monitoni.purchase import HttpPurchaseServer
from tests.fake_purchase_server import FakePurchaseServer
from tests.fake_waveshare import FakeWaveshare

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
LONG = 10.0  # a timeout that never fires inside a test


@pytest.fixture
def make_config(tmp_path):
    """Default config in mock mode, data under tmp_path, every timeout long unless given."""

    def _make(**timings: float) -> Config:
        config = load_config(DEFAULT_PATH)
        config.hardware.mode = "mock"
        config.web.port = 0
        config.database.path = tmp_path / "events.db"
        config.qr.dir = tmp_path / "qr"
        config.purchase_server.poll_interval_s = 0.01
        config.purchase_server.outbox_backoff_s = [0.05, 0.1, 0.2]
        config.purchase_server.timeout_s = 0.3
        config.vending.timings.relock_delay_s = 0.05
        for key in ("sleep_timeout_s", "purchase_timeout_s",
                    "door_unlock_timeout_s", "door_alarm_delay_s"):
            setattr(config.vending.timings, key, timings.get(key, LONG))
        motor = config.hardware.motor
        motor.spindle_pre_delay_ms, motor.spin_after_release_ms = 10, 10
        motor.spindle_post_delay_ms = 5
        return config

    return _make


@pytest.fixture
async def client():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        yield session


@pytest.fixture
async def purchase_fake():
    """A fake HTTP purchase server for machine VM001."""
    fake = FakePurchaseServer()
    await fake.start()
    yield fake
    await fake.stop()


def http_purchase(config: Config, fake: FakePurchaseServer,
                  token: str | None = None) -> HttpPurchaseServer:
    """An HTTP purchase client pointed at the fake with its token (not started)."""
    config.purchase_server.base_url = fake.url
    config.purchase_server.token = fake.token if token is None else token
    return HttpPurchaseServer(config.purchase_server)


@pytest.fixture
async def fakes():
    """Two fake Waveshare modules: (core: 8 coils + 8 inputs, levels: 30 coils). Door closed."""
    core = FakeWaveshare(coils=8, inputs=8)
    levels = FakeWaveshare(coils=30, inputs=0)
    core.inputs[0] = True  # di_active is "low" in default.yaml, so high means closed
    await core.start()
    await levels.start()
    yield core, levels
    await core.stop()
    await levels.stop()


@pytest.fixture
def real_config(make_config, fakes, monkeypatch):
    """Real hardware mode pointed at the two fakes, with fast polling and reconnects."""
    monkeypatch.setattr(modbus, "RECONNECT_BACKOFF", (0.05,))
    monkeypatch.setattr(modbus, "MONITOR_INTERVAL_S", 0.02)
    core, levels = fakes
    config = make_config()
    config.hardware.mode = "real"
    for module, fake in ((config.hardware.relay_core, core),
                         (config.hardware.relay_levels, levels)):
        module.host = "127.0.0.1"
        module.port = fake.port
        module.timeout = 0.3
    config.hardware.door_sensor.poll_interval_ms = 10
    return config

