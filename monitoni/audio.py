"""Sounds over HDMI with pygame.mixer. Not safety-relevant: nothing here reaches the flow.

Three sounds exist, as files in hardware.audio.dir: `success` (a door was unlocked), `alarm`
(looped while a door alarm is on), `error` (a hardware fault). No audio device, as on CI and
some laptops: `available` is False, logged once at WARNING, and every call is a no-op.
"""

import logging
import os
import time
from typing import Protocol

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # keeps pygame's banner out of the journal
import pygame  # noqa: E402

from monitoni.config import AudioConfig  # noqa: E402
from monitoni.stamp import local_time  # noqa: E402

log = logging.getLogger(__name__)

SOUNDS = ("success", "alarm", "error")
MOCK_SOUND_S = 1.0  # how long the mock reports a one-shot sound as playing


class Audio(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def play(self, name: str, loop: bool = False) -> None:
        """Play one of SOUNDS, looped until stop_playing() if `loop`. Raises ValueError."""
        ...

    def stop_playing(self) -> None: ...

    def set_volume(self, volume: float) -> None: ...

    def status(self) -> dict:
        """{enabled, available, since, volume, playing}"""
        ...


def _check(name: str) -> None:
    if name not in SOUNDS:
        raise ValueError(f"unknown sound {name!r}; there are {', '.join(SOUNDS)}")


# -- the real one --------------------------------------------------------------------

class PygameAudio:
    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.volume = config.volume
        self.enabled = config.enabled  # the config flag, for the settings screens
        self.available = False
        self.since: str | None = None  # when audio became unavailable; None while fine
        self._sounds: dict[str, pygame.mixer.Sound] = {}
        self._channel: pygame.mixer.Channel | None = None
        self._playing: str | None = None

    async def start(self) -> None:
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            for name in SOUNDS:
                self._sounds[name] = pygame.mixer.Sound(str(self.config.dir / f"{name}.wav"))
        except (pygame.error, OSError) as exc:
            log.warning("audio unavailable, sounds are off: %s", exc)
            self._sounds.clear()
            self.since = local_time()
            pygame.mixer.quit()
            return
        self.available = True
        self.set_volume(self.volume)
        log.info("audio: %d Hz, %d channel(s), sounds from %s",
                 pygame.mixer.get_init()[0], pygame.mixer.get_init()[2], self.config.dir)

    async def stop(self) -> None:
        if self.available:
            self.available = False
            self._sounds.clear()
            self._channel = self._playing = None
            pygame.mixer.quit()

    def play(self, name: str, loop: bool = False) -> None:
        _check(name)
        if not self.available:
            return
        self._channel = self._sounds[name].play(loops=-1 if loop else 0)
        self._playing = name
        log.info("audio: %s%s", name, " (looped)" if loop else "")

    def stop_playing(self) -> None:
        if not self.available:
            return
        pygame.mixer.stop()
        self._channel = self._playing = None

    def set_volume(self, volume: float) -> None:
        self.volume = min(1.0, max(0.0, float(volume)))
        for sound in self._sounds.values():
            sound.set_volume(self.volume)

    def status(self) -> dict:
        playing = None
        if self.available and self._channel is not None and self._channel.get_busy():
            playing = self._playing
        return {"enabled": self.enabled, "available": self.available, "since": self.since,
                "volume": self.volume, "playing": playing}


# -- the mock, for mock mode and tests -------------------------------------------------

class MockAudio:
    """Records every call; a one-shot counts as playing for MOCK_SOUND_S, a loop until stopped."""

    def __init__(self, volume: float = 0.7, enabled: bool = True) -> None:
        self.volume = volume
        self.enabled = enabled
        self.available = True
        self.calls: list[tuple] = []  # (monotonic time, method, args...)
        self._playing: tuple[str, bool, float] | None = None  # name, looped, started

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def play(self, name: str, loop: bool = False) -> None:
        _check(name)
        log.info("mock audio: play %s%s", name, " (looped)" if loop else "")
        self.calls.append((time.monotonic(), "play", name, loop))
        self._playing = (name, loop, time.monotonic())

    def stop_playing(self) -> None:
        log.info("mock audio: stop")
        self.calls.append((time.monotonic(), "stop_playing"))
        self._playing = None

    def set_volume(self, volume: float) -> None:
        self.volume = min(1.0, max(0.0, float(volume)))
        self.calls.append((time.monotonic(), "set_volume", self.volume))

    def status(self) -> dict:
        playing = None
        if self._playing is not None:
            name, loop, started = self._playing
            if loop or time.monotonic() - started < MOCK_SOUND_S:
                playing = name
        return {"enabled": self.enabled, "available": True, "since": None,
                "volume": self.volume, "playing": playing}
