"""The settings area's API: `POST /api/settings/<name>` with a JSON object body (may be empty).

`enter` checks the PIN and is allowed in idle and out_of_order; every other route only while
the flow is in `settings` (409 otherwise). A successful call writes one `command` row
{tool, body} (the PIN is never written) and answers with the status object; `test_server` adds
its result. Hardware rules are the usual ones: read-back, no retries, a HardwareError is 503
and a hardware fault (out_of_order), like everywhere else.
"""

import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

from monitoni.flow import DoorOpen, Event, IllegalTransition, State
from monitoni.hardware.base import HardwareError
from monitoni.purchase import PurchaseServerError
from monitoni.web.common import DAEMON, error

if TYPE_CHECKING:
    from monitoni.daemon import Daemon

log = logging.getLogger(__name__)

DEFAULT_PIN = "0000"


class WrongPin(Exception):
    pass


class Refused(Exception):
    """The request is well-formed but not possible right now (409)."""


# -- helpers ----------------------------------------------------------------------

def _bool(body: dict, key: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _fraction(body: dict, key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError(f"{key} must be a number from 0 to 1")
    return float(value)


def _level(daemon: "Daemon", body: dict) -> int:
    value = body.get("level")
    levels = daemon.config.vending.levels
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= levels:
        raise ValueError(f"level must be an integer from 1 to {levels}")
    return value


def _rgb(body: dict) -> tuple[int, int, int] | None:
    value = body.get("rgb")
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 3
            or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255
                       for c in value)):
        raise ValueError("rgb must be three integers from 0 to 255")
    return tuple(value)  # type: ignore[return-value]


# -- the routes -------------------------------------------------------------------

async def enter(daemon: "Daemon", body: dict) -> None:
    pin = body.get("pin")
    if not isinstance(pin, str):
        raise ValueError("pin must be a string")
    if not secrets.compare_digest(pin, daemon.config.settings.pin):
        raise WrongPin()
    await daemon.flow.dispatch(Event.ENTER_SETTINGS)


async def exit_(daemon: "Daemon", body: dict) -> None:
    await daemon.flow.dispatch(Event.EXIT_SETTINGS)  # DoorOpen while the sensor reads open


async def out_of_order(daemon: "Daemon", body: dict) -> None:
    """The switch takes effect on leaving settings (the flow's exit rule)."""
    daemon.runtime.out_of_order = _bool(body, "on")
    daemon.runtime.save()


async def brightness(daemon: "Daemon", body: dict) -> None:
    value = _fraction(body, "value")
    daemon.leds.set_brightness(value)
    daemon.runtime.brightness = value
    daemon.runtime.save()


async def volume(daemon: "Daemon", body: dict) -> None:
    value = _fraction(body, "value")
    daemon.audio.set_volume(value)
    daemon.runtime.volume = value
    daemon.runtime.save()


async def door(daemon: "Daemon", body: dict) -> None:
    """The second and last caller of unlock_door besides the door_unlocked hook."""
    level = _level(daemon, body)
    if _bool(body, "unlock"):
        await daemon.hardware.unlock_door(level)
    else:
        await daemon.hardware.lock_door(level)


async def lock_all(daemon: "Daemon", body: dict) -> None:
    await daemon.hardware.lock_all_doors()


async def spindle(daemon: "Daemon", body: dict) -> None:
    open_ = _bool(body, "open")
    if daemon.motor.active:
        raise Refused("the motor sequence is running; release TURN first")
    await daemon.hardware.set_spindle(open_)


async def leds(daemon: "Daemon", body: dict) -> None:
    action = body.get("action")
    if action == "off":
        daemon.leds.off()
    elif action == "fill":
        rgb = _rgb(body)
        if rgb is None:
            raise ValueError("fill needs rgb")
        daemon.leds.fill(rgb)
    elif action == "level":
        rgb = _rgb(body) or tuple(daemon.config.led.colours.open)
        daemon.leds.light_level(_level(daemon, body), rgb)
    else:
        raise ValueError("action must be off, fill or level")


async def audio(daemon: "Daemon", body: dict) -> None:
    action = body.get("action")
    if action == "play":
        sound = body.get("sound")
        if not isinstance(sound, str):
            raise ValueError("play needs a sound name")
        daemon.audio.play(sound)  # one-shot; the loop is the alarm's, not a tool's
    elif action == "stop":
        daemon.audio.stop_playing()
    else:
        raise ValueError("action must be play or stop")


async def test_server(daemon: "Daemon", body: dict) -> dict:
    """One permission request, reported as reachable or not plus the time it took. The answer
    itself is ignored: a test never unlocks anything."""
    started = time.monotonic()
    ok, message = True, None
    try:
        await daemon.purchase.permission()
    except PurchaseServerError as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # a bug in the client is still "not reachable" for the tester
        log.exception("test_server: the purchase client raised")
        ok, message = False, f"{type(exc).__name__}: {exc}"
    took_ms = round((time.monotonic() - started) * 1000)
    log.info("test_server: %s in %d ms%s", "ok" if ok else "failed", took_ms,
             "" if ok else f": {message}")
    return {"test_server": {"ok": ok, "error": message, "took_ms": took_ms}}


Handler = Callable[["Daemon", dict], Awaitable[dict | None]]
TOOLS: dict[str, Handler] = {
    "enter": enter, "exit": exit_, "out_of_order": out_of_order, "brightness": brightness,
    "volume": volume, "door": door, "lock_all": lock_all, "spindle": spindle, "leds": leds,
    "audio": audio, "test_server": test_server,
}


async def api_settings(request: web.Request) -> web.Response:
    daemon: Daemon = request.app[DAEMON]
    name = request.match_info["name"]
    handler = TOOLS.get(name)
    if handler is None:
        return error(404, f"unknown settings route {name!r}")
    if request.can_read_body:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return error(400, "body must be JSON")
        if not isinstance(body, dict):
            return error(400, "body must be an object")
    else:
        body = {}
    if name != "enter" and daemon.flow.state is not State.SETTINGS:
        return error(409, f"{name} is only allowed in state settings")

    state = daemon.flow.state.value
    safe_body = {} if name == "enter" else body  # the PIN never reaches the log
    try:
        extra = await handler(daemon, body)
    except ValueError as exc:
        return error(400, str(exc))
    except WrongPin:
        log.warning("settings: wrong PIN")
        await daemon.events.write("command", state, details={"tool": name, "accepted": False})
        return error(403, "wrong PIN")
    except (IllegalTransition, DoorOpen, Refused) as exc:
        return error(409, str(exc))
    except HardwareError as exc:
        log.error("settings %s failed: %s", name, exc)
        await daemon.events.write("command", state, level=body.get("level"),
                                  details={"tool": name, "body": safe_body, "error": str(exc)})
        await daemon.flow.dispatch(Event.HARDWARE_FAULT, error=f"settings {name}: {exc}")
        return error(503, str(exc))
    await daemon.events.write("command", state, level=body.get("level"),
                              details={"tool": name, "body": safe_body})
    daemon.changed.set()  # a tool changed something the page shows (lock, pattern, volume)
    return web.json_response({**daemon.status(), **(extra or {})})
