"""aiohttp routes. Status goes out over the WebSocket, commands come in over POST."""

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import qrcode
from aiohttp import web
from qrcode.image.svg import SvgPathImage

from monitoni.eventlog import FILTERS
from monitoni.flow import Event, IllegalTransition, State
from monitoni.hardware.base import HardwareError
from monitoni.web.common import DAEMON, error
from monitoni.web.settings import api_settings

if TYPE_CHECKING:
    from monitoni.daemon import Daemon

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_S = 1.0
MAX_EVENTS = 1000

WEBSOCKETS = web.AppKey("websockets", set)
BROADCASTER = web.AppKey("broadcaster", asyncio.Task)


def create_app(daemon: "Daemon") -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app[WEBSOCKETS] = set()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/command", api_command)
    app.router.add_post("/api/settings/{name}", api_settings)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/events/summary", api_events_summary)
    app.router.add_get(r"/api/qr/{level:\d+}.svg", api_qr)
    app.router.add_get(r"/api/qr/{level:\d+}.json", api_qr_json)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(start_broadcaster)
    app.on_shutdown.append(stop_broadcaster)
    app.on_shutdown.append(close_websockets)
    return app


# -- pages and status ----------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_status(request: web.Request) -> web.Response:
    return web.json_response(request.app[DAEMON].status())


async def api_events(request: web.Request) -> web.Response:
    """?limit=50&before=<id>&filter=all|vends|hardware|network — newest first."""
    try:
        limit = int(request.query.get("limit", "50"))
        before = request.query.get("before")
        before = None if before is None else int(before)
    except ValueError:
        return error(400, "limit and before must be integers")
    filter_ = request.query.get("filter", "all")
    if filter_ not in FILTERS:
        return error(400, f"filter must be one of {', '.join(FILTERS)}")
    limit = max(1, min(limit, MAX_EVENTS))
    return web.json_response(await request.app[DAEMON].events.query(limit, before, filter_))


