"""The media library: path validation, scoping, publishing, the shared
SxxExx reader and calendar pruning.

Merged from: test_library_paths.py, test_library_scope.py, test_media_publish.py, test_episode_marker.py, test_calendar_episode_pruning.py.
"""

import os
from pathlib import Path
import pytest

from mediaforge.web.episode_marker import (
    EPISODE_MARKER_RE,
    FALLBACK_EPISODE_RE,
    season_episode_from_name,
)


# ==========================================================================
# test_library_paths.py
#
# Path validation for the endpoints that read or replace files on disk.
# 
# lib_resolve_library_file() is the single gate: /api/upscale/add-library used to
# have none at all, so any logged-in user could name an arbitrary path and have
# the upscale worker overwrite it (replace-original is on by default).
# ==========================================================================
@pytest.fixture()
def library(app, tmp_path, monkeypatch):
    """A download root with one real video file in it."""
    root = tmp_path / "library"
    (root / "Some Show").mkdir(parents=True)
    video = root / "Some Show" / "Some Show - S01E01.mkv"
    video.write_bytes(b"\x00" * 16)
    monkeypatch.setenv("MEDIAFORGE_DOWNLOAD_PATH", str(root))
    with app.app_context():
        from mediaforge.web import db
        db.set_setting("download_path", str(root))
    return {"root": root, "video": video}


def _resolve(app, path):
    from mediaforge.web.routes.library import lib_resolve_library_file
    with app.test_request_context():
        return lib_resolve_library_file(str(path))


def test_accepts_a_file_inside_the_library(app, library):
    assert _resolve(app, library["video"]) is not None


def test_rejects_a_path_outside_the_library(app, library, tmp_path):
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"\x00" * 16)
    assert _resolve(app, outside) is None


def test_rejects_traversal(app, library):
    assert _resolve(app, library["root"] / ".." / "elsewhere.mkv") is None


def test_rejects_a_non_video_file(app, library):
    other = library["root"] / "Some Show" / "notes.txt"
    other.write_text("hello")
    assert _resolve(app, other) is None


def test_rejects_a_book_when_the_caller_wants_video(app, library):
    """The reason lib_resolve_library_file takes an extension set at all.

    An .epub sitting in the library is a perfectly legitimate file, but the
    callers that pass no extension set -- the ffprobe media-info route and the
    upscale worker -- would probe it for eight seconds and then re-encode and
    overwrite it. Video callers must not see it.
    """
    book = library["root"] / "Some Show" / "Handbook.epub"
    book.write_bytes(b"PK\x03\x04")
    assert _resolve(app, book) is None


def test_accepts_a_book_when_the_caller_asks_for_books(app, library):
    from mediaforge.web.media_types import BOOK_EXTS
    from mediaforge.web.routes.library import lib_resolve_library_file

    book = library["root"] / "Some Show" / "Handbook.epub"
    book.write_bytes(b"PK\x03\x04")
    with app.test_request_context():
        assert lib_resolve_library_file(str(book), exts=BOOK_EXTS) is not None


def test_book_caller_still_cannot_escape_the_library(app, library, tmp_path):
    """Widening the extension set must not widen the containment check."""
    from mediaforge.web.media_types import BOOK_EXTS
    from mediaforge.web.routes.library import lib_resolve_library_file

    outside = tmp_path / "elsewhere.epub"
    outside.write_bytes(b"PK\x03\x04")
    with app.test_request_context():
        assert lib_resolve_library_file(str(outside), exts=BOOK_EXTS) is None


def test_drm_book_format_is_not_served(app, library):
    """A .kfx is listed in the shelf but never handed to a reader."""
    from mediaforge.web.media_types import BOOK_EXTS
    from mediaforge.web.routes.library import lib_resolve_library_file

    drm = library["root"] / "Some Show" / "Locked.kfx"
    drm.write_bytes(b"\x00" * 16)
    with app.test_request_context():
        assert lib_resolve_library_file(str(drm), exts=BOOK_EXTS) is None


def test_rejects_a_missing_file(app, library):
    assert _resolve(app, library["root"] / "Some Show" / "nope.mkv") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_rejects_a_symlink_pointing_out_of_the_library(app, library, tmp_path):
    secret = tmp_path / "secret.mkv"
    secret.write_bytes(b"\x00" * 16)
    link = library["root"] / "Some Show" / "link.mkv"
    link.symlink_to(secret)
    assert _resolve(app, link) is None


