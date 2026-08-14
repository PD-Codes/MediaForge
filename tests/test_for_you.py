"""The "could be for you" row.

Only the parts that can actually go wrong without anybody noticing: that a
title already in the library never gets recommended back, and that two of the
user's titles agreeing outranks one title with a great rating.
"""

import json

from mediaforge.web import recommend


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
