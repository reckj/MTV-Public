"""aiohttp routes: GET / (UI), GET /api/status, WS /ws, static files under /static."""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from monitoni.daemon import Daemon

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATUS_PUSH_INTERVAL_S = 2.0

DAEMON = web.AppKey("daemon", object)
WEBSOCKETS = web.AppKey("websockets", set)


def create_app(daemon: "Daemon") -> web.Application:
    app = web.Application()
    app[DAEMON] = daemon
    app[WEBSOCKETS] = set()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static", STATIC_DIR)
    app.on_shutdown.append(close_websockets)
    return app


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_status(request: web.Request) -> web.Response:
    return web.json_response(request.app[DAEMON].status())


async def websocket(request: web.Request) -> web.WebSocketResponse:
    """Push the status object on connect and every STATUS_PUSH_INTERVAL_S."""
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    request.app[WEBSOCKETS].add(ws)
    daemon = request.app[DAEMON]
    log.debug("websocket connected (%d open)", len(request.app[WEBSOCKETS]))

    async def push_status() -> None:
        while not ws.closed:
            await ws.send_json(daemon.status())
            await asyncio.sleep(STATUS_PUSH_INTERVAL_S)

    pusher = asyncio.create_task(push_status())
    try:
        async for _ in ws:  # drains incoming frames until the peer or we close
            pass
    finally:
        pusher.cancel()
        request.app[WEBSOCKETS].discard(ws)
        log.debug("websocket closed (%d open)", len(request.app[WEBSOCKETS]))
    return ws


async def close_websockets(app: web.Application) -> None:
    for ws in set(app[WEBSOCKETS]):
        await ws.close(code=1001, message=b"daemon shutting down")
