import logging

import pytest

from monitoni.eventlog import EventLog


async def test_write_and_recent_newest_first(tmp_path):
    log = EventLog(tmp_path / "sub" / "events.db")
    await log.start()
    await log.write("transition", "idle", details={"to": "idle"})
    await log.write("command", "idle", level=2, purchase_id="abc")
    await log.write("timeout", "sleep")
    rows = await log.recent(2)
    await log.stop()
    assert [r["kind"] for r in rows] == ["timeout", "command"]
    assert rows[1]["level"] == 2 and rows[1]["purchase_id"] == "abc" and rows[1]["details"] is None
    assert rows[0]["ts"].endswith("+00:00")


async def test_schema_survives_restart(tmp_path):
    path = tmp_path / "events.db"
    for _ in range(2):
        log = EventLog(path)
        await log.start()
        await log.write("daemon", "idle")
        await log.stop()
    log = EventLog(path)
    await log.start()
    assert len(await log.recent(10)) == 2
    await log.stop()


async def test_failed_write_logs_and_continues(tmp_path, caplog):
    log = EventLog(tmp_path / "events.db")
    await log.start()
    await log.stop()
    with caplog.at_level(logging.ERROR, logger="monitoni.eventlog"):
        await log.write("transition", "idle")  # must not raise
    assert "event log write failed" in caplog.text
    assert await log.recent(10) == []


# -- filters, paging, summary (Milestone 6) ---------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402


async def seed(log: EventLog) -> None:
    """A vend, a forced door, a fault, a network blip and some noise, oldest first."""
    rows = [
        ("daemon", "idle", None, {"event": "start"}),
        ("command", "idle", None, {"event": "touch"}),
        ("transition", "checking_purchase", 3,
         {"from": "idle", "to": "checking_purchase", "event": "select_level"}),
        ("purchase_check", "checking_purchase", 3, {"permitted": True, "item": 3, "selected": 3}),
        ("transition", "door_unlocked", 3,
         {"from": "checking_purchase", "to": "door_unlocked", "event": "purchase_valid"}),
        ("hardware", "door_unlocked", 3, {"event": "door_opened"}),
        ("transition", "door_opened", 3,
         {"from": "door_unlocked", "to": "door_opened", "event": "door_opened"}),
        ("outbox", "door_unlocked", 3, {"kind": "complete", "delivered": False, "attempts": 0}),
        ("timeout", "door_opened", 3, {"seconds": 10}),
        ("transition", "door_alarm", 3, {"from": "door_opened", "to": "door_alarm",
                                         "event": "timeout"}),
        ("transition", "completing", 3, {"from": "door_alarm", "to": "completing",
                                         "event": "door_closed"}),
        ("transition", "idle", 3, {"from": "completing", "to": "idle", "event": "complete"}),
        ("transition", "door_forced", None, {"from": "idle", "to": "door_forced",
                                             "event": "door_opened"}),
        ("transition", "idle", None, {"from": "door_forced", "to": "idle",
                                      "event": "door_closed"}),
        ("network", "idle", None, {"purchase_server": "unreachable", "error": "x"}),
        ("motor", "idle", None, {"event": "start"}),
        ("rejected", "idle", None, {"event": "complete"}),
        ("transition", "out_of_order", None, {"from": "idle", "to": "out_of_order",
                                              "event": "hardware_fault", "error": "x"}),
        ("transition", "settings", None, {"from": "out_of_order", "to": "settings",
                                          "event": "enter_settings"}),
    ]
    for kind, state, level, details in rows:
        await log.write(kind, state, level=level, details=details)


async def test_filters_pick_the_right_rows(tmp_path):
    log = EventLog(tmp_path / "events.db")
    await log.start()
    await seed(log)
    kinds = {name: [(r["kind"], (r["details"] or {}).get("to")) for r in
                    reversed(await log.query(100, filter=name))] for name in ("vends", "hardware",
                                                                              "network")}
    assert kinds["vends"] == [
        ("transition", "checking_purchase"), ("purchase_check", None),
        ("transition", "door_unlocked"), ("transition", "door_opened"), ("outbox", None),
        ("transition", "door_alarm"), ("transition", "completing"), ("transition", "idle")]
    assert kinds["hardware"] == [
        ("hardware", None), ("timeout", None), ("transition", "door_forced"), ("motor", None),
        ("rejected", None), ("transition", "out_of_order")]
    assert kinds["network"] == [("network", None)]
    assert len(await log.query(100)) == 19
    with pytest.raises(KeyError):
        await log.query(10, filter="everything")
    await log.stop()


async def test_before_pages_backwards(tmp_path):
    log = EventLog(tmp_path / "events.db")
    await log.start()
    await seed(log)
    page1 = await log.query(5)
    page2 = await log.query(5, before=page1[-1]["id"])
    assert [r["id"] for r in page1] == list(range(19, 14, -1))
    assert [r["id"] for r in page2] == list(range(14, 9, -1))
    vends1 = await log.query(3, filter="vends")
    vends2 = await log.query(3, before=vends1[-1]["id"], filter="vends")
    assert [r["details"].get("to") for r in vends1 + vends2] == [
        "idle", "completing", "door_alarm", None, "door_opened", "door_unlocked"]
    await log.stop()


async def test_summary_counts_vends_alarms_and_faults_of_the_local_day(tmp_path):
    log = EventLog(tmp_path / "events.db")
    await log.start()
    await seed(log)
    # a vend and an alarm from two days ago, inserted with an old timestamp
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="milliseconds")
    for to in ("completing", "door_alarm"):
        await log._db.execute(
            "INSERT INTO events (ts, kind, state, level, details) VALUES (?, ?, ?, ?, ?)",
            (old, "transition", to, 2, f'{{"from": "x", "to": "{to}", "event": "y"}}'))
    await log._db.commit()
    assert await log.summary() == {"vends_today": 1, "vends_total": 2, "alarms_today": 2,
                                   "faults_today": 1}
    # "today" starts at the local midnight of the clock it is given: from tomorrow's point of
    # view nothing has happened yet, from the old rows' day everything counts
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    assert (await log.summary(tomorrow))["vends_today"] == 0
    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    assert (await log.summary(two_days_ago))["vends_today"] == 2
    await log.stop()


async def test_empty_log_summary(tmp_path):
    log = EventLog(tmp_path / "events.db")
    await log.start()
    assert await log.summary() == {"vends_today": 0, "vends_total": 0, "alarms_today": 0,
                                   "faults_today": 0}
    assert await log.query(10, before=5, filter="vends") == []
    await log.stop()
