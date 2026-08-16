"""Everything that guesses: the "could be for you" row, its visibility
switches, and the local-data-only recommender behind both.

Merged from: test_for_you.py, test_foryou_visibility_prefs.py, test_recommend.py.
"""

import json
import pytest

from mediaforge.web import recommend


# ==========================================================================
# test_for_you.py
#
# The "could be for you" row.
# 
# Only the parts that can actually go wrong without anybody noticing: that a
# title already in the library never gets recommended back, and that two of the
# user's titles agreeing outranks one title with a great rating.
# ==========================================================================
def _entry(title, recs, genres=("Drama",)):
    return (title, {"genres": [{"name": g} for g in genres],
                    "recommendations": recs})


def _rec(tmdb_id, title, vote=5.0):
    return {"id": tmdb_id, "title": title, "poster_path": "/p.jpg",
            "vote_average": vote}


def test_owned_titles_are_never_recommended(monkeypatch):
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha", "beta"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: [
        _entry("alpha", [_rec(1, "Beta"), _rec(2, "Gamma")]),
    ])
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    out = recommend.for_you("pytest")
    assert out["configured"] is True
    assert [i["title"] for i in out["items"]] == ["Gamma"]
    assert out["items"][0]["reason_seeds"] == ["alpha"]
    assert out["items"][0]["genre"] == "Drama"
    assert out["items"][0]["poster_url"].endswith("w342/p.jpg")


def test_alias_only_owned_titles_are_excluded(monkeypatch):
    """A library title TMDB knows under an alias (see _cached_tmdb_entries)
    must still count as owned, even though `_owned_titles()` only ever
    learns the folder name ("Alpha"), not the alias ("Alias Name") that the
    recommendation itself is titled with. Regression test for exclude being
    computed before the alias merge -- that ordering let an owned title come
    back as a "for you" suggestion forever."""
    rows = [{"cache_key": "Alpha|||DE|||de",
             "data_json": json.dumps({
                 "titles": ["Alias Name"],
                 "genres": [{"name": "Drama"}],
                 "recommendations": [_rec(1, "Alias Name"), _rec(2, "Gamma")],
             })}]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    monkeypatch.setattr("mediaforge.web.db.get_db", lambda: _Conn())
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_seed_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    items = recommend.for_you("pytest")["items"]
    assert [i["title"] for i in items] == ["Gamma"], \
        "the alias of an owned title must not come back as a suggestion"


def test_owned_title_excluded_by_tmdb_id_even_with_unrelated_text(monkeypatch):
    """Text matching (owned title / its known aliases) is not the only guard
    -- an owned title's cached payload also carries its OWN tmdb_id
    (recommend.for_you's `exclude_ids`), and a recommended candidate with
    that same id must be dropped even when its displayed title shares no
    text at all with the owned title or any alias TMDB happened to record.
    Regression test: before exclude_ids existed, a recommendation could
    only ever be caught by string equality, so any localisation/region
    mismatch between "what the library folder is called" and "what this
    recommendation is titled" let an owned show straight through."""
    rows = [{"cache_key": "Alpha|||DE|||de",
             "data_json": json.dumps({
                 "tmdb_id": 1,
                 "titles": ["Only Known Alias"],
                 "genres": [{"name": "Drama"}],
                 "recommendations": [_rec(1, "Completely Different Title"), _rec(2, "Gamma")],
             })}]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    monkeypatch.setattr("mediaforge.web.db.get_db", lambda: _Conn())
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_seed_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    items = recommend.for_you("pytest")["items"]
    assert [i["title"] for i in items] == ["Gamma"], \
        "a recommendation sharing the owned title's own tmdb_id must be excluded by id"


def test_shuffle_reorders_even_when_pool_is_smaller_than_the_row_limit(monkeypatch):
    """Regression test: shuffle used to be gated on "more candidates than
    the row limit" (MAX_ROW=20), so a household with a modest library --
    the common case, well under 20 distinct "could be for you" candidates
    -- clicked Shuffle and got the exact same list back every time. The
    button must reorder any pool with more than one candidate, not only an
    overflowing one."""
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: [
        _entry("alpha", [_rec(i, f"Title {i}") for i in range(1, 6)]),
    ])
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)
    monkeypatch.setattr(recommend.random, "sample", lambda pool, k: list(reversed(pool))[:k])

    # 5 candidates, well under MAX_ROW=20 -- exactly the case the old
    # `len(pool) > limit` guard silently skipped.
    unshuffled = [i["title"] for i in recommend.for_you("pytest", shuffle=False)["items"]]
    shuffled = [i["title"] for i in recommend.for_you("pytest", shuffle=True)["items"]]
    assert shuffled != unshuffled
    assert shuffled == list(reversed(unshuffled))


