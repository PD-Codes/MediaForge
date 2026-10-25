"""Library: targeted cache invalidation, the alias index memo, the scoped
alias map of /api/downloaded-folders and the watcher's one-scan-at-a-time rule.

* A delete, rename or move used to mark EVERY library location stale, so the
  next auto-rescan walked the whole library for one renamed folder.
* alias_index() rebuilt the whole index (one table read, a JSON parse per row)
  on every page init and download dialog; it is now memoised on the table's
  (row count, newest resolved_at).
* /api/downloaded-folders handed the alias index of ALL locations to a group
  restricted to some of them.
* The watcher started a second scan of a location while the first was still
  running, and the paths of the skipped one were lost.
"""

import threading
import time
from pathlib import Path

import pytest

from mediaforge.web import db


def _scanned_at(key):
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT scanned_at FROM library_cache WHERE path_key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _drop(*keys):
    conn = db.get_db()
    try:
        conn.executemany("DELETE FROM library_cache WHERE path_key=?", [(k,) for k in keys])
        conn.commit()
    finally:
        conn.close()


def test_invalidate_only_the_given_locations(app):
    keys = ("t-inv-a", "t-inv-b")
    for key in keys:
        db.set_library_cache(key, {"titles": []}, scanned_at=1000.0)
    try:
        db.invalidate_library_cache(["t-inv-a"])
        assert _scanned_at("t-inv-a") == 0
        assert _scanned_at("t-inv-b") == 1000.0
    finally:
        _drop(*keys)


def test_invalidate_paths_marks_the_location_holding_the_path(app, tmp_path, monkeypatch):
    from mediaforge.web.routes import library as lib

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    (root_a / "Show").mkdir(parents=True)
    root_b.mkdir()
    monkeypatch.setattr(lib, "_lib_build_scan_targets",
                        lambda: [("A", 901, root_a), ("B", 902, root_b)])
    monkeypatch.setattr(lib, "_lib_resolved_bases",
                        lambda targets: [(cp, Path(base).resolve()) for _, cp, base in targets])
    seen = []
    monkeypatch.setattr(lib, "invalidate_library_cache", lambda keys=None: seen.append(keys))

    lib._lib_invalidate_paths(str(root_a / "Show"))
    lib._lib_invalidate_paths(str(tmp_path / "elsewhere"))

    # Inside A: only A. Outside every location: all of them (never a stale listing).
    assert seen == [["901"], None]


def test_alias_index_is_memoised_until_the_table_changes(app):
    from mediaforge.web import library_aliases as la

    db.set_library_aliases("t-alias-folder", ["Alias One"])
    try:
        first = la.alias_index()
        assert first.get("aliasone") == "t-alias-folder"
        assert la.alias_index() is first          # unchanged table: memo

        db.set_library_aliases("t-alias-other", ["Second Name"])
        second = la.alias_index()
        assert second is not first
        assert second.get("secondname") == "t-alias-other"
    finally:
        db.prune_library_aliases([f for f in db.get_library_aliases()
                                  if not f.startswith("t-alias-")])


def test_downloaded_folders_only_hands_out_aliases_of_visible_folders(
        app, client, as_user, monkeypatch):
    from mediaforge.web import library_aliases as la
    from mediaforge.web.routes import browse

    monkeypatch.setattr(browse, "downloaded_folder_names", lambda scope=None: ["Visible Show"])
    monkeypatch.setattr(la, "alias_index",
                        lambda: {"visibleshow": "visible show", "hidden": "other location show"})
    as_user("user")
    resp = client.get("/api/downloaded-folders")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["aliases"] == {"visibleshow": "visible show"}

    # The ETag lets the per-page poll revalidate with an empty 304.
    again = client.get("/api/downloaded-folders",
                       headers={"If-None-Match": resp.headers["ETag"]})
    assert again.status_code == 304


def test_watcher_runs_one_scan_per_location_and_keeps_late_paths():
    lw = pytest.importorskip("mediaforge.web.library_watcher")
    if not lw.WATCHDOG_AVAILABLE:
        pytest.skip("watchdog not installed")

    release = threading.Event()
    calls, active, peak = [], [0], [0]
    lock = threading.Lock()

    def callback(_key, changed):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            calls.append(set(changed))
        if len(calls) == 1:
            release.wait(5)
        with lock:
            active[0] -= 1

    handler = lw._LibraryEventHandler("default", callback, debounce=0.05)
    handler._schedule_scan("/lib/a.mkv")
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    handler._schedule_scan("/lib/b.mkv")      # arrives while the first scan runs
    time.sleep(0.3)                           # its timer fires and must not start a scan
    assert len(calls) == 1
    release.set()
    deadline = time.time() + 5
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert calls == [{"/lib/a.mkv"}, {"/lib/b.mkv"}]
    assert peak[0] == 1
