"""Load config/default.yaml, overlay config/local.yaml, validate with pydantic.

Installation config only. The three values a user changes on the machine (out of order, LED
brightness, audio volume) live in data/runtime.json, see monitoni/runtime.py.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Rgb = Annotated[list[Annotated[int, Field(ge=0, le=255)]], Field(min_length=3, max_length=3)]
PixelRange = Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=2, max_length=2)]


class ConfigError(Exception):
    """Raised when the configuration is missing, unreadable or invalid."""


class _Strict(BaseModel):
    """Unknown keys are errors, so a typo in local.yaml fails loudly."""

    model_config = ConfigDict(extra="forbid")


class SystemConfig(_Strict):
    name: str
    machine_id: str
    maintenance_message: str  # shown on the out-of-order screen while the runtime switch is on


class RelayModuleConfig(_Strict):
    """A Waveshare Modbus relay module reached over TCP (transparent mode)."""

    host: str
    port: int
    slave_address: int
    timeout: float
    max_channels: int


class WledConfig(_Strict):
    """The Gledopto controller running WLED: ArtNet in over UDP, `/json/info` over HTTP."""

    ip_address: str
    port: int = Field(ge=1, le=65535)  # ArtNet UDP port
    universe: int = Field(ge=0)  # the first universe; 170 pixels each, consecutive after that
    fps: int = Field(ge=1)
    pixel_count: int = Field(ge=1)
    health_poll_s: float = Field(gt=0)  # GET /json/info this often; ArtNet itself says nothing back
    enabled: bool


class DoorLocksConfig(_Strict):
    """relay_levels channel per level, index = level - 1. Relay ON = unlocked."""

    channels: list[int]


class DoorSensorConfig(_Strict):
    """Door sensor is a digital input on relay_core, read over Modbus (FC02)."""

    di_index: int = Field(ge=0)
    di_active: Literal["low", "high"]  # which DI level means "door open"
    poll_interval_ms: int = Field(ge=1)
    debounce_count: int = Field(ge=1)


class MotorConfig(_Strict):
    """Motor and spindle lock relays on relay_core, and the hold-to-turn timings."""

    motor_channel: int
    spindle_channel: int
    spindle_pre_delay_ms: int = Field(ge=0)
    spin_after_release_ms: int = Field(ge=0)
    spindle_post_delay_ms: int = Field(ge=0)
    max_run_s: float = Field(gt=0)


class AudioConfig(_Strict):
    volume: float = Field(ge=0, le=1)
    enabled: bool
    dir: Path  # holds success.wav, alarm.wav, error.wav


class HardwareConfig(_Strict):
    mode: Literal["mock", "real"]
    relay_core: RelayModuleConfig
    relay_levels: RelayModuleConfig
    door_locks: DoorLocksConfig
    door_sensor: DoorSensorConfig
    motor: MotorConfig
    wled: WledConfig
    audio: AudioConfig


class LedColoursConfig(_Strict):
    idle: Rgb
    selected: Rgb
    unlocked: Rgb
    open: Rgb
    alarm: Rgb
    fault: Rgb


class LedConfig(_Strict):
    """What the strip shows. Zones are per machine (local.yaml): one [first, last] pixel range per
    level, index = level - 1, checked against vending.levels and hardware.wled.pixel_count."""

    brightness: float = Field(ge=0, le=1)  # scales every frame
    zones: list[PixelRange]
    colours: LedColoursConfig


class WebConfig(_Strict):
    host: str
    port: int


class TimingsConfig(_Strict):
    sleep_timeout_s: float
    purchase_timeout_s: float
    door_unlock_timeout_s: float
    door_alarm_delay_s: float
    relock_delay_s: float = Field(ge=0)  # the lock pin drops back this long after the door opened
    settings_timeout_s: float = Field(gt=0)  # settings area: auto-exit after this long untouched


class VendingConfig(_Strict):
    levels: int = Field(ge=1)
    timings: TimingsConfig


class PurchaseServerConfig(_Strict):
    """The Monitoni server. `token` identifies this machine; it never leaves local.yaml."""

    base_url: str
    permission_path: str
    complete_path: str
    close_path: str
    token: str  # mandatory when the HTTP client is used; checked in __main__, not here
    poll_interval_s: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    # waits between report delivery attempts; the last value repeats
    outbox_backoff_s: list[Annotated[float, Field(gt=0)]] = Field(min_length=1)


class QrConfig(_Strict):
    base_url: str  # the QR for level N encodes base_url + "?level=" + N; rendered on demand


class SettingsConfig(_Strict):
    """The settings area. The PIN is checked by the daemon; the browser never holds it."""

    pin: str = Field(pattern=r"^[0-9]{4,8}$")


class DatabaseConfig(_Strict):
    path: Path


class Config(_Strict):
    system: SystemConfig
    hardware: HardwareConfig
    led: LedConfig
    web: WebConfig
    vending: VendingConfig
    purchase_server: PurchaseServerConfig
    qr: QrConfig
    settings: SettingsConfig
    database: DatabaseConfig


def load_config(default_path: Path, local_path: Path | None = None) -> Config:
    """Read default.yaml, overlay local.yaml if it exists, validate.

    Validation errors are reported as ConfigError with one line per problem,
    each naming the offending key as a dotted path.
    """
    data = _read_yaml(default_path)
    if local_path is not None and local_path.exists():
        data = _deep_merge(data, _read_yaml(local_path))
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None
    errors = _cross_checks(config)
    if errors:
        raise ConfigError("\n".join(errors))
    return config


def _cross_checks(config: Config) -> list[str]:
    """Rules that span sections; each line names the offending key."""
    hw = config.hardware
    errors = []
    channels = hw.door_locks.channels
    if len(channels) != config.vending.levels:
        errors.append(f"hardware.door_locks.channels: {len(channels)} channels listed for "
                      f"vending.levels = {config.vending.levels}")
    outside = [c for c in channels if not 1 <= c <= hw.relay_levels.max_channels]
    if outside:
        errors.append(f"hardware.door_locks.channels: {outside} outside "
                      f"1..{hw.relay_levels.max_channels} (relay_levels.max_channels)")
    if len(set(channels)) != len(channels):
        errors.append("hardware.door_locks.channels: a channel is listed twice")
    for key in ("motor_channel", "spindle_channel"):
        value = getattr(hw.motor, key)
        if not 1 <= value <= hw.relay_core.max_channels:
            errors.append(f"hardware.motor.{key}: {value} outside "
                          f"1..{hw.relay_core.max_channels} (relay_core.max_channels)")
    if hw.motor.motor_channel == hw.motor.spindle_channel:
        errors.append("hardware.motor.spindle_channel: must differ from motor_channel")
    if hw.door_sensor.di_index >= hw.relay_core.max_channels:
        errors.append(f"hardware.door_sensor.di_index: {hw.door_sensor.di_index} outside "
                      f"0..{hw.relay_core.max_channels - 1} (relay_core.max_channels)")
    errors += _zone_checks(config.led.zones, config.vending.levels, hw.wled.pixel_count)
    return errors


def _zone_checks(zones: list[list[int]], levels: int, pixel_count: int) -> list[str]:
    """One [first, last] range per level, inside the strip, none overlapping."""
    errors = []
    if len(zones) != levels:
        errors.append(f"led.zones: {len(zones)} zones listed for vending.levels = {levels}")
    for i, (start, end) in enumerate(zones):
        if start > end:
            errors.append(f"led.zones.{i}: [{start}, {end}] ends before it starts")
        elif end >= pixel_count:
            errors.append(f"led.zones.{i}: [{start}, {end}] outside 0..{pixel_count - 1} "
                          f"(hardware.wled.pixel_count)")
    for i, (start, end) in enumerate(zones):
        for j, (other_start, other_end) in enumerate(zones[:i]):
            if start <= other_end and other_start <= end:
                errors.append(f"led.zones.{i}: [{start}, {end}] overlaps led.zones.{j} "
                              f"[{other_start}, {other_end}]")
    return errors


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