def test_two_seeds_beat_one_high_rating(monkeypatch):
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha", "beta"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: [
        _entry("alpha", [_rec(1, "Twice", 3.0), _rec(2, "Once", 9.9)]),
        _entry("beta", [_rec(1, "Twice", 3.0)]),
    ])
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    items = recommend.for_you("pytest")["items"]
    assert [i["title"] for i in items] == ["Twice", "Once"]
    assert items[0]["score"] > items[1]["score"]
    assert items[0]["reason_seeds"] == ["alpha", "beta"]


def test_no_api_key_means_no_work(monkeypatch):
    """The gate: not configured must not even look at the library."""
    def boom():
        raise AssertionError("library read despite missing TMDB key")
    monkeypatch.setattr(recommend, "_owned_titles", boom)
    monkeypatch.setattr("mediaforge.web.db.get_setting", lambda k, d="": "")

    out = recommend.for_you("pytest")
    assert out == {"configured": False, "items": [], "hero": [],
                   "generated_at": out["generated_at"]}


def test_cache_key_scan_matches_on_title(monkeypatch):
    """tmdb_cache keys are '<title>|||<country>|||<lang>'; IMDB-keyed rows
    must be skipped rather than matched by accident."""
    rows = [{"cache_key": "Alpha|||DE|||de",
             "data_json": json.dumps({"titles": ["Alpha JP"],
                                      "recommendations": [_rec(1, "X")]})},
            {"cache_key": "tt123|||DE|||de", "data_json": json.dumps({})}]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    monkeypatch.setattr("mediaforge.web.db.get_db", lambda: _Conn())
    owned = {"alpha"}
    got = recommend._cached_tmdb_entries(owned)
    assert [t for t, _ in got] == ["Alpha"]     # display casing, not the key
    assert "alpha jp" in owned      # aliases join the owned set


def test_cache_row_matches_via_alias_not_just_cache_key(monkeypatch):
    """Regression test for the "Reincarnated as a Sword" bug: two providers
    stored the show under folder names that don't match EACH OTHER or the
    tmdb_cache key (a Japanese-script title from one provider, a romaji
    title from the library's own owned-titles set), while the cache row
    itself is keyed by yet a third, English string. Before this fix,
    _cached_tmdb_entries only matched when the CACHE KEY equalled an owned
    title -- an owned title that only appears in the row's alias list never
    triggered the match, so the English cache-key title (which is exactly
    what a recommendation displays) kept coming back as a suggestion even
    though the show was demonstrably already in the library."""
    rows = [{"cache_key": "Reincarnated as a Sword|||DE|||de",
             "data_json": json.dumps({
                 "titles": ["Tensei Shitara Ken Deshita", "転生したら剣でした"],
                 "genres": [{"name": "Drama"}],
                 "recommendations": [_rec(1, "Reincarnated as a Sword"), _rec(2, "Gamma")],
             })}]

    class _Conn:
        def execute(self, *a):
            return self

        def fetchall(self):
            return rows

        def close(self):
            pass

    monkeypatch.setattr("mediaforge.web.db.get_db", lambda: _Conn())
    # The library owns the romaji folder, not the English cache-key title.
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"tensei shitara ken deshita"})
    monkeypatch.setattr(recommend, "_seed_titles", lambda: {"tensei shitara ken deshita"})
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    items = recommend.for_you("pytest")["items"]
    assert [i["title"] for i in items] == ["Gamma"], \
        "an owned title matched only via alias must still exclude the cache key's own title"


def _capture_topup(monkeypatch):
    """Swap the background top-up for a recorder. The real one spawns a
    thread and hits the network; what matters here is what it is HANDED."""
    seen = []
    monkeypatch.setattr(recommend, "_schedule_recommendation_topup",
                        lambda titles, key, country, lang: seen.append(list(titles)))
    return seen


