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
    assert row["state"] == "running"
    assert row["detail"] == "job 7"


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
        unknown = [w for w in worker_registry.snapshot()
                   if w["worker"] == "mediascan"]
    assert unknown and unknown[0]["state"] in ("unknown", "idle", "running", "error")


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
