"""Purchase server: the protocol, the check results, the HTTP client and the mock.

Wire protocol, as the old machines spoke it (the server exists, we match it):
  POST <base_url><check_path>    {"machine_id", "level"}
      200 {"valid": true, "purchase_id": ...}  paid        -> Paid(purchase_id)
      200 {"valid": false, ...}                rejected    -> Invalid
      404                                      nothing yet -> NotYet
      anything else / no answer                            -> PurchaseServerError
  POST <base_url><complete_path> {"purchase_id", "machine_id", "level", "success"}
      200 = accepted; anything else = not accepted (the outbox tries again later)
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from monitoni.config import PurchaseServerConfig

log = logging.getLogger(__name__)


class PurchaseServerError(Exception):
    """Transport error, timeout or an unexpected status. The poll loop simply tries again."""


@dataclass(frozen=True)
class Paid:
    purchase_id: str


@dataclass(frozen=True)
class NotYet:
    pass


@dataclass(frozen=True)
class Invalid:
    pass


CheckResult = Paid | NotYet | Invalid


class PurchaseServer(Protocol):
    on_reachability: Callable[[bool], None]  # called when `reachable` changes

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def check(self, level: int) -> CheckResult:
        """Is there a paid, unredeemed purchase for this level? Raises PurchaseServerError."""
        ...

    async def complete(self, purchase_id: str, level: int, success: bool) -> bool:
        """Tell the server the purchase was handed out (or not). True if the server accepted it."""
        ...

    def status(self) -> dict:
        """{reachable, last_ok, last_error}, JSON-serialisable."""
        ...


# -- the real one ----------------------------------------------------------------

class HttpPurchaseServer:
    """One httpx client for the daemon's lifetime. No retries here: polling and the outbox retry."""

    def __init__(self, config: PurchaseServerConfig, machine_id: str) -> None:
        self.config = config
        self.machine_id = machine_id
        self.on_reachability: Callable[[bool], None] = lambda ok: None
        self.reachable: bool | None = None  # unknown until the first request
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.config.base_url,
                                         timeout=self.config.timeout_s)

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def status(self) -> dict:
        return {"reachable": self.reachable, "last_ok": self.last_ok, "last_error": self.last_error}

    async def check(self, level: int) -> CheckResult:
        response = await self._post(self.config.check_path,
                                    {"machine_id": self.machine_id, "level": level}, (200, 404))
        if response.status_code == 404:
            return NotYet()
        data = response.json() if response.content else None
        if not isinstance(data, dict):
            raise PurchaseServerError(f"check answered 200 without a JSON object: "
                                      f"{response.text!r}")
        if data.get("valid") is True:
            purchase_id = data.get("purchase_id")
            if not isinstance(purchase_id, str) or not purchase_id:
                raise PurchaseServerError(f"valid purchase without a purchase_id: {data!r}")
            return Paid(purchase_id)
        return Invalid()

    async def complete(self, purchase_id: str, level: int, success: bool) -> bool:
        body = {"purchase_id": purchase_id, "machine_id": self.machine_id, "level": level,
                "success": success}
        try:
            await self._post(self.config.complete_path, body, (200,))
        except PurchaseServerError as exc:
            log.warning("completion of %s not accepted: %s", purchase_id, exc)
            return False
        return True

    async def _post(self, path: str, body: dict, ok_statuses: tuple[int, ...]) -> httpx.Response:
        """One request. Any exception or unexpected status marks the server unreachable."""
        if self._client is None:
            raise PurchaseServerError("purchase client is not started")
        try:
            response = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            self._record(False, f"{type(exc).__name__}: {exc or 'no detail'}")
            raise PurchaseServerError(self.last_error) from None
        if response.status_code not in ok_statuses:
            self._record(False, f"HTTP {response.status_code} from {path}")
            raise PurchaseServerError(self.last_error)
        self._record(True, None)
        return response

    def _record(self, ok: bool, error: str | None) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if ok:
            self.last_ok, self.last_error = now, None
        else:
            self.last_error = error
        if ok != self.reachable:
            self.reachable = ok
            log.log(logging.INFO if ok else logging.WARNING,
                    "purchase server %s%s", "reachable" if ok else "unreachable",
                    "" if ok else f": {error}")
            self.on_reachability(ok)


# -- the mock, for mock mode and tests -------------------------------------------

class MockPurchaseServer:
    """`check` says NotYet until `simulate_payment(level)`, then Paid once; always reachable."""

    def __init__(self) -> None:
        self.on_reachability: Callable[[bool], None] = lambda ok: None
        self._paid: set[int] = set()
        self._invalid: set[int] = set()
        self.completions: list[dict] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {"reachable": True, "last_ok": None, "last_error": None}

    def simulate_payment(self, level: int) -> None:
        log.info("mock purchase server: payment simulated for level %d", level)
        self._paid.add(level)

    def simulate_invalid(self, level: int) -> None:
        """The next check for this level answers 'known and rejected' (tests only)."""
        self._invalid.add(level)

    async def check(self, level: int) -> CheckResult:
        if level in self._invalid:
            self._invalid.discard(level)
            return Invalid()
        if level not in self._paid:
            return NotYet()
        self._paid.discard(level)
        purchase_id = uuid.uuid4().hex
        log.info("mock purchase server: valid purchase %s for level %d", purchase_id, level)
        return Paid(purchase_id)

    async def complete(self, purchase_id: str, level: int, success: bool) -> bool:
        log.info("mock purchase server: complete %s level %d success=%s",
                 purchase_id, level, success)
        self.completions.append({"purchase_id": purchase_id, "level": level, "success": success})
        return True
