"""A fake Monitoni purchase server, for tests and browser walks.

Speaks the protocol in monitoni/purchase.py: three GETs, header `Monitoni-Terminal` checked
against the configured token.

  GET /api/vending/permission  200 {"HasPermission": true, "Item": N} while a paid, unexpired
                               permission is open (oldest first), else 200 {"HasPermission": false}
  GET /api/vending/complete    201 {}; consumes the oldest open permission; recorded in `reports`
  GET /api/vending/close       201 {}; recorded in `reports`

Knobs: `pay(item, ttl_s=60)`, `fail_next`/`fail_mode` ("500" or "timeout"), `stop()`/`start()`
keeping the port (connection refused in between). Every request lands in `requests` as
{method, path, token_ok, body} (body = bytes received). Standalone:
`python -m tests.fake_purchase_server --port N --token T`, then GET /pay?item=N[&ttl=S] and
GET /reports from a browser or curl.

Guesses, because the note does not say: a wrong or missing token gets 401; the body of
HasPermission=false is `{"HasPermission": false}`; complete and close answer `201 {}`; a
`complete` without an open permission still gets 201; a permission stays open until a
`complete` consumes it or its ttl passes.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime

from aiohttp import web

from monitoni.purchase import TOKEN_HEADER

log = logging.getLogger(__name__)

PERMISSION_PATH = "/api/vending/permission"
COMPLETE_PATH = "/api/vending/complete"
CLOSE_PATH = "/api/vending/close"
TIMEOUT_HOLD_S = 30.0


class FakePurchaseServer:
    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.permissions: list[tuple[int, float]] = []  # (item, expires at loop time)
        self.reports: list[dict] = []  # {"kind", "ts", "t"} oldest first
        self.requests: list[dict] = []  # {"method", "path", "token_ok", "body"} oldest first
        self.fail_next = 0
        self.fail_mode = "500"  # or "timeout"
        self._runner: web.AppRunner | None = None
        self._port: int | None = None  # kept after stop(), so a restart can reuse it

    # -- knobs -----------------------------------------------------------------

    def pay(self, item: int, ttl_s: float = 60.0) -> None:
        """A customer paid for `item`; the permission expires after ttl_s like the real server's."""
        self.permissions.append((item, asyncio.get_running_loop().time() + ttl_s))

    def report_kinds(self) -> list[str]:
        return [r["kind"] for r in self.reports]

    # -- lifecycle -------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, port: int = 0) -> None:
        app = web.Application()
        app.router.add_get(PERMISSION_PATH, self._permission)
        app.router.add_get(COMPLETE_PATH, self._complete)
        app.router.add_get(CLOSE_PATH, self._close)
        app.router.add_get("/pay", self._pay)
        app.router.add_get("/reports", self._reports)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        self._port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    # -- the protocol ------------------------------------------------------------

    async def _record(self, request: web.Request) -> web.Response | None:
        """Log the request; answer 401 for a bad token or the configured failure, else None."""
        token_ok = request.headers.get(TOKEN_HEADER) == self.token
        self.requests.append({"method": request.method, "path": request.path,
                              "token_ok": token_ok, "body": await request.read()})
        if not token_ok:
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.fail_next > 0:
            self.fail_next -= 1
            if self.fail_mode == "timeout":
                await asyncio.sleep(TIMEOUT_HOLD_S)
            return web.json_response({"error": "simulated failure"}, status=500)
        return None

    def _open_permissions(self) -> list[tuple[int, float]]:
        now = asyncio.get_running_loop().time()
        self.permissions = [(item, expiry) for item, expiry in self.permissions if expiry > now]
        return self.permissions

    async def _permission(self, request: web.Request) -> web.Response:
        if (early := await self._record(request)) is not None:
            return early
        if open_ := self._open_permissions():
            return web.json_response({"HasPermission": True, "Item": open_[0][0]})
        return web.json_response({"HasPermission": False})

    async def _complete(self, request: web.Request) -> web.Response:
        if (early := await self._record(request)) is not None:
            return early
        if self._open_permissions():
            self.permissions.pop(0)
        return self._report("complete")

    async def _close(self, request: web.Request) -> web.Response:
        if (early := await self._record(request)) is not None:
            return early
        return self._report("close")

    def _report(self, kind: str) -> web.Response:
        self.reports.append({"kind": kind, "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                             "t": asyncio.get_running_loop().time()})
        log.info("report: %s", kind)
        return web.json_response({}, status=201)

    # -- browser-walk helpers ------------------------------------------------------

    async def _pay(self, request: web.Request) -> web.Response:
        item = int(request.query["item"])
        ttl = float(request.query.get("ttl", 60))
        self.pay(item, ttl)
        log.info("paid: item %d (ttl %.0fs)", item, ttl)
        return web.json_response({"paid": True, "item": item, "ttl_s": ttl})

    async def _reports(self, request: web.Request) -> web.Response:
        return web.json_response([{"kind": r["kind"], "ts": r["ts"]} for r in self.reports])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fake Monitoni purchase server for manual runs")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", default="test-token")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def run() -> None:
        fake = FakePurchaseServer(args.token)
        await fake.start(port=args.port)
        log.info("fake purchase server on %s (GET /pay?item=N[&ttl=S], GET /reports)", fake.url)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