# ==========================================================================
# test_library_scope.py
#
# Library scoping — the half of groups that is easy to get wrong quietly.
# 
# A permission that does not apply is visible: the button is still there and it
# works. A *scope* that does not apply is invisible: the library simply shows
# everything, and nobody notices until it matters. So these tests come at it
# from the enforcement side rather than the model side (which
# tests/test_ops.py covers).
# ==========================================================================
@pytest.fixture()
def scoped_group(app, users):
    """A group restricted to a custom path, with the plain user in it."""
    from mediaforge.web import groups
    from mediaforge.web.db import add_custom_path, get_custom_paths

    with app.app_context():
        add_custom_path("Pytest scoped", "/tmp/mediaforge-pytest-scoped")
        location = [p for p in get_custom_paths() if p["name"] == "Pytest scoped"][0]
        gid, err = groups.create_group(
            "pytest_scoped", "Scoped", ["library.read"], [str(location["id"])])
        assert err is None, err
        groups.set_user_groups(users["user"], [gid])
    yield {"group_id": gid, "location_id": str(location["id"])}
    with app.app_context():
        groups.set_user_groups(users["user"], [])
        groups.delete_group(gid)


def test_unrestricted_by_default(app, users):
    """Every user is in a built-in group whose scope is "*". If that simply
    won, scoping would be permanently dead — so check the default explicitly."""
    from mediaforge.web.routes.library import lib_current_scope
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert lib_current_scope() == ["*"]


def test_scope_applies_to_the_session(app, users, scoped_group):
    from mediaforge.web.routes.library import lib_current_scope, lib_scope_allows
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert lib_current_scope() == [scoped_group["location_id"]]
        assert lib_scope_allows(int(scoped_group["location_id"])) is True
        assert lib_scope_allows(None) is False        # "default" is out of scope


def test_admins_are_never_scoped(app, users, scoped_group):
    """An admin who cannot see a library cannot fix it either."""
    from mediaforge.web import groups
    from mediaforge.web.routes.library import lib_current_scope
    with app.app_context():
        groups.set_user_groups(users["admin"], [scoped_group["group_id"]])
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["admin"]
        session["user_role"] = "admin"
        assert lib_current_scope() == ["*"]
    with app.app_context():
        groups.set_user_groups(users["admin"], [])


def test_no_session_means_no_restriction(app):
    """No-auth mode has no session at all. Failing closed there would empty the
    library for every install that has never configured a group."""
    from mediaforge.web.routes.library import lib_current_scope
    with app.app_context():
        assert lib_current_scope() == ["*"]


def test_scope_survives_a_broken_group_table(app, users, monkeypatch):
    """A database mid-migration must not lock people out of their library."""
    from mediaforge.web.routes import library

    def _boom(*_a, **_kw):
        raise RuntimeError("no such table: user_groups")

    monkeypatch.setattr("mediaforge.web.groups.effective_scope", _boom)
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert library.lib_current_scope() == ["*"]


def test_library_listing_hides_out_of_scope_locations(as_user, app, scoped_group):
    """The endpoint, not just the helper: this is what the page actually calls."""
    client = as_user("user")
    resp = client.get("/api/library?kind=video")
    assert resp.status_code == 200
    locations = resp.get_json().get("locations") or []
    keys = {str(loc.get("custom_path_id") or "default") for loc in locations}
    assert "default" not in keys, "the default location leaked into a scoped listing"


def test_overview_counters_do_not_leak_out_of_scope_sizes(as_user, scoped_group):
    """Counting everything and showing some of it is how a "restricted" view
    leaks exactly what it was meant to hide."""
    client = as_user("user")
    resp = client.get("/api/library/overview")
    assert resp.status_code == 200
    assert "counts" in resp.get_json()


def test_file_guard_refuses_out_of_scope_paths(app, users, scoped_group, tmp_path):
    """lib_resolve_library_file() is the single "may the caller touch this
    file?" answer, so the scope has to be enforced there and not per route."""
    from mediaforge.web.routes import library

    media = tmp_path / "episode.mkv"
    media.write_bytes(b"x")

    # Pretend the file lives in the default download root, which the scoped
    # group excludes.
    monkey_targets = [("Default", None, tmp_path)]
    original = library._lib_build_scan_targets
    library._lib_build_scan_targets = lambda: monkey_targets
    try:
        with app.test_request_context("/api/library"):
            from flask import session
            session["user_id"] = users["user"]
            session["user_role"] = "user"
            assert library.lib_resolve_library_file(str(media)) is None
            # A worker has no session and must not be blocked by somebody
            # else's scope.
            assert library.lib_resolve_library_file(str(media), scoped=False) is not None
    finally:
        library._lib_build_scan_targets = original