async def api_events_summary(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    return web.json_response({**await daemon.events.summary(),
                              "outbox_pending": daemon.outbox.pending_count})


# -- commands ------------------------------------------------------------------

async def api_command(request: web.Request) -> web.Response:
    daemon = request.app[DAEMON]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return error(400, "body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("command"), str):
        return error(400, 'body must be an object with a "command" string')
    command = body["command"]

    try:
        if command == "select_level":
            level = body.get("level")
            if not isinstance(level, int) or isinstance(level, bool):
                return error(400, "level must be an integer")
            await daemon.flow.dispatch(Event.SELECT_LEVEL, level=level)
        elif command == "cancel":
            await daemon.flow.dispatch(Event.CANCEL)
        elif command == "touch":
            await daemon.flow.dispatch(Event.TOUCH)
        elif command in ("motor_press", "motor_release"):
            if daemon.flow.state not in (State.IDLE, State.SETTINGS):
                return error(409, f"{command} is only allowed in idle or settings")
            if command == "motor_press":
                await daemon.flow.dispatch(Event.TOUCH)  # holding TURN is activity: no sleep
                await daemon.motor.press()
            else:
                await daemon.motor.release()
        elif command == "simulate_payment":
            if not daemon.purchase_is_mock:
                return error(403, "payments can only be simulated with the mock purchase server")
            level = daemon.flow.selected_level
            if level is None:
                return error(409, "no level selected")
            daemon.purchase.simulate_payment(level)
            await daemon.events.write("dev", daemon.flow.state.value, level=level, details=body)
        elif command == "simulate_door":
            if daemon.config.hardware.mode != "mock":
                return error(403, "the door can only be simulated with mock hardware")
            open_ = body.get("open")
            if not isinstance(open_, bool):
                return error(400, "open must be true or false")
            daemon.hardware.simulate_door(open_)
            await daemon.events.write("dev", daemon.flow.state.value,
                                      level=daemon.flow.selected_level, details=body)
        elif command == "simulate_server":
            if not daemon.purchase_is_mock:
                return error(403, "reachability can only be simulated with the mock purchase "
                                  "server")
            reachable = body.get("reachable")
            if not isinstance(reachable, bool):
                return error(400, "reachable must be true or false")
            daemon.purchase.simulate_reachable(reachable)
            await daemon.events.write("dev", daemon.flow.state.value, details=body)
        else:
            return error(400, f"unknown command {command!r}")
    except ValueError as exc:
        return error(400, str(exc))
    except IllegalTransition as exc:
        return error(409, str(exc))
    except HardwareError as exc:
        return error(503, str(exc))  # the motor has already stopped and queued the fault
    return web.json_response(daemon.status())


# -- QR codes ------------------------------------------------------------------

async def api_qr(request: web.Request) -> web.Response:
    """The level's QR code as an SVG path without a quiet zone and without a fill: the page
    inlines it, colours the modules and surrounds it with the cream plate. A pure function of
    qr.base_url and the level, rendered on every request (a few milliseconds)."""
    daemon = request.app[DAEMON]
    level = int(request.match_info["level"])
    if not 1 <= level <= daemon.config.vending.levels:
        return error(404, f"level must be 1..{daemon.config.vending.levels}")
    svg = qr_svg(qr_data(daemon.config.qr.base_url, level))
    return web.Response(text=svg, content_type="image/svg+xml")


async def api_qr_json(request: web.Request) -> web.Response:
    """{level, data}: what the QR for a level encodes, for the settings screen."""
    daemon = request.app[DAEMON]
    level = int(request.match_info["level"])
    if not 1 <= level <= daemon.config.vending.levels:
        return error(404, f"level must be 1..{daemon.config.vending.levels}")
    return web.json_response({"level": level, "data": qr_data(daemon.config.qr.base_url, level)})


def qr_data(base_url: str, level: int) -> str:
    """What the QR code for a level encodes: the old machines' pattern, which the
    existing purchase server expects. The machine id travels in the purchase check
    request, not in the QR."""
    return f"{base_url}?level={level}"


class _QrPath(SvgPathImage):
    QR_PATH_STYLE = {"fill-rule": "nonzero", "stroke": "none"}  # no fill: the page colours it


def qr_svg(data: str) -> str:
    """`<svg viewBox="0 0 N N"><path d=.../></svg>`, one unit per module, no quiet zone."""
    qr = qrcode.QRCode(border=0)
    qr.add_data(data)
    image = qr.make_image(image_factory=_QrPath)
    return image.to_string(encoding="unicode").replace(' id="qr-path"', "")  # inlined twice


# -- WebSocket: status out, nothing in ---------------------------------------

async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    request.app[WEBSOCKETS].add(ws)
    log.debug("websocket connected (%d open)", len(request.app[WEBSOCKETS]))
    try:
        await ws.send_json(request.app[DAEMON].status())
        async for _ in ws:  # drains incoming frames until the peer or we close
            pass
    except ConnectionResetError:
        pass
    finally:
        request.app[WEBSOCKETS].discard(ws)
        log.debug("websocket closed (%d open)", len(request.app[WEBSOCKETS]))
        if not request.app[WEBSOCKETS] and request.app[DAEMON].motor.active:
            await request.app[DAEMON].stop_motor("ws_closed")  # nobody is holding the button
    return ws


async def broadcast(app: web.Application) -> None:
    """Push status to every socket on each state change, and at least every HEARTBEAT_S."""
    daemon = app[DAEMON]
    while True:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(HEARTBEAT_S):
                await daemon.changed.wait()
        daemon.changed.clear()
        payload = daemon.status()
        for ws in set(app[WEBSOCKETS]):
            with contextlib.suppress(ConnectionResetError):
                await ws.send_json(payload)


async def start_broadcaster(app: web.Application) -> None:
    app[BROADCASTER] = asyncio.create_task(broadcast(app), name="ws-broadcast")


async def stop_broadcaster(app: web.Application) -> None:
    app[BROADCASTER].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app[BROADCASTER]


async def close_websockets(app: web.Application) -> None:
    for ws in set(app[WEBSOCKETS]):
        await ws.close(code=1001, message=b"daemon shutting down")
