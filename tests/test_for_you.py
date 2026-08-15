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
