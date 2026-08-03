"""Regression tests for db.delete_calendar_episodes_except().

The function used to build one `(season = ? AND episode = ?)` OR-term per kept
episode. SQLite parses a chain of ORs into a left-deep binary tree, so the
expression depth grew with the episode count and any long-running series past
~1000 episodes aborted the whole calendar sync with:

    OperationalError: Expression tree is too large (maximum depth 1000)

Reported from a crash log at routes/calendar_routes.py::_sync_calendar_item.
The number below is deliberately well past that limit -- One Piece alone is alr-
eady over 1100 episodes, so this is an ordinary library, not a stress test.
"""

import pytest


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
