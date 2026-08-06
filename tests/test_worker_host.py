"""Worker heartbeats and the external worker host.

The heartbeat test is a regression test with a story: the first version of
``beat()`` wrote NULL into ``worker_heartbeats.last_error``, which is NOT NULL.
Every single heartbeat failed, ``beat()`` swallowed the error by design (a
heartbeat must never take a worker down), and the symptom was an Operations
view that was simply empty. Nothing in the log at anything above DEBUG.
"""

import pytest

from mediaforge.web import worker_host, worker_registry


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

def _row(app, worker):
    from mediaforge.web.db import get_db
    with app.app_context():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM worker_heartbeats WHERE worker = ?", (worker,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def test_first_heartbeat_actually_writes_a_row(app):
    """The INSERT branch. This is the one that used to fail on every call."""
    with app.app_context():
        worker_registry.beat("pytest-fresh", state="idle", detail="hello")
    row = _row(app, "pytest-fresh")
    assert row is not None, "beat() silently wrote nothing"
    assert row["state"] == "idle"
    assert row["detail"] == "hello"
    assert row["last_error"] == ""      # NOT NULL: never NULL, even with no error


def test_heartbeat_updates_in_place(app):
    with app.app_context():
        worker_registry.beat("pytest-update", state="idle")
        worker_registry.working("pytest-update", detail="job 7")
    row = _row(app, "pytest-update")
    # "working", not "running": the Operations view knows exactly three states
    # (idle / working / error) and beat() normalizes onto them on write, so a
    # legacy caller passing "running" also lands here.
    assert row["state"] == worker_registry.STATE_WORKING
    assert row["detail"] == "job 7"


def test_legacy_state_names_are_normalized_on_write(app):
    """A module's worker (or an old call site) may still say "running"."""
    with app.app_context():
        worker_registry.beat("pytest-legacy", state="running")
        assert _row(app, "pytest-legacy")["state"] == worker_registry.STATE_WORKING
        worker_registry.beat("pytest-legacy", state="unknown")
        assert _row(app, "pytest-legacy")["state"] == worker_registry.STATE_IDLE


def test_extras_merge_instead_of_replacing(app):
    """A "still working" beat must not wipe the count the last run reported."""
    with app.app_context():
        worker_registry.done("pytest-extra", extra={"found": 42})
        worker_registry.working("pytest-extra", detail="scanning")
        entry = [w for w in worker_registry.snapshot()
                 if w["worker"] == "pytest-extra"][0]
    assert entry["extra"]["found"] == 42


def test_a_working_worker_gets_a_stall_deadline_and_an_idle_one_does_not(app):
    with app.app_context():
        worker_registry.working("upscale", detail="episode 1")
        working = [w for w in worker_registry.snapshot()
                   if w["worker"] == "upscale"][0]
        assert working["stall_deadline"], "a working worker must be watched"
        assert not worker_registry.is_stalled(working)

        # Same worker, idle: nothing to count down to. This is the false
        # "overdue" the old fixed-window check produced.
        worker_registry.done("upscale")
        idle = [w for w in worker_registry.snapshot()
                if w["worker"] == "upscale"][0]
        assert idle["stall_deadline"] is None
        assert not worker_registry.is_stalled(idle)


def test_scheduled_workers_are_never_watched_for_stalls(app):
    """Sleeping for an hour between rounds is waiting, not stalling."""
    assert worker_registry.stall_after("autosync") is None
    assert worker_registry.stall_after("library_scan") is None
    assert worker_registry.stall_after("queue")


def test_errors_are_sticky_until_explicitly_cleared(app):
    """A failure at 4 a.m. has to still be visible in the morning."""
    with app.app_context():
        worker_registry.fail("pytest-sticky", "boom", detail="job 3")
        assert _row(app, "pytest-sticky")["last_error"] == "boom"
        assert _row(app, "pytest-sticky")["error_at"]

        # A plain heartbeat must not wipe it.
        worker_registry.beat("pytest-sticky", state="idle")
        assert _row(app, "pytest-sticky")["last_error"] == "boom"

        # done() clears it explicitly.
        worker_registry.done("pytest-sticky", detail="recovered")
        assert _row(app, "pytest-sticky")["last_error"] == ""


def test_last_run_is_not_erased_by_a_plain_heartbeat(app):
    with app.app_context():
        worker_registry.done("pytest-lastrun")
        first = _row(app, "pytest-lastrun")["last_run"]
        assert first
        worker_registry.beat("pytest-lastrun", state="idle")
        assert _row(app, "pytest-lastrun")["last_run"] == first


def test_snapshot_lists_known_workers_even_when_silent(app):
    """"never started" and "does not exist" are different problems."""
    with app.app_context():
        names = {w["worker"] for w in worker_registry.snapshot()}
    assert set(worker_registry.WORKERS) <= names
    with app.app_context():
        silent = [w for w in worker_registry.snapshot()
                  if w["worker"] == "mediascan"]
    # A worker that has never reported is idle, not "unknown" -- see the
    # STATE_* constants in worker_registry for why that state is gone.
    assert silent and silent[0]["state"] in (
        worker_registry.STATE_IDLE,
        worker_registry.STATE_WORKING,
        worker_registry.STATE_ERROR,
    )