def test_topup_is_bounded_and_prefers_never_enriched(monkeypatch):
    """Thin pool -> repair, but never more than FORYOU_TOPUP_MAX titles, and
    rows that were never enriched (no key) before rows TMDB already answered
    with nothing (empty list)."""
    entries = [("empty%s" % i, {"recommendations": []}) for i in range(10)]
    entries += [("never%s" % i, {}) for i in range(10)]
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"x"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: entries)
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)
    seen = _capture_topup(monkeypatch)

    recommend.for_you("pytest")
    assert len(seen) == 1
    assert len(seen[0]) == recommend.FORYOU_TOPUP_MAX
    assert all(t.startswith("never") for t in seen[0])


def test_topup_does_not_run_without_an_api_key(monkeypatch):
    monkeypatch.setattr("mediaforge.web.db.get_setting", lambda k, d="": "")
    seen = _capture_topup(monkeypatch)
    recommend.for_you("pytest")
    assert seen == []


def test_topup_stays_away_when_the_pool_is_healthy(monkeypatch):
    entries = [("alpha", {"recommendations": [_rec(i, "R%s" % i)
                                              for i in range(1, recommend.MAX_ROW + 1)]}),
               ("thin", {})]
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"alpha"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: entries)
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)
    seen = _capture_topup(monkeypatch)

    recommend.for_you("pytest")
    assert seen == []


def test_reason_seed_keeps_its_original_casing(monkeypatch):
    """Seeds are rendered verbatim, so the normalised form must never leak."""
    monkeypatch.setattr(recommend, "_owned_titles", lambda: {"steins;gate"})
    monkeypatch.setattr(recommend, "_cached_tmdb_entries", lambda owned: [
        _entry("Steins;Gate", [_rec(1, "Gamma")]),
    ])
    monkeypatch.setattr(recommend, "_hero_items", lambda items, key: [])
    monkeypatch.setattr("mediaforge.web.db.get_setting",
                        lambda k, d="": "key" if k == "cineinfo_tmdb_api_key" else d)

    assert recommend.for_you("pytest")["items"][0]["reason_seeds"] == ["Steins;Gate"]


# ==========================================================================
# test_foryou_visibility_prefs.py
#
# 'Could be for you' visibility, split into hero banner and rail.
# 
# Two independent per-account prefs (foryou_hero_hidden, foryou_hidden -- the
# latter kept its original name/meaning, now scoped to just the rail, see
# static/home_foryou.js's heroHidden()/railHidden()) plus their instance-default
# counterparts an admin sets under Settings -> Start Page.
# ==========================================================================
def test_foryou_hero_hidden_pref_accepts_bool_strings_rejects_junk(as_user):
    ok = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "1"})
    assert ok.status_code == 200
    ok2 = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "0"})
    assert ok2.status_code == 200
    bad = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "maybe"})
    assert bad.status_code >= 400


def test_foryou_rail_pref_still_works_unchanged(as_user):
    """foryou_hidden predates the hero/rail split -- its key and validator
    must not have moved under existing accounts."""
    ok = as_user("user").post("/api/user/preferences", json={"foryou_hidden": "1"})
    assert ok.status_code == 200


def test_foryou_instance_defaults_round_trip(as_user):
    admin = as_user("admin")
    try:
        resp = admin.put("/api/settings", json={
            "foryou_hero_hidden_default": "1",
            "foryou_hidden_default": "1",
        })
        assert resp.status_code == 200
        data = admin.get("/api/settings").get_json()
        assert data["foryou_hero_hidden_default"] == "1"
        assert data["foryou_hidden_default"] == "1"
    finally:
        admin.put("/api/settings", json={
            "foryou_hero_hidden_default": "",
            "foryou_hidden_default": "",
        })


def test_foryou_instance_default_junk_is_dropped_not_stored(as_user):
    admin = as_user("admin")
    admin.put("/api/settings", json={"foryou_hero_hidden_default": "yes-please"})
    data = admin.get("/api/settings").get_json()
    # An unrecognised value falls back to "" rather than being stored as-is
    # (see routes/settings.py's PUT handler) -- the client only ever sends
    # "", "0" or "1", so anything else is a fresher-request race or a
    # malformed request, not a value MediaForge invented and needs to keep.
    assert data["foryou_hero_hidden_default"] == ""


# ==========================================================================
# test_recommend.py
#
# Recommendations built from local data only.
# 
# The interesting cases are all about *not* recommending: an empty library, a
# seed with no genre information, a title the user already watched, and a
# progress row whose file no longer exists. A recommender that quietly returns
# something in all of those is one that fills the home page with nonsense.
# ==========================================================================
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
