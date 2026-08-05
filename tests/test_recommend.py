"""Recommendations built from local data only.

The interesting cases are all about *not* recommending: an empty library, a
seed with no genre information, a title the user already watched, and a
progress row whose file no longer exists. A recommender that quietly returns
something in all of those is one that fills the home page with nonsense.
"""

import pytest

from mediaforge.web import recommend


def _progress(app, username, path, position, duration, watched=0):
    from mediaforge.web.db import get_db
    with app.app_context():
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO watch_progress "
                "(username, file_path, position_seconds, duration_seconds, watched, updated_at)"
                " VALUES (?,?,?,?,?, datetime('now'))",
                (username, path, position, duration, watched))
            conn.commit()
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def clean_progress(app):
    yield
    from mediaforge.web.db import get_db
    with app.app_context():
        conn = get_db()
        try:
            conn.execute("DELETE FROM watch_progress WHERE username LIKE 'pytest%'")
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

def test_fraction_is_clamped():
    """A position past the duration is a player rounding artefact, not 140%."""
    assert recommend._fraction({"position_seconds": 60, "duration_seconds": 120}) == 0.5
    assert recommend._fraction({"position_seconds": 200, "duration_seconds": 120}) == 1.0
    assert recommend._fraction({"position_seconds": 10, "duration_seconds": 0}) == 0.0
    assert recommend._fraction({}) == 0.0


def test_watched_threshold_is_below_one():
    """Nobody watches the credits. At 1.0 every finished series would sit in
    "continue watching" forever."""
    assert 0.85 < recommend.WATCHED_FRACTION < 1.0
    assert 0 < recommend.STARTED_FRACTION < 0.1


# ---------------------------------------------------------------------------
# Continue watching
# ---------------------------------------------------------------------------

def test_nothing_watched_means_no_rows(app):
    with app.app_context():
        assert recommend.continue_watching("pytest-empty") == []
        assert recommend.personal_rows("pytest-empty") == []


def test_progress_for_a_missing_file_is_dropped(app):
    """The file was deleted or moved. A card for it cannot be played."""
    _progress(app, "pytest-gone", "/nowhere/at/all/ep01.mkv", 300, 1200)
    with app.app_context():
        assert recommend.continue_watching("pytest-gone") == []


def test_barely_started_and_nearly_finished_are_both_excluded(app, monkeypatch):
    index = {
        "/lib/a/ep01.mkv": {"title": "A", "poster": "", "series_url": "", "total_episodes": 1},
        "/lib/b/ep01.mkv": {"title": "B", "poster": "", "series_url": "", "total_episodes": 1},
        "/lib/c/ep01.mkv": {"title": "C", "poster": "", "series_url": "", "total_episodes": 1},
    }
    monkeypatch.setattr(recommend, "_library_index", lambda: index)

    _progress(app, "pytest-thresh", "/lib/a/ep01.mkv", 5, 1200)      # 0.4 %
    _progress(app, "pytest-thresh", "/lib/b/ep01.mkv", 1190, 1200)   # 99 %
    _progress(app, "pytest-thresh", "/lib/c/ep01.mkv", 600, 1200)    # 50 %

    with app.app_context():
        titles = [c["title"] for c in recommend.continue_watching("pytest-thresh")]
    assert titles == ["C"]


def test_one_card_per_series_not_per_episode(app, monkeypatch):
    """A season you are working through takes one slot, not twelve."""
    index = {
        "/lib/s/ep%02d.mkv" % n: {"title": "Same Series", "poster": "",
                                  "series_url": "", "total_episodes": 12}
        for n in range(1, 6)
    }
    monkeypatch.setattr(recommend, "_library_index", lambda: index)
    for n in range(1, 6):
        _progress(app, "pytest-dedupe", "/lib/s/ep%02d.mkv" % n, 600, 1200)

    with app.app_context():
        cards = recommend.continue_watching("pytest-dedupe")
    assert len(cards) == 1


