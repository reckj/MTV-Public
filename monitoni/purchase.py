"""The Monitoni purchase server: protocol, HTTP client and mock.

Wire protocol (notes/purchase-server.md). Every request is a GET with no body and the header
`Monitoni-Terminal: <token>`; the token identifies the machine and never appears in logs,
exceptions or status.

  GET <base_url><permission_path>   200 {"HasPermission": true, "Item": N}  -> Permitted(N)
                                    200 {"HasPermission": false, ...}       -> NotYet()
                                    anything else                           -> PurchaseServerError
  GET <base_url><complete_path>     2xx -> accepted: the product was handed out
  GET <base_url><close_path>        2xx -> accepted: the door is closed again

`Item` is the level that was paid; the machine unlocks it whatever was selected on the screen.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from monitoni.config import PurchaseServerConfig
from monitoni.stamp import local_time

log = logging.getLogger(__name__)

TOKEN_HEADER = "Monitoni-Terminal"


class PurchaseServerError(Exception):
    """Transport error, timeout or an answer we cannot use. The poll loop simply tries again."""


@dataclass(frozen=True)
class Permitted:
    item: int  # the paid level


@dataclass(frozen=True)
class NotYet:
    pass


PermissionResult = Permitted | NotYet


class PurchaseServer(Protocol):
    on_reachability: Callable[[bool], None]  # called when `reachable` changes

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def permission(self) -> PermissionResult:
        """Is a purchase for this machine paid and open? Raises PurchaseServerError."""
        ...

    async def complete(self) -> bool:
        """The product was handed out (the door opened). True if the server accepted it."""
        ...

    async def close(self) -> bool:
        """The door is closed again. True if the server accepted it."""
        ...

    def status(self) -> dict:
        """{reachable, since, last_ok, last_error}, JSON-serialisable, without the token."""
        ...


# -- the real one ----------------------------------------------------------------

class HttpPurchaseServer:
    """One httpx client for the daemon's lifetime. No retries here: polling and the outbox retry."""

    def __init__(self, config: PurchaseServerConfig) -> None:
        self.config = config
        self.on_reachability: Callable[[bool], None] = lambda ok: None
        self.reachable: bool | None = None  # unknown until the first request
        self.since: str | None = None  # when `reachable` last flipped; None while ok since start
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.config.base_url,
                                         timeout=self.config.timeout_s,
                                         headers={TOKEN_HEADER: self.config.token})

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def status(self) -> dict:
        return {"reachable": self.reachable, "since": self.since, "last_ok": self.last_ok,
                "last_error": self.last_error}

    async def permission(self) -> PermissionResult:
        path = self.config.permission_path
        response = await self._get(path, (200,))
        try:
            data = response.json()
        except ValueError:
            raise self._fail(f"{path} answered 200 without JSON") from None
        if not isinstance(data, dict) or "HasPermission" not in data:
            raise self._fail(f"{path} answered without a HasPermission key")
        has = data["HasPermission"]
        if has is False:
            return NotYet()
        if has is not True:
            raise self._fail(f"{path} answered HasPermission={has!r}")
        item = data.get("Item")
        if isinstance(item, bool) or not isinstance(item, int):
            raise self._fail(f"{path} answered HasPermission=true without an integer Item")
        return Permitted(item)

    async def complete(self) -> bool:
        return await self._report(self.config.complete_path)

    async def close(self) -> bool:
        return await self._report(self.config.close_path)

    async def _report(self, path: str) -> bool:
        try:
            await self._get(path, range(200, 300))
        except PurchaseServerError as exc:
            log.warning("%s not accepted: %s", path, exc)
            return False
        return True

    async def _get(self, path: str, ok_statuses) -> httpx.Response:
        """One request. Any exception or unexpected status marks the server unreachable."""
        if self._client is None:
            raise PurchaseServerError("purchase client is not started")
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:  # transport errors and timeouts; messages carry no header
            raise self._fail(f"{type(exc).__name__}: {exc or 'no detail'}") from None
        if response.status_code not in ok_statuses:
            raise self._fail(f"HTTP {response.status_code} from {path}")
        self._record(True, None)
        return response

    def _fail(self, error: str) -> PurchaseServerError:
        self._record(False, error)
        return PurchaseServerError(error)

    def _record(self, ok: bool, error: str | None) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if ok:
            self.last_ok, self.last_error = now, None
        else:
            self.last_error = error
        if ok != self.reachable:
            if not (self.reachable is None and ok):
                self.since = local_time()
            self.reachable = ok
            log.log(logging.INFO if ok else logging.WARNING,
                    "purchase server %s%s", "reachable" if ok else "unreachable",
                    "" if ok else f": {error}")
            self.on_reachability(ok)


# -- the mock, for mock mode and tests -------------------------------------------

class MockPurchaseServer:
    """`permission` says NotYet until `simulate_payment(level)`, then Permitted once; always
    reachable. `complete`/`close` are recorded in order in `reports`."""

    def __init__(self) -> None:
        self.on_reachability: Callable[[bool], None] = lambda ok: None
        self._pending: list[int] = []
        self.reports: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {"reachable": True, "since": None, "last_ok": None, "last_error": None}

    def simulate_payment(self, level: int, item: int | None = None) -> None:
        """The next permission answers true with Item = `item` (default: the level paid for)."""
        log.info("mock purchase server: payment simulated for level %d", level)
        self._pending.append(level if item is None else item)

    async def permission(self) -> PermissionResult:
        if not self._pending:
            return NotYet()
        item = self._pending.pop(0)
        log.info("mock purchase server: permission for item %d", item)
        return Permitted(item)

    async def complete(self) -> bool:
        log.info("mock purchase server: complete")
        self.reports.append("complete")
        return True

    async def close(self) -> bool:
        log.info("mock purchase server: close")
        self.reports.append("close")
        return True
