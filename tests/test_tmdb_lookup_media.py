"""lookup_media(): the public TMDB entry point filters what callers used to
filter by hand.

Only routes/search.py ever checked title_confident, and calendar_routes.py
open-coded ``found and media_type == "tv"`` in six places -- so a caller that
forgot either guard rendered TMDB's first fuzzy /search/multi hit as a real one.
"""

import pytest


@pytest.fixture
def tmdb(monkeypatch, app):
    """lookup_media with the network layer replaced and a key configured."""
    from mediaforge.web import tmdb_cache

    calls = []

    def fake_lookup(title, imdb_id, api_key, country, ui_lang="de"):
        calls.append((title, imdb_id, api_key, country, ui_lang))
        return fake_lookup.result

    fake_lookup.result = {"found": False}
    monkeypatch.setattr(tmdb_cache, "_tmdb_lookup_cached", fake_lookup)
    monkeypatch.setattr(
        tmdb_cache, "get_setting",
        lambda key, default=None: {
            "cineinfo_tmdb_api_key": "key123",
            "cineinfo_country": "DE",
        }.get(key, default),
    )
    return tmdb_cache, fake_lookup, calls


def test_returns_the_hit(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    assert mod.lookup_media("Dark")["tmdb_id"] == 1


def test_miss_returns_none_not_a_found_false_dict(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": False}
    assert mod.lookup_media("Nope") is None


def test_media_type_mismatch_is_rejected(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "movie",
                   "title_confident": True}
    assert mod.lookup_media("Dark", media_type="tv") is None
    assert mod.lookup_media("Dark", media_type="movie") is not None


def test_require_confident_rejects_a_fuzzy_match(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": False}
    assert mod.lookup_media("Dark", require_confident=True) is None
    assert mod.lookup_media("Dark") is not None  # off by default


def test_no_api_key_returns_none_without_calling_tmdb(monkeypatch, app):
    from mediaforge.web import tmdb_cache
    monkeypatch.setattr(tmdb_cache, "get_setting", lambda key, default=None: "")

    def boom(*a, **kw):
        raise AssertionError("TMDB was called without a configured key")

    monkeypatch.setattr(tmdb_cache, "_tmdb_lookup_cached", boom)
    assert tmdb_cache.lookup_media("Dark") is None
    assert tmdb_cache.is_tmdb_configured() is False


def test_empty_query_returns_none_without_calling_tmdb(tmdb):
    mod, fake, calls = tmdb
    assert mod.lookup_media("") is None
    assert mod.lookup_media(None, imdb_id="  ") is None
    assert calls == []


def test_defaults_are_resolved_from_the_settings(tmdb):
    """A module passes a title and nothing else; the key/country/language come
    from the configuration, not from the caller."""
    mod, fake, calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    mod.lookup_media("Dark")
    assert calls[-1] == ("Dark", None, "key123", "DE", "de")


def test_explicit_api_key_skips_the_setting_read(tmdb):
    """Core loops pass a pre-resolved key so a lookup per title doesn't become
    a DB read per title."""
    mod, fake, calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    mod.lookup_media("Dark", api_key="other", country="US", ui_lang="en")
    assert calls[-1] == ("Dark", None, "other", "US", "en")
