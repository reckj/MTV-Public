"""A fake purchase server speaking the protocol in monitoni/purchase.py, for tests and walks.

  POST /api/purchase/check     {"machine_id", "level"} -> 200 {"valid": true, "purchase_id"}
                               once a purchase was marked paid (consumed by that answer),
                               200 {"valid": false, "reason": "rejected"} once marked invalid
                               (also consumed), else 404
  POST /api/purchase/complete  {"purchase_id", "machine_id", "level", "success"} -> 200 {"ok": true}

Knobs: `pay()`, `mark_invalid()`, `fail_next` (how many requests fail) with `fail_mode` "500" or
"timeout". Everything received is in `requests`; every accepted completion in `completions`.
Standalone: `python -m tests.fake_purchase_server --port N`, then GET /pay?level=N,
GET /invalid?level=N and GET /completions from a browser or curl.

Guesses, because the note does not say: the body of a 200 to `complete` ({"ok": true}), that a
paid purchase is reported valid once, and the 404 body.
"""

import argparse
import asyncio
import logging
import uuid

from aiohttp import web

log = logging.getLogger(__name__)

CHECK_PATH = "/api/purchase/check"
COMPLETE_PATH = "/api/purchase/complete"
TIMEOUT_HOLD_S = 30.0


class FakePurchaseServer:
    def __init__(self, machine_id: str = "VM001") -> None:
        self.machine_id = machine_id
        self.paid: dict[tuple[str, int], str] = {}  # (machine_id, level) -> purchase_id
        self.invalid: set[tuple[str, int]] = set()
        self.completions: list[dict] = []
        self.requests: list[tuple[str, dict]] = []  # (path, body), oldest first
        self.fail_next = 0
        self.fail_mode = "500"  # or "timeout"
        self._runner: web.AppRunner | None = None
        self._port: int | None = None  # kept after stop(), so a restart can reuse it

    # -- knobs -----------------------------------------------------------------

    def pay(self, level: int, machine_id: str | None = None, purchase_id: str | None = None) -> str:
        purchase_id = purchase_id or f"p-{uuid.uuid4().hex[:8]}"
        self.paid[(machine_id or self.machine_id, level)] = purchase_id
        return purchase_id

    def mark_invalid(self, level: int, machine_id: str | None = None) -> None:
        self.invalid.add((machine_id or self.machine_id, level))

    # -- lifecycle -------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, port: int = 0) -> None:
        app = web.Application()
        app.router.add_post(CHECK_PATH, self._check)
        app.router.add_post(COMPLETE_PATH, self._complete)
        app.router.add_get("/pay", self._pay)
        app.router.add_get("/invalid", self._invalid)
        app.router.add_get("/completions", self._completions)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        self._port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    # -- the protocol ------------------------------------------------------------

    async def _failure(self) -> web.Response | None:
        if self.fail_next <= 0:
            return None
        self.fail_next -= 1
        if self.fail_mode == "timeout":
            await asyncio.sleep(TIMEOUT_HOLD_S)
        return web.json_response({"error": "simulated failure"}, status=500)

    async def _check(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append((CHECK_PATH, body))
        if (failure := await self._failure()) is not None:
            return failure
        key = (body.get("machine_id"), body.get("level"))
        if key in self.invalid:
            self.invalid.discard(key)
            return web.json_response({"valid": False, "reason": "rejected"})
        if key in self.paid:
            return web.json_response({"valid": True, "purchase_id": self.paid.pop(key)})
        return web.json_response({"error": "no purchase"}, status=404)

    async def _complete(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append((COMPLETE_PATH, body))
        if (failure := await self._failure()) is not None:
            return failure
        self.completions.append(body)
        log.info("completion: %s", body)
        return web.json_response({"ok": True})

    # -- browser-walk helpers ------------------------------------------------------

    async def _pay(self, request: web.Request) -> web.Response:
        level = int(request.query["level"])
        purchase_id = self.pay(level, request.query.get("machine_id"), request.query.get("id"))
        log.info("paid: level %d -> %s", level, purchase_id)
        return web.json_response({"paid": True, "level": level, "purchase_id": purchase_id})

    async def _invalid(self, request: web.Request) -> web.Response:
        level = int(request.query["level"])
        self.mark_invalid(level, request.query.get("machine_id"))
        return web.json_response({"invalid": True, "level": level})

    async def _completions(self, request: web.Request) -> web.Response:
        return web.json_response(self.completions)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fake purchase server for manual runs")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--machine-id", default="VM001")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def run() -> None:
        fake = FakePurchaseServer(args.machine_id)
        await fake.start(port=args.port)
        log.info("fake purchase server for %s on %s (GET /pay?level=N, /invalid?level=N, "
                 "/completions)", args.machine_id, fake.url)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
