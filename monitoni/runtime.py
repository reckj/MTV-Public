"""data/runtime.json: the three switches a user changes on the machine, owned by the daemon.

{"out_of_order": bool, "brightness": 0..1, "volume": 0..1}. Read once at start (`load`); the
values for brightness and volume win over the YAML (`apply`); written atomically (tmp file +
os.replace) on every change (`save`). A missing file is normal, a malformed file or value is
logged and the YAML values stand. Deleting the file resets all three.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from monitoni.config import Config

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    path: Path | None = None  # None: not persisted (tests)
    out_of_order: bool = False
    brightness: float | None = None  # None: the config value
    volume: float | None = None

    @classmethod
    def load(cls, path: Path | None, config: Config) -> "Runtime":
        runtime = cls(path, False, config.led.brightness, config.hardware.audio.volume)
        if path is None or not path.exists():
            return runtime
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
        except (OSError, ValueError) as exc:
            log.warning("%s ignored: %s", path, exc)
            return runtime
        if "out_of_order" in data:
            if isinstance(data["out_of_order"], bool):
                runtime.out_of_order = data["out_of_order"]
            else:
                log.warning("%s: out_of_order must be true or false, not %r", path,
                            data["out_of_order"])
        for key in ("brightness", "volume"):
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
                log.warning("%s: %s must be a number in 0..1, not %r; using the config value",
                            path, key, value)
                continue
            setattr(runtime, key, float(value))
        log.info("%s: out_of_order=%s brightness=%s volume=%s", path, runtime.out_of_order,
                 runtime.brightness, runtime.volume)
        return runtime

    def apply(self, config: Config) -> None:
        """Brightness and volume from the file win over the YAML."""
        if self.brightness is not None:
            config.led.brightness = self.brightness
        if self.volume is not None:
            config.hardware.audio.volume = self.volume

    def save(self) -> None:
        """Write the whole file atomically; a failure is logged, the values stay in memory."""
        if self.path is None:
            return
        data = {"out_of_order": self.out_of_order, "brightness": self.brightness,
                "volume": self.volume}
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("cannot write %s: %s", self.path, exc)
