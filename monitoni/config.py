"""Load config/default.yaml, overlay config/local.yaml, validate with pydantic."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigError(Exception):
    """Raised when the configuration is missing, unreadable or invalid."""


class _Strict(BaseModel):
    """Unknown keys are errors, so a typo in local.yaml fails loudly."""

    model_config = ConfigDict(extra="forbid")


class SystemConfig(_Strict):
    name: str
    machine_id: str
    maintenance_mode: bool
    maintenance_message: str


class RelayModuleConfig(_Strict):
    """A Waveshare Modbus relay module reached over TCP (transparent mode)."""

    host: str
    port: int
    slave_address: int
    timeout: float
    max_channels: int


class WledConfig(_Strict):
    ip_address: str
    universe: int
    fps: int
    pixel_count: int


class DoorSensorConfig(_Strict):
    """Door sensor is a digital input on relay_core, read over Modbus."""

    di_index: int
    poll_interval_ms: int


class AudioConfig(_Strict):
    volume: float


class HardwareConfig(_Strict):
    mode: Literal["mock", "real"]
    relay_core: RelayModuleConfig
    relay_levels: RelayModuleConfig
    wled: WledConfig
    door_sensor: DoorSensorConfig
    audio: AudioConfig


class WebConfig(_Strict):
    host: str
    port: int


class Config(_Strict):
    system: SystemConfig
    hardware: HardwareConfig
    web: WebConfig


def load_config(default_path: Path, local_path: Path | None = None) -> Config:
    """Read default.yaml, overlay local.yaml if it exists, validate.

    Validation errors are reported as ConfigError with one line per problem,
    each naming the offending key as a dotted path.
    """
    data = _read_yaml(default_path)
    if local_path is not None and local_path.exists():
        data = _deep_merge(data, _read_yaml(local_path))
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base with overlay applied; nested dicts merge, everything else replaces."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        key = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"{key}: {err['msg']}")
    return "\n".join(lines)
