"""Local event log in SQLite. One table, append only. A failed write never stops the flow.

Reads for the settings screen: `query` (newest first, `before` an id for paging, one SQL per
filter) and `summary` (counters; "today" is the machine's local day). Rows are data; the page
turns them into phrases.
"""

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
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events (kind, ts);
"""

VEND_STATES = ("checking_purchase", "door_unlocked", "door_opened", "door_alarm", "completing")
_VEND_STATES_SQL = ", ".join(f"'{state}'" for state in VEND_STATES)
# SQL WHERE clauses per filter; "all" has none
FILTERS: dict[str, str] = {
    "all": "",
    "vends": (f"(kind = 'transition' AND (json_extract(details, '$.to') IN ({_VEND_STATES_SQL}) "
              f"OR json_extract(details, '$.from') IN ({_VEND_STATES_SQL}))) "
              "OR kind IN ('purchase_check', 'outbox')"),
    "hardware": ("kind IN ('hardware', 'motor', 'timeout', 'rejected') "
                 "OR (kind = 'transition' "
                 "AND json_extract(details, '$.to') IN ('out_of_order', 'door_forced'))"),
    "network": "kind = 'network'",
}
_COLUMNS = "id, ts, kind, state, level, purchase_id, details"


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
        return await self.query(limit)

    async def query(self, limit: int, before: int | None = None,
                    filter: str = "all") -> list[dict]:
        """Newest first; `before` = only rows with a smaller id (paging). Raises KeyError for an
        unknown filter."""
        if self._db is None:
            return []
        where = [FILTERS[filter]] if FILTERS[filter] else []
        params: list = []
        if before is not None:
            where.append("id < ?")
            params.append(before)
        sql = f"SELECT {_COLUMNS} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(f"({clause})" for clause in where)
        cursor = await self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "state": r[3], "level": r[4],
             "purchase_id": r[5], "details": None if r[6] is None else json.loads(r[6])}
            for r in rows
        ]

    async def summary(self, now: datetime | None = None) -> dict:
        """{vends_today, vends_total, alarms_today, faults_today}. A vend is a transition to
        completing, an alarm one to door_alarm or door_forced, a fault one to out_of_order by a
        hardware or database fault. "Today" starts at local midnight of `now` (default: now)."""
        if self._db is None:
            return {"vends_today": 0, "vends_total": 0, "alarms_today": 0, "faults_today": 0}
        local_now = (now or datetime.now(UTC)).astimezone()
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.astimezone(UTC).isoformat(timespec="milliseconds")
        cursor = await self._db.execute(
            "SELECT "
            "  COALESCE(SUM(json_extract(details, '$.to') = 'completing' AND ts >= ?), 0), "
            "  COALESCE(SUM(json_extract(details, '$.to') = 'completing'), 0), "
            "  COALESCE(SUM(json_extract(details, '$.to') IN ('door_alarm', 'door_forced') "
            "               AND ts >= ?), 0), "
            "  COALESCE(SUM(json_extract(details, '$.to') = 'out_of_order' "
            "               AND json_extract(details, '$.event') "
            "                   IN ('hardware_fault', 'database_fault') AND ts >= ?), 0) "
            "FROM events WHERE kind = 'transition'", (since, since, since))
        vends_today, vends_total, alarms_today, faults_today = await cursor.fetchone()
        return {"vends_today": vends_today, "vends_total": vends_total,
                "alarms_today": alarms_today, "faults_today": faults_today}
