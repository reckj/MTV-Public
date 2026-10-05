import logging

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
