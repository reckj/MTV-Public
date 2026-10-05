"""Local event log in SQLite. One table, append only. A failed write never stops the flow."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    level       INTEGER,
    purchase_id TEXT,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("event log at %s", self.path)

    async def stop(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            await db.close()

    async def write(self, kind: str, state: str, level: int | None = None,
                    purchase_id: str | None = None, details: dict | None = None) -> None:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        row = (ts, kind, state, level, purchase_id,
               None if details is None else json.dumps(details, default=str))
        try:
            if self._db is None:
                raise RuntimeError("event log is not open")
            await self._db.execute(
                "INSERT INTO events (ts, kind, state, level, purchase_id, details) "
                "VALUES (?, ?, ?, ?, ?, ?)", row)
            await self._db.commit()
        except Exception as exc:
            log.error("event log write failed (%s): %s", exc, row)

    async def recent(self, limit: int) -> list[dict]:
        """Newest first."""
        if self._db is None:
            return []
        cursor = await self._db.execute(
            "SELECT id, ts, kind, state, level, purchase_id, details "
            "FROM events ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "state": r[3], "level": r[4],
             "purchase_id": r[5], "details": None if r[6] is None else json.loads(r[6])}
            for r in rows
        ]
