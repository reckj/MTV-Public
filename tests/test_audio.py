"""Sounds: the mock's record, pygame with SDL's dummy driver, and the no-device path."""

import asyncio
import logging

import pytest

from monitoni.audio import MockAudio, PygameAudio

SOUNDS_DIR = "assets/sounds"


def pygame_params(path: str) -> tuple[int, int, int]:
    """(channels, bytes per sample, rate) of a wav file."""
    import wave

    with wave.open(path) as wav:
        return wav.getnchannels(), wav.getsampwidth(), wav.getframerate()


def test_mock_records_calls_and_reports_what_plays(monkeypatch):
    audio = MockAudio(volume=0.7)
    assert audio.status() == {"enabled": True, "available": True, "volume": 0.7, "playing": None}
    audio.play("success")
    assert audio.status()["playing"] == "success"
    audio.play("alarm", loop=True)
    audio.set_volume(2)
    assert audio.status() == {"enabled": True, "available": True, "volume": 1.0,
                              "playing": "alarm"}
    audio.stop_playing()
    assert audio.status()["playing"] is None
    assert [c[1:] for c in audio.calls] == [("play", "success", False), ("play", "alarm", True),
                                            ("set_volume", 1.0), ("stop_playing",)]
    with pytest.raises(ValueError, match="unknown sound 'ding'"):
        audio.play("ding")


def test_mock_carries_the_config_flag():
    assert MockAudio(enabled=False).status()["enabled"] is False


def test_mock_one_shot_ends_by_itself(monkeypatch):
    import monitoni.audio as audio_module

    monkeypatch.setattr(audio_module, "MOCK_SOUND_S", 0.0)
    audio = MockAudio()
    audio.play("error")
    assert audio.status()["playing"] is None


@pytest.fixture
def audio_config(make_config):
    config = make_config().hardware.audio
    config.dir = type(config.dir)(SOUNDS_DIR)
    return config


async def test_pygame_plays_and_loops_without_a_device(audio_config, monkeypatch):
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    audio = PygameAudio(audio_config)
    await audio.start()
    try:
        assert audio.status() == {"enabled": True, "available": True, "volume": 0.7,
                                  "playing": None}
        audio.play("alarm", loop=True)
        assert audio.status()["playing"] == "alarm"
        await asyncio.sleep(1.2)  # alarm.wav is one second long: a loop is still going
        assert pygame_params("assets/sounds/success.wav") == (2, 2, 44100)  # like the other two
        assert audio.status()["playing"] == "alarm"
        audio.stop_playing()
        assert audio.status()["playing"] is None
        audio.play("success")
        assert audio.status()["playing"] == "success"
        audio.set_volume(0.2)
        assert audio.status()["volume"] == 0.2
        for name in ("error", "alarm"):
            audio.play(name)
        with pytest.raises(ValueError, match="unknown sound"):
            audio.play("fanfare")
    finally:
        await audio.stop()
    assert audio.status()["available"] is False


async def test_pygame_without_an_audio_device_is_a_no_op(audio_config, monkeypatch, caplog):
    monkeypatch.setenv("SDL_AUDIODRIVER", "no-such-driver")
    audio = PygameAudio(audio_config)
    with caplog.at_level(logging.WARNING, logger="monitoni.audio"):
        await audio.start()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "audio unavailable" in warnings[0].getMessage()
    assert audio.status() == {"enabled": True, "available": False, "volume": 0.7,
                              "playing": None}
    audio.play("alarm", loop=True)  # all no-ops, nothing raises
    audio.stop_playing()
    audio.set_volume(0.5)
    assert audio.status() == {"enabled": True, "available": False, "volume": 0.5,
                              "playing": None}
    with pytest.raises(ValueError):  # a wrong name is a bug even without a device
        audio.play("fanfare")
    await audio.stop()


async def test_pygame_with_a_missing_sound_file_is_unavailable(audio_config, tmp_path,
                                                                monkeypatch, caplog):
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    audio_config.dir = tmp_path
    audio = PygameAudio(audio_config)
    with caplog.at_level(logging.WARNING, logger="monitoni.audio"):
        await audio.start()
    assert audio.status()["available"] is False
    assert "audio unavailable" in caplog.text and "success.wav" in caplog.text
