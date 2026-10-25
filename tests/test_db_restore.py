"""restore_snapshot() while other connections are open (P23-03).

The restore used to delete ``-wal``/``-shm`` and copy the snapshot over the
live file. Every connection opened before that (the request's own, the worker
threads', the process' idle anchor) kept the unlinked WAL: new connections then
failed with "disk I/O error", and when the last old connection closed it
checkpointed the OLD WAL into the restored file -- silently undoing the restore.
These tests run the whole cycle against a throwaway database, so the session's
shared test database is never replaced.
"""

import sqlite3

import pytest

INSERT = "INSERT INTO download_queue (title) VALUES (?)"


@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    from mediaforge.web import db, dbmigrate

    path = tmp_path / "mediaforge.db"
    # dbmigrate resolves DB_PATH from the db package at call time.
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(dbmigrate, "SNAPSHOT_DIR", tmp_path / "db_snapshots")
    conn = db._configure_connection(sqlite3.connect(str(path)))
    conn.execute("CREATE TABLE download_queue (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.executemany(INSERT, [("T%d" % i,) for i in range(200)])
    conn.commit()
    conn.close()
    return path


def _open(path):
    from mediaforge.web import db
    return db._configure_connection(sqlite3.connect(str(path), timeout=30,
                                                    check_same_thread=False))


def _counts(conn):
    return dict(conn.execute(
        "SELECT substr(title, 1, 1), COUNT(*) FROM download_queue GROUP BY 1").fetchall())


def test_restore_with_open_connections_is_neither_broken_nor_undone(live_db):
    from mediaforge.web import dbmigrate

    snap = dbmigrate.snapshot(reason="manual")

    # An idle connection like the per-process anchor, and a worker connection
    # that stays open across the restore and wrote rows after the snapshot.
    idle = sqlite3.connect(str(live_db), check_same_thread=False)
    idle.execute("PRAGMA journal_mode=WAL").fetchall()
    worker = _open(live_db)
    for i in range(300):
        worker.execute(INSERT, ("W%d" % i,))
    worker.commit()

    result = dbmigrate.restore_snapshot(snap["id"])
    assert result["ok"], result
    assert result["rollback_snapshot"]

    # A connection opened after the restore works and sees the snapshot only.
    fresh = _open(live_db)
    assert _counts(fresh) == {"T": 200}
    fresh.close()

    # The old worker connection keeps working and closes last -- which used to
    # checkpoint the pre-restore WAL into the restored file.
    for i in range(50):
        worker.execute(INSERT, ("A%d" % i,))
        worker.commit()
    worker.close()
    idle.close()

    check = sqlite3.connect(str(live_db))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _counts(check) == {"T": 200, "A": 50}
    finally:
        check.close()


def test_restore_gives_up_on_a_held_write_lock_and_changes_nothing(live_db, monkeypatch):
    from mediaforge.web import dbmigrate

    snap = dbmigrate.snapshot(reason="manual")
    monkeypatch.setattr(dbmigrate, "_RESTORE_LOCK_WAIT", 0.3)

    writer = _open(live_db)
    writer.execute(INSERT, ("W0",))          # opens the write transaction
    try:
        result = dbmigrate.restore_snapshot(snap["id"])
    finally:
        writer.commit()
        writer.close()
    assert result["ok"] is False
    assert "lock" in result["error"].lower()

    # Nothing was half-applied: the write that held the lock is still there.
    check = _open(live_db)
    try:
        assert _counts(check) == {"T": 200, "W": 1}
    finally:
        check.close()
