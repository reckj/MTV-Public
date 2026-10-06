"""Shared by the web modules: the app key under which the daemon lives, and error responses."""

from aiohttp import web

DAEMON = web.AppKey("daemon", object)


def error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)
