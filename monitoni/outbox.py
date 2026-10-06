"""Durable reports to the purchase server: `complete` (the door opened) and `close` (it closed).

Every report goes into the SQLite `outbox` table first; a sender task delivers the oldest row,
deletes it once the server answered 2xx, otherwise records the failure and waits
(`outbox_backoff_s`, last value repeats) before trying again. Rows survive restarts. This is the
one component in the daemon that retries: an idempotent HTTP GET is not a hardware command. It
never touches hardware. Event rows (kind `outbox`) are written when a report is queued and when
it is delivered; a failed attempt only updates the row and the daemon log.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from monitoni.eventlog import EventLog
from monitoni.purchase import PurchaseServer

log = logging.getLogger(__name__)

KINDS = ("complete", "close")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    level       INTEGER NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
"""


class Outbox:
    def __init__(self, path: Path, purchase: PurchaseServer, events: EventLog,
                 backoff: Sequence[float], state_name: Callable[[], str],
                 on_change: Callable[[], None] | None = None) -> None:
        self.path = path
        self.purchase = purchase
        self.events = events
        self.backoff = tuple(backoff)
        self.state_name = state_name  # for the event-log rows
        self.on_change = on_change or (lambda: None)  # the daemon pushes a status
        self.pending_count = 0  # kept current for the status object
        self._db: aiosqlite.Connection | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        self.pending_count = await self.pending()
        if self.pending_count:
            log.info("outbox: %d report(s) waiting from before this start", self.pending_count)
        self._task = asyncio.create_task(self._send_loop(), name="outbox-sender")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        db, self._db = self._db, None
        if db is not None:
            await db.close()

    async def enqueue(self, kind: str, level: int) -> None:
        """Store a report; an idle sender picks it up at once. Raises if the database fails."""
        if kind not in KINDS:
            raise ValueError(f"unknown report kind {kind!r}")
        created = datetime.now(UTC).isoformat(timespec="milliseconds")
        await self._db.execute("INSERT INTO outbox (created_ts, kind, level) VALUES (?, ?, ?)",
                               (created, kind, level))
        await self._db.commit()
        self.pending_count = await self.pending()
        await self.events.write("outbox", self.state_name(), level=level,
                                details={"kind": kind, "delivered": False, "attempts": 0})
        log.info("outbox: queued %s for level %d, %d pending", kind, level, self.pending_count)
        self._wake.set()
        self.on_change()

    async def pending(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM outbox")
        (count,) = await cursor.fetchone()
        return count

    # -- the sender ------------------------------------------------------------

    async def _send_loop(self) -> None:
        failures = 0  # consecutive failures on the current head row; picks the backoff
        while True:
            try:
                failures = await self._send_one(failures)
            except Exception:
                # not the normal "server said no" (that returns False above): a bug or a database
                # error. The one retrying component must not die quietly.
                log.exception("outbox: sender failed unexpectedly; trying again in %ss",
                              self.backoff[-1])
                await asyncio.sleep(self.backoff[-1])

    async def _send_one(self, failures: int) -> int:
        """Deliver the oldest row or wait for one. Returns the updated failure streak."""
        row = await self._oldest()
        if row is None:
            await self._wake.wait()
            self._wake.clear()
            return 0
        row_id, kind, level, attempts = row
        report = self.purchase.complete if kind == "complete" else self.purchase.close
        if await report():
            await self._db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            await self._db.commit()
            self.pending_count = await self.pending()
            await self.events.write("outbox", self.state_name(), level=level,
                                    details={"kind": kind, "delivered": True,
                                             "attempts": attempts + 1})
            log.info("outbox: delivered %s for level %d after %d attempt(s), %d pending",
                     kind, level, attempts + 1, self.pending_count)
            self.on_change()
            return 0
        error = self.purchase.status().get("last_error") or "not accepted"
        await self._db.execute("UPDATE outbox SET attempts = ?, last_error = ? WHERE id = ?",
                               (attempts + 1, error, row_id))
        await self._db.commit()
        delay = self.backoff[min(failures, len(self.backoff) - 1)]
        log.warning("outbox: %s for level %d failed (attempt %d: %s); next try in %ss",
                    kind, level, attempts + 1, error, delay)
        await asyncio.sleep(delay)  # a new row does not help this one; oldest first, after the wait
        return failures + 1

    async def _oldest(self) -> tuple | None:
        cursor = await self._db.execute(
            "SELECT id, kind, level, attempts FROM outbox ORDER BY id LIMIT 1")
        return await cursor.fetchone()