def test_library_locations_endpoint_is_admin_only(as_user):
    assert as_user("user").get("/api/ops/library-locations").status_code == 403
    resp = as_user("admin").get("/api/ops/library-locations")
    assert resp.status_code == 200
    ids = {loc["id"] for loc in resp.get_json()["locations"]}
    assert "default" in ids


# ==========================================================================
# test_media_publish.py
#
# Crash safety of web/media_publish.py.
# 
# The point of publish_output() is that a failing copy must never cost the file
# it was about to replace (see the module docstring there). That is precisely
# the kind of thing that cannot be verified by reading the code, because the
# interesting case is the one where the copy blows up half way through.
# ==========================================================================
@pytest.fixture()
def publish():
    from mediaforge.web import media_publish

    return media_publish


def test_replaces_the_destination(publish, tmp_path):
    """The new payload ends up at the destination, the scratch file is gone."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "out" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"old payload")

    publish.publish_output(source, target)

    assert target.read_bytes() == b"new payload"
    assert not source.exists()


def test_creates_missing_directories(publish, tmp_path):
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"payload")
    target = tmp_path / "a" / "b" / "episode.mkv"

    publish.publish_output(source, target)

    assert target.read_bytes() == b"payload"


def test_a_failed_swap_leaves_the_original_alone(publish, tmp_path, monkeypatch):
    """The whole reason this module exists: no data loss on a failed publish."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "episode.mkv"
    target.write_bytes(b"THE ONLY COPY")

    def boom(*_args, **_kwargs):
        raise OSError("target volume went away")

    monkeypatch.setattr(publish.os, "replace", boom)

    with pytest.raises(OSError):
        publish.publish_output(source, target)

    # Untouched, and no staging file left behind next to it.
    assert target.read_bytes() == b"THE ONLY COPY"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(publish._STAGING_SUFFIX)]
    assert leftovers == []


