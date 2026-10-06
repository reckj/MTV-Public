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
    assert config.purchase_server.permission_path == "/api/vending/permission"
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


def test_purchase_server_section_loads(tmp_path):
    ps = load_config(DEFAULT_PATH).purchase_server
    assert ps.base_url == "https://monitoni.zhdk.ch" and ps.token == ""
    assert (ps.permission_path, ps.complete_path, ps.close_path) == (
        "/api/vending/permission", "/api/vending/complete", "/api/vending/close")
    assert ps.poll_interval_s == 1.0 and ps.timeout_s == 5.0
    assert load_config(DEFAULT_PATH).vending.timings.relock_delay_s == 0.5
    data = default_data()
    data["purchase_server"]["check_path"] = "/api/purchase/check"  # Milestone 3 key, gone
    with pytest.raises(ConfigError, match=r"purchase_server\.check_path: Extra inputs"):
        load_config(write_yaml(tmp_path / "default.yaml", data))


def test_outbox_backoff_loads_and_is_checked(tmp_path):
    assert load_config(DEFAULT_PATH).purchase_server.outbox_backoff_s == [1, 2, 5, 15, 60]
    for bad in ([], [1, 0, 5]):
        data = default_data()
        data["purchase_server"]["outbox_backoff_s"] = bad
        with pytest.raises(ConfigError, match=r"purchase_server\.outbox_backoff_s"):
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


# -- Milestone 5: LEDs and audio -------------------------------------------------------

def test_milestone_5_feedback_sections_load():
    config = load_config(DEFAULT_PATH)
    wled, audio, led = config.hardware.wled, config.hardware.audio, config.led
    assert (wled.port, wled.universe, wled.fps, wled.health_poll_s, wled.enabled) == (
        6454, 0, 30, 30.0, True)
    assert audio.volume == 0.7 and audio.enabled and str(audio.dir) == "assets/sounds"
    assert led.brightness == 0.6
    assert led.zones == [[12 * i, 12 * i + 11] for i in range(10)]
    assert led.colours.idle == [60, 40, 20] and led.colours.alarm == [239, 90, 106]


ZONES = [[12 * i, 12 * i + 11] for i in range(10)]


@pytest.mark.parametrize("zones,message", [
    (ZONES[:9], "led.zones: 9 zones listed for vending.levels = 10"),
    (ZONES[:9] + [[108, 300]],
     "led.zones.9: [108, 300] outside 0..299 (hardware.wled.pixel_count)"),
    (ZONES[:9] + [[119, 108]], "led.zones.9: [119, 108] ends before it starts"),
    (ZONES[:9] + [[95, 119]], "led.zones.9: [95, 119] overlaps led.zones.7 [84, 95]"),
    (ZONES[:9] + [[108, 119, 5]], "led.zones.9: List should have at most 2 items"),
    (ZONES[:9] + [[-1, 11]], "led.zones.9.0: Input should be greater than or equal to 0"),
], ids=["too-few", "past-the-end", "reversed", "overlap", "three-numbers", "negative"])
def test_zone_rules_name_the_key(tmp_path, zones, message):
    data = default_data()
    data["led"]["zones"] = zones
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write_yaml(tmp_path / "default.yaml", data))


@pytest.mark.parametrize("path,value", [
    ("led.brightness", 1.5),
    ("led.colours.open", [255, 255]),
    ("led.colours.alarm", [256, 0, 0]),
    ("hardware.wled.fps", 0),
    ("hardware.wled.health_poll_s", 0),
    ("hardware.wled.port", 70000),
    ("hardware.audio.volume", -0.1),
], ids=["brightness", "two-channels", "channel-256", "fps-0", "poll-0", "port", "volume"])
def test_feedback_values_are_checked(tmp_path, path, value):
    data = default_data()
    *parents, key = path.split(".")
    section = data
    for part in parents:
        section = section[part]
    section[key] = value
    with pytest.raises(ConfigError, match=re.escape(path)):  # a list item adds its index
        load_config(write_yaml(tmp_path / "default.yaml", data))


# -- Milestone 6: the settings area ------------------------------------------------------

def test_settings_section_loads():
    config = load_config(DEFAULT_PATH)
    assert config.settings.pin == "0000" and config.vending.timings.settings_timeout_s == 300.0
    assert not hasattr(config.system, "maintenance_mode") and not hasattr(config.qr, "dir")


@pytest.mark.parametrize("pin", ["123", "123456789", "12a4", 1234, ""],
                         ids=["short", "long", "letters", "number", "empty"])
def test_pin_must_be_4_to_8_digits_as_a_string(tmp_path, pin):
    data = default_data()
    data["settings"]["pin"] = pin
    with pytest.raises(ConfigError, match=r"settings\.pin: "):
        load_config(write_yaml(tmp_path / "default.yaml", data))


@pytest.mark.parametrize("section,key", [("system", "maintenance_mode"), ("qr", "dir")])
def test_deleted_keys_are_refused(tmp_path, section, key):
    local = write_yaml(tmp_path / "local.yaml", {section: {key: True}})
    with pytest.raises(ConfigError, match=re.escape(f"{section}.{key}: Extra inputs")):
        load_config(DEFAULT_PATH, local)