def test_beat_never_raises(app, monkeypatch):
    """Diagnostics must not be able to take a worker down."""
    def _boom():
        raise RuntimeError("database is gone")
    monkeypatch.setattr("mediaforge.web.db.get_db", _boom)
    with app.app_context():
        worker_registry.beat("pytest-safe", state="idle")   # must not raise


# ---------------------------------------------------------------------------
# Worker host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, "inprocess"),
    ("", "inprocess"),
    ("inprocess", "inprocess"),
    ("anything-else", "inprocess"),
    ("external", "external"),
    ("EXTERNAL", "external"),
    ("separate", "external"),
    ("host", "external"),
])
def test_worker_mode_defaults_to_in_process(monkeypatch, value, expected):
    """Doing nothing must keep the old behaviour. This is the safety property."""
    monkeypatch.delenv("MEDIAFORGE_WORKER_MODE", raising=False)
    if value is not None:
        monkeypatch.setenv("MEDIAFORGE_WORKER_MODE", value)
    assert worker_host.worker_mode() == expected
    assert worker_host.workers_run_in_web_process() is (expected == "inprocess")


def test_selected_workers_defaults_to_all(monkeypatch):
    monkeypatch.delenv("MEDIAFORGE_WORKERS", raising=False)
    assert worker_host.selected_workers() == list(worker_host.DEFAULT_WORKERS)


def test_selected_workers_honours_the_list(monkeypatch):
    monkeypatch.setenv("MEDIAFORGE_WORKERS", "queue, encoding")
    assert worker_host.selected_workers() == ["queue", "encoding"]


def test_unknown_worker_names_fall_back_to_all(monkeypatch, caplog):
    """A typo must not silently mean "run nothing" -- the symptom would be
    downloads sitting there with nothing in the log."""
    monkeypatch.setenv("MEDIAFORGE_WORKERS", "queeu")
    assert worker_host.selected_workers() == list(worker_host.DEFAULT_WORKERS)


def test_every_declared_worker_resolves():
    """The dotted paths are strings, so nothing checks them until runtime."""
    for name, path in worker_host._WORKERS.items():
        fn = worker_host._resolve(path)
        assert callable(fn), name


def test_host_workers_are_a_subset_of_the_registry():
    """A worker the host can run but the Operations view does not know about
    would be invisible exactly when it is the one that is stuck."""
    unknown = set(worker_host._WORKERS) - set(worker_registry.WORKERS)
    assert not unknown, unknown


# ---------------------------------------------------------------------------
# Stall watchdog
# ---------------------------------------------------------------------------

def test_a_dead_worker_thread_is_noticed_even_though_it_reported_idle(app, monkeypatch):
    """The failure the first version of the watchdog could not see.

    A worker that unwinds on request (or crashes) last reported "idle", and an
    idle worker has no stall deadline -- so a watchdog that only looked at
    stall deadlines would never restart the one failure it can fully repair.
    """
    from mediaforge.web import worker_watchdog as wd

    restarted = []
    monkeypatch.setattr(wd, "_thread_alive", lambda name: False)
    monkeypatch.setattr(wd, "_restart",
                        lambda worker, entry, age: restarted.append(worker) or "restarted")
    monkeypatch.setattr(wd, "_audit", lambda *a, **k: None)

    with app.app_context():
        worker_registry.idle("queue")
        acted = wd.check_once()

    assert "queue" in acted
    assert "queue" in restarted


def test_a_live_idle_worker_is_left_alone(app, monkeypatch):
    from mediaforge.web import worker_watchdog as wd

    touched = []
    monkeypatch.setattr(wd, "_thread_alive", lambda name: True)
    monkeypatch.setattr(wd, "_restart",
                        lambda worker, entry, age: touched.append(worker) or "restarted")
    monkeypatch.setattr(wd, "_audit", lambda *a, **k: None)

    with app.app_context():
        worker_registry.idle("queue")
        worker_registry.idle("encoding")
        worker_registry.idle("upscale")
        assert wd.check_once() == []
    assert touched == []


def test_worker_exiting_clears_the_started_flag(monkeypatch):
    """Without this the watchdog's own restart request kills a worker for good:
    the thread ends, the module still thinks it started one, and
    _ensure_*_worker() refuses to start another for the life of the process."""
    from mediaforge.web import queue_worker
    from mediaforge.web import worker_watchdog as wd

    queue_worker._queue_worker_started = True
    wd._event_for("queue").set()

    wd.worker_exiting("queue")

    assert queue_worker._queue_worker_started is False
    assert not wd.restart_requested("queue")


def test_publishing_is_deduplicated(app, monkeypatch):
    """An idle worker beats every few seconds. Publishing each one would make
    every connected Operations view rebuild a full snapshot for no change."""
    published = []
    monkeypatch.setattr(worker_registry, "_last_published", {})
    from mediaforge.web import events

    monkeypatch.setattr(events, "publish", lambda topic, payload: published.append(payload))

    with app.app_context():
        worker_registry.idle("pytest-dedup", detail="waiting")
        worker_registry.idle("pytest-dedup", detail="waiting")
        worker_registry.idle("pytest-dedup", detail="waiting")
        assert len(published) == 1, "an unchanged heartbeat must not publish"

        worker_registry.working("pytest-dedup", detail="job 1")
        assert len(published) == 2, "a real change must publish"