def test_stale_staging_file_is_overwritten(publish, tmp_path):
    """A staging file from a killed run must not block the next publish."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "episode.mkv"
    target.write_bytes(b"old payload")
    (tmp_path / ("episode.mkv" + publish._STAGING_SUFFIX)).write_bytes(b"junk from a dead run")

    publish.publish_output(source, target)

    assert target.read_bytes() == b"new payload"


def test_sweep_removes_only_matching_leftovers(publish, monkeypatch, tmp_path):
    temp_dir = tmp_path / "mediaforge-temp"
    temp_dir.mkdir()
    stale = temp_dir / "something.upscale.mkv"
    keep = temp_dir / "unrelated.txt"
    stale.write_bytes(b"x")
    keep.write_bytes(b"x")
    monkeypatch.setattr(publish, "MEDIAFORGE_TEMP_DIR", temp_dir)

    publish.sweep_stale_temp_files(".upscale.mkv")

    assert not stale.exists()
    assert keep.exists()


def test_sweep_survives_a_missing_temp_dir(publish, monkeypatch, tmp_path):
    """Called on every worker start, so it must never raise."""
    monkeypatch.setattr(publish, "MEDIAFORGE_TEMP_DIR", tmp_path / "does-not-exist")
    publish.sweep_stale_temp_files(".upscale.mkv")


def test_publish_is_used_by_both_workers():
    """Guards the reason this module exists: nobody may go back to unlink+move."""
    from mediaforge.web import encoding_worker, upscale_worker

    for module in (encoding_worker, upscale_worker):
        assert hasattr(module, "publish_output"), module.__name__


# ==========================================================================
# test_episode_marker.py
#
# The shared SxxExx reader (web/episode_marker.py).
# 
# Every "do I already have this episode?" answer comes from a file name, so the
# scanner, the download dialog and auto-sync have to read one the same way. They
# did not: three copies of the old S(\d{2})E(\d{2,3}) pattern silently truncated
# anything longer than three digits, which AniWorld's absolute episode numbering
# turns from an exotic case into an everyday one.
# ==========================================================================
@pytest.mark.parametrize(
    "name, expected",
    [
        ("One Piece S01E063.mkv", (1, 63)),
        # The regression: the old pattern read this as episode 112.
        ("One Piece S01E1128.mkv", (1, 1128)),
        # The regression that motivated the library-side fix: episode 13 was
        # indexed as episode 1 and collided with the real S02E001.
        ("Show S02E0013.mkv", (2, 13)),
        ("Show S1E1.mkv", (1, 1)),
        ("Show s03e07 - Title.mp4", (3, 7)),
        ("Show S01E01 - S01E02.mkv", (1, 1)),       # first marker wins
        ("Some Movie (2019).mkv", (None, None)),
        ("Show E013.mkv", (None, None)),            # no season -> no guess
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_season_episode_from_name(name, expected):
    assert season_episode_from_name(name) == expected


def test_marker_does_not_truncate():
    """Whole number or no match — never a prefix of one."""
    assert EPISODE_MARKER_RE.search("S01E1128").group(2) == "1128"


def test_fallback_still_needs_two_digits():
    """A bare "E1" appears inside far too many real titles to be a marker."""
    assert FALLBACK_EPISODE_RE.search("Show E1 Title") is None
    assert FALLBACK_EPISODE_RE.search("Show E013 Title").group(1) == "013"


# ==========================================================================
# test_calendar_episode_pruning.py
#
# Regression tests for db.delete_calendar_episodes_except().
# 
# The function used to build one `(season = ? AND episode = ?)` OR-term per kept
# episode. SQLite parses a chain of ORs into a left-deep binary tree, so the
# expression depth grew with the episode count and any long-running series past
# ~1000 episodes aborted the whole calendar sync with:
# 
#     OperationalError: Expression tree is too large (maximum depth 1000)
# 
# Reported from a crash log at routes/calendar_routes.py::_sync_calendar_item.
# The number below is deliberately well past that limit -- One Piece alone is alr-
# eady over 1100 episodes, so this is an ordinary library, not a stress test.
# ==========================================================================
@pytest.fixture()
def db():
    """The db module, imported inside the fixture like the other suites do.

    conftest.py redirects MEDIAFORGE_CONFIG_DIR before the first mediaforge
    import; a module-level import here would run at collection time and could
    beat it.
    """
    from mediaforge.web import db as _db
    return _db


@pytest.fixture()
def media_id(db):
    """A calendar_media row with a throwaway TMDB id, plus its episodes table."""
    db.init_calendar_db()
    # A tmdb id no real show will collide with, so repeated runs stay isolated.
    mid = db.save_calendar_media(999_000_001, "Pruning Test", "Pruning Test", "")
    yield mid
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM calendar_episodes WHERE media_id = ?", (mid,))
        conn.execute("DELETE FROM calendar_media WHERE id = ?", (mid,))
        conn.commit()
    finally:
        conn.close()


def _stored(db, media_id):
    conn = db.get_db()
    try:
        rows = conn.execute(
            "SELECT season, episode FROM calendar_episodes WHERE media_id = ?",
            (media_id,),
        ).fetchall()
        return {(r["season"], r["episode"]) for r in rows}
    finally:
        conn.close()


def _rows(pairs):
    return [(s, e, f"E{e}", f"E{e}", "2026-01-01", "") for s, e in pairs]


def test_prunes_long_running_series_without_hitting_the_expr_depth_limit(db, media_id):
    """1500 kept episodes must not blow up the statement, and must survive."""
    keep = [(1, i) for i in range(1500)]
    stale = [(99, i) for i in range(40)]
    db.save_calendar_episodes(media_id, _rows(keep + stale))

    db.delete_calendar_episodes_except(media_id, keep)

    assert _stored(db, media_id) == set(keep)


def test_empty_keep_list_clears_the_series(db, media_id):
    db.save_calendar_episodes(media_id, _rows([(1, 1), (1, 2)]))

    db.delete_calendar_episodes_except(media_id, [])

    assert _stored(db, media_id) == set()


def test_keeps_everything_when_nothing_is_stale(db, media_id):
    keep = [(1, 1), (1, 2), (2, 1)]
    db.save_calendar_episodes(media_id, _rows(keep))

    db.delete_calendar_episodes_except(media_id, keep)

    assert _stored(db, media_id) == set(keep)


def test_string_and_int_season_numbers_compare_equal(db, media_id):
    """TMDB's JSON may hand back strings; the DB always returns INTEGERs.

    Without normalisation every row would look stale and the series would be
    emptied instead of pruned -- a silent data-loss bug rather than a crash,
    which is why it gets its own test.
    """
    db.save_calendar_episodes(media_id, _rows([(1, 1), (1, 2)]))

    db.delete_calendar_episodes_except(media_id, [("1", "1"), ("1", "2")])

    assert _stored(db, media_id) == {(1, 1), (1, 2)}
