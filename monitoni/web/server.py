"""aiohttp routes. Status goes out over the WebSocket, commands come in over POST."""

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import qrcode
from aiohttp import web

from monitoni.flow import Event, IllegalTransition

if TYPE_CHECKING:
    from monitoni.daemon import Daemon

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_S = 1.0
MAX_EVENTS = 1000

DAEMON = web.AppKey("daemon", object)
WEBSOCKETS = web.AppKey("websockets", set)
BROADCASTER = web.AppKey("broadcaster", asyncio.Task)


def create_app(daemon: "Daemon") -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app[WEBSOCKETS] = set()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/command", api_command)
    app.router.add_get("/api/events", api_events)
    app.router.add_get(r"/api/qr/{level:\d+}.png", api_qr)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(start_broadcaster)
    app.on_shutdown.append(stop_broadcaster)
    app.on_shutdown.append(close_websockets)
    return app


def error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


# -- pages and status ----------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_status(request: web.Request) -> web.Response:
    return web.json_response(request.app[DAEMON].status())


async def api_events(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        return error(400, "limit must be an integer")
    limit = max(1, min(limit, MAX_EVENTS))
    return web.json_response(await request.app[DAEMON].events.recent(limit))


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
        elif command in ("simulate_payment", "simulate_door"):
            if daemon.config.hardware.mode != "mock":
                return error(403, "simulate commands are only available in mock mode")
            if command == "simulate_payment":
                level = daemon.flow.selected_level
                if level is None:
                    return error(409, "no level selected")
                daemon.purchase.simulate_payment(level)
            else:
                open_ = body.get("open")
                if not isinstance(open_, bool):
                    return error(400, "open must be true or false")
                daemon.hardware.simulate_door(open_)
            await daemon.events.write("dev", daemon.flow.state.value,
                                      level=daemon.flow.selected_level, details=body)
        else:
            return error(400, f"unknown command {command!r}")
    except ValueError as exc:
        return error(400, str(exc))
    except IllegalTransition as exc:
        return error(409, str(exc))
    return web.json_response(daemon.status())


# -- QR codes ------------------------------------------------------------------

async def api_qr(request: web.Request) -> web.StreamResponse:
    daemon = request.app[DAEMON]
    level = int(request.match_info["level"])
    if not 1 <= level <= daemon.config.vending.levels:
        return error(404, f"level must be 1..{daemon.config.vending.levels}")
    path = daemon.config.qr.dir / f"level_{level}.png"
    if not path.exists():
        data = f"{daemon.config.qr.base_url}/{daemon.config.system.machine_id}/{level}"
        await asyncio.to_thread(write_qr_png, data, path)
        log.info("generated %s for %s", path, data)
    return web.FileResponse(path)


def write_qr_png(data: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(data).save(path)


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
    return ws


async def broadcast(app: web.Application) -> None:
    """Push status to every socket on each state change, and at least every HEARTBEAT_S."""
    daemon = app[DAEMON]
    while True:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(daemon.changed.wait(), timeout=HEARTBEAT_S)
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
