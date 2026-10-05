from pathlib import Path

import pytest

from monitoni.config import Config, load_config

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
        for key in ("sleep_timeout_s", "purchase_timeout_s",
                    "door_unlock_timeout_s", "door_alarm_delay_s"):
            setattr(config.vending.timings, key, timings.get(key, LONG))
        return config

    return _make