def test_watched_flag_excludes_even_at_half(app, monkeypatch):
    """An explicit "watched" beats the fraction: the user said so."""
    index = {"/lib/w/ep01.mkv": {"title": "W", "poster": "", "series_url": "",
                                 "total_episodes": 1}}
    monkeypatch.setattr(recommend, "_library_index", lambda: index)
    _progress(app, "pytest-flag", "/lib/w/ep01.mkv", 600, 1200, watched=1)
    with app.app_context():
        assert recommend.continue_watching("pytest-flag") == []


# ---------------------------------------------------------------------------
# Because you watched
# ---------------------------------------------------------------------------

def test_no_seed_means_no_row(app, monkeypatch):
    monkeypatch.setattr(recommend, "_library_index", lambda: {})
    with app.app_context():
        assert recommend.because_you_watched("pytest-noseed") is None


def test_a_seed_without_genres_produces_nothing(app, monkeypatch):
    """Returning an empty row would render a header with nothing under it."""
    index = {"/lib/x/ep01.mkv": {"title": "X", "poster": "", "series_url": "",
                                 "total_episodes": 1}}
    monkeypatch.setattr(recommend, "_library_index", lambda: index)
    monkeypatch.setattr(recommend, "_genres_for", lambda title: set())
    _progress(app, "pytest-nogenre", "/lib/x/ep01.mkv", 1200, 1200, watched=1)
    with app.app_context():
        assert recommend.because_you_watched("pytest-nogenre") is None


def test_recommends_by_genre_overlap_and_names_its_seed(app, monkeypatch):
    index = {
        "/lib/seed/ep01.mkv": {"title": "Seed", "poster": "", "series_url": "", "total_episodes": 1},
        "/lib/near/ep01.mkv": {"title": "Near", "poster": "", "series_url": "", "total_episodes": 1},
        "/lib/far/ep01.mkv":  {"title": "Far", "poster": "", "series_url": "", "total_episodes": 1},
    }
    genres = {"Seed": {"action", "comedy"}, "Near": {"comedy"}, "Far": {"documentary"}}
    monkeypatch.setattr(recommend, "_library_index", lambda: index)
    monkeypatch.setattr(recommend, "_genres_for", lambda title: genres.get(title, set()))

    _progress(app, "pytest-genre", "/lib/seed/ep01.mkv", 1200, 1200, watched=1)

    with app.app_context():
        row = recommend.because_you_watched("pytest-genre")

    assert row is not None
    assert row["seed"] == "Seed"
    titles = [item["title"] for item in row["items"]]
    assert titles == ["Near"], "no overlap must mean no card"
    assert row["items"][0]["shared"] == ["comedy"]


def test_already_watched_titles_are_not_recommended(app, monkeypatch):
    index = {
        "/lib/seed/ep01.mkv": {"title": "Seed", "poster": "", "series_url": "", "total_episodes": 1},
        "/lib/seen/ep01.mkv": {"title": "Seen", "poster": "", "series_url": "", "total_episodes": 1},
    }
    monkeypatch.setattr(recommend, "_library_index", lambda: index)
    monkeypatch.setattr(recommend, "_genres_for", lambda title: {"action"})

    _progress(app, "pytest-seen", "/lib/seed/ep01.mkv", 1200, 1200, watched=1)
    _progress(app, "pytest-seen", "/lib/seen/ep01.mkv", 600, 1200)

    with app.app_context():
        row = recommend.because_you_watched("pytest-seen")
    assert row is None or "Seen" not in [i["title"] for i in row["items"]]


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_home_feed_carries_the_row(as_user):
    resp = as_user("user").get("/api/home-feed/personal")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "because" in body and "because_seed" in body


def test_the_row_is_in_the_default_order(as_user):
    cfg = as_user("user").get("/api/home-feed/sources").get_json()["config"]
    assert "because" in cfg["order"]
    # Behind "continue": the thing you already started beats anything a guess
    # can infer.
    assert cfg["order"].index("because") > cfg["order"].index("continue")
