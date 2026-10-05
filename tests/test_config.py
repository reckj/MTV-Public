import re
from pathlib import Path

import pytest
import yaml

from monitoni.config import ConfigError, load_config

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def default_data() -> dict:
    return yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))


def test_default_config_loads():
    config = load_config(DEFAULT_PATH)
    assert config.system.machine_id == "VM001"
    assert config.hardware.mode == "real"
    assert config.hardware.relay_levels.max_channels == 30
    assert config.web.host == "127.0.0.1"


def test_local_overlay_merges_nested_keys(tmp_path):
    local = write_yaml(tmp_path / "local.yaml", {
        "system": {"machine_id": "VM042"},
        "hardware": {"relay_core": {"host": "10.0.0.5"}},
    })
    config = load_config(DEFAULT_PATH, local)
    assert config.system.machine_id == "VM042"
    assert config.hardware.relay_core.host == "10.0.0.5"
    # untouched siblings keep their defaults
    assert config.hardware.relay_core.port == 502
    assert config.system.name == "MoniToni Vending Machine"


def test_missing_local_file_is_fine(tmp_path):
    config = load_config(DEFAULT_PATH, tmp_path / "does-not-exist.yaml")
    assert config.system.machine_id == "VM001"


def test_missing_default_file_names_path(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing)


def test_missing_key_is_named(tmp_path):
    data = default_data()
    del data["web"]["port"]
    with pytest.raises(ConfigError, match=r"web\.port: Field required"):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_bad_type_is_named(tmp_path):
    data = default_data()
    data["hardware"]["relay_core"]["port"] = "five-oh-two"
    with pytest.raises(ConfigError, match=r"hardware\.relay_core\.port: "):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_bad_mode_is_named(tmp_path):
    data = default_data()
    data["hardware"]["mode"] = "simulated"
    with pytest.raises(ConfigError, match=r"hardware\.mode: "):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_unknown_key_is_named(tmp_path):
    local = write_yaml(tmp_path / "local.yaml", {"hardware": {"gpio": {"pin": 5}}})
    with pytest.raises(ConfigError, match=r"hardware\.gpio: Extra inputs are not permitted"):
        load_config(DEFAULT_PATH, local)


def test_milestone_1_sections_load():
    config = load_config(DEFAULT_PATH)
    assert config.vending.levels == 10
    assert config.vending.timings.door_alarm_delay_s == 10.0
    assert config.purchase_server.check_path == "/api/purchase/check"
    assert str(config.qr.dir) == "data/qr"
    assert str(config.database.path) == "data/monitoni.db"


def test_bad_timing_type_is_named(tmp_path):
    data = default_data()
    data["vending"]["timings"]["sleep_timeout_s"] = "soon"
    with pytest.raises(ConfigError, match=r"vending\.timings\.sleep_timeout_s: "):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_levels_must_be_positive(tmp_path):
    data = default_data()
    data["vending"]["levels"] = 0
    with pytest.raises(ConfigError, match=r"vending\.levels: "):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_milestone_2_hardware_sections_load():
    hw = load_config(DEFAULT_PATH).hardware
    assert hw.door_locks.channels == list(range(1, 11))
    assert hw.door_sensor.di_active == "low" and hw.door_sensor.debounce_count == 2
    assert hw.motor.motor_channel == 1 and hw.motor.spindle_channel == 2
    assert hw.motor.max_run_s == 10.0


@pytest.mark.parametrize("section,key,value", [
    ("door_locks", "channels", [1, 2, 3]),
    ("door_locks", "channels", [1, 2, 3, 4, 5, 6, 7, 8, 9, 31]),
    ("door_locks", "channels", [1, 2, 3, 4, 5, 6, 7, 8, 9, 9]),
    ("motor", "spindle_channel", 1),
    ("motor", "motor_channel", 9),
    ("motor", "max_run_s", 0),
    ("door_sensor", "di_active", "middle"),
    ("door_sensor", "di_index", 8),
    ("door_sensor", "debounce_count", 0),
], ids=["too-few-channels", "channel-31", "duplicate-channel", "same-channel",
        "motor-channel-9", "max-run-0", "di-active", "di-index-8", "debounce-0"])
def test_hardware_rules_name_the_key(tmp_path, section, key, value):
    data = default_data()
    data["hardware"][section][key] = value
    with pytest.raises(ConfigError, match=re.escape(f"hardware.{section}.{key}: ")):
        load_config(write_yaml(tmp_path / "default.yaml", data))
