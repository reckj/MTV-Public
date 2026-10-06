"""Durable purchase completions.

Every completion (`success` true or false) goes into the SQLite `outbox` table first; a sender
task delivers the oldest row, deletes it when the server accepted it, otherwise records the
failure and waits (`outbox_backoff_s`, last value repeats) before trying again. Rows survive
restarts. This is the one component in the daemon that retries: an idempotent HTTP POST is not
a hardware command. It never touches hardware.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  TEXT NOT NULL,
    purchase_id TEXT NOT NULL,
    level       INTEGER NOT NULL,
    success     INTEGER NOT NULL,
    reason      TEXT,
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
            log.info("outbox: %d completion(s) waiting from before this start", self.pending_count)
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

    async def enqueue(self, purchase_id: str, level: int, success: bool,
                      reason: str | None = None) -> None:
        """Store a completion; the sender picks it up at once. Raises if the database fails."""
        created = datetime.now(UTC).isoformat(timespec="milliseconds")
        await self._db.execute(
            "INSERT INTO outbox (created_ts, purchase_id, level, success, reason) "
            "VALUES (?, ?, ?, ?, ?)", (created, purchase_id, level, int(success), reason))
        await self._db.commit()
        self.pending_count = await self.pending()
        details = {"delivered": False, "attempts": 0, "success": success}
        if reason is not None:
            details["reason"] = reason
        await self.events.write("purchase_complete", self.state_name(), level=level,
                                purchase_id=purchase_id, details=details)
        log.info("outbox: queued completion of %s (success=%s%s), %d pending",
                 purchase_id, success, f", {reason}" if reason else "", self.pending_count)
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
            row = await self._oldest()
            if row is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            row_id, purchase_id, level, success, reason, attempts = row
            if await self.purchase.complete(purchase_id, level, bool(success)):
                await self._db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                await self._db.commit()
                self.pending_count = await self.pending()
                failures = 0
                details = {"delivered": True, "attempts": attempts + 1, "success": bool(success)}
                if reason is not None:
                    details["reason"] = reason
                await self.events.write("purchase_complete", self.state_name(), level=level,
                                        purchase_id=purchase_id, details=details)
                log.info("outbox: delivered completion of %s after %d attempt(s), %d pending",
                         purchase_id, attempts + 1, self.pending_count)
                self.on_change()
                continue
            error = self.purchase.status().get("last_error") or "not accepted"
            await self._db.execute("UPDATE outbox SET attempts = ?, last_error = ? WHERE id = ?",
                                   (attempts + 1, error, row_id))
            await self._db.commit()
            await self.events.write("purchase_complete", self.state_name(), level=level,
                                    purchase_id=purchase_id,
                                    details={"delivered": False, "attempts": attempts + 1,
                                             "success": bool(success), "error": error})
            delay = self.backoff[min(failures, len(self.backoff) - 1)]
            failures += 1
            log.warning("outbox: completion of %s failed (attempt %d: %s); next try in %ss",
                        purchase_id, attempts + 1, error, delay)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):  # a new row does not help this one, but it
                async with asyncio.timeout(delay):   # is cheap to look again right away
                    await self._wake.wait()

    async def _oldest(self) -> tuple | None:
        cursor = await self._db.execute(
            "SELECT id, purchase_id, level, success, reason, attempts FROM outbox "
            "ORDER BY id LIMIT 1")
        return await cursor.fetchone()
