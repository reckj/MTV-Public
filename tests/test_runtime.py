"""data/runtime.json: load with fallbacks, apply to the config, atomic save."""

import json
import logging

from monitoni.runtime import Runtime


def test_missing_file_means_the_config_values(make_config, tmp_path):
    config = make_config()
    runtime = Runtime.load(tmp_path / "runtime.json", config)
    assert (runtime.out_of_order, runtime.brightness, runtime.volume) == (False, 0.6, 0.7)
    runtime.apply(config)
    assert config.led.brightness == 0.6 and config.hardware.audio.volume == 0.7


def test_file_values_win_and_bad_ones_are_ignored(make_config, tmp_path, caplog):
    config = make_config()
    path = tmp_path / "runtime.json"
    path.write_text('{"out_of_order": true, "brightness": 0.25, "volume": 1}')
    runtime = Runtime.load(path, config)
    assert (runtime.out_of_order, runtime.brightness, runtime.volume) == (True, 0.25, 1.0)
    runtime.apply(config)
    assert config.led.brightness == 0.25 and config.hardware.audio.volume == 1.0

    path.write_text('{"out_of_order": "yes", "brightness": 7, "volume": "loud", "other": 1}')
    with caplog.at_level(logging.WARNING, logger="monitoni.runtime"):
        runtime = Runtime.load(path, config)
    assert (runtime.out_of_order, runtime.brightness, runtime.volume) == (False, 0.25, 1.0)
    assert "out_of_order must be true or false, not 'yes'" in caplog.text
    assert "brightness must be a number in 0..1, not 7" in caplog.text
    assert "volume must be a number in 0..1, not 'loud'" in caplog.text

    path.write_text("not json")
    with caplog.at_level(logging.WARNING, logger="monitoni.runtime"):
        runtime = Runtime.load(path, config)
    assert runtime.brightness == 0.25 and f"{path} ignored" in caplog.text


def test_save_is_atomic_and_round_trips(make_config, tmp_path):
    config = make_config()
    path = tmp_path / "data" / "runtime.json"  # the directory does not exist yet
    runtime = Runtime.load(path, config)
    runtime.out_of_order, runtime.brightness = True, 0.3
    runtime.save()
    assert json.loads(path.read_text()) == {"out_of_order": True, "brightness": 0.3, "volume": 0.7}
    assert not path.with_suffix(".json.tmp").exists()
    again = Runtime.load(path, config)
    assert (again.out_of_order, again.brightness, again.volume) == (True, 0.3, 0.7)
    Runtime().save()  # no path: nothing happens
