"""A cold TMDB lookup takes two requests: the search and one details call.

Details, watch providers, the age rating, trailers and recommendations used to
be five to six separate sequential requests per lookup; they now ride along on
the details call via append_to_response.
"""

import pytest

from mediaforge.web import tmdb_cache as tc


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def fake_tmdb(monkeypatch):
    calls = []
    stored = {}

    monkeypatch.setattr(tc, "get_tmdb_cache", lambda key, *a, **k: None)
    monkeypatch.setattr(tc, "set_tmdb_cache", lambda key, value: stored.__setitem__(key, value))
    monkeypatch.setattr(tc._tmdb_rl, "acquire", lambda *a, **k: None)

    def _get(url, params=None, **_kw):
        path = url.split("/3", 1)[1]
        calls.append((path, dict(params or {})))
        if path == "/search/multi":
            return _Resp({"results": [{"id": 42, "media_type": "tv"}]})
        if path == "/tv/42":
            return _Resp({
                "id": 42, "name": "Beispiel", "original_name": "Example",
                "overview": "Text", "vote_average": 7.84,
                "genres": [{"id": 1, "name": "Drama"}],
                "credits": {"cast": []}, "external_ids": {"imdb_id": "tt1"},
                "alternative_titles": {"results": []},
                "watch/providers": {"results": {"DE": {
                    "flatrate": [{"provider_name": "Netflix"}],
                    "buy": [{"provider_name": "Apple TV"}, {"provider_name": "Netflix"}],
                }}},
                "content_ratings": {"results": [{"iso_3166_1": "DE", "rating": "12"}]},
                "videos": {"results": [
                    {"site": "YouTube", "type": "Teaser", "iso_639_1": "de", "key": "teaser"},
                    {"site": "YouTube", "type": "Trailer", "iso_639_1": "en", "key": "en-trailer"},
                    {"site": "YouTube", "type": "Trailer", "iso_639_1": "de", "key": "de-trailer"},
                ]},
                "recommendations": {"results": [
                    {"id": i, "name": "Rec %d" % i, "poster_path": "/p.jpg", "vote_average": 5}
                    for i in range(10)
                ]},
            })
        raise AssertionError("unexpected TMDB request: %s" % path)

    monkeypatch.setattr(tc._rq_tmdb, "get", _get)
    return calls, stored


def test_cold_lookup_uses_two_requests(fake_tmdb):
    calls, stored = fake_tmdb
    out = tc._tmdb_lookup_cached("Beispiel", None, "key", "DE", "de")

    assert [path for path, _ in calls] == ["/search/multi", "/tv/42"]
    appended = calls[1][1]["append_to_response"].split(",")
    for part in ("credits", "external_ids", "alternative_titles", "watch/providers",
                 "content_ratings", "videos", "recommendations"):
        assert part in appended

    assert out["found"] and out["tmdb_id"] == 42 and out["media_type"] == "tv"
    assert out["providers"] == ["Netflix", "Apple TV"]
    assert out["fsk"] == "12"
    assert out["trailer_key"] == "de-trailer"
    assert [r["id"] for r in out["recommendations"]] == list(range(6))
    assert out["vote_average"] == 7.8
    # The cached raw details keep their old shape.
    for part in ("watch/providers", "content_ratings", "videos", "recommendations"):
        assert part not in out["raw_details"]
    assert "credits" in out["raw_details"]
    assert stored  # cached under the title key


def test_movie_rating_comes_from_release_dates(fake_tmdb, monkeypatch):
    calls, _stored = fake_tmdb

    def _get(url, params=None, **_kw):
        path = url.split("/3", 1)[1]
        calls.append((path, dict(params or {})))
        if path == "/search/multi":
            return _Resp({"results": [{"id": 7, "media_type": "movie"}]})
        if path == "/movie/7":
            return _Resp({
                "id": 7, "title": "Film", "genres": [],
                "release_dates": {"results": [{"iso_3166_1": "DE", "release_dates": [
                    {"certification": ""}, {"certification": "16"}]}]},
                "videos": {"results": []},
            })
        if path == "/movie/7/videos":
            # No German/English/language-less trailer: the all-language
            # fallback still runs.
            return _Resp({"results": [
                {"site": "YouTube", "type": "Trailer", "iso_639_1": "ja", "key": "ja-trailer"}]})
        raise AssertionError("unexpected TMDB request: %s" % path)

    monkeypatch.setattr(tc._rq_tmdb, "get", _get)
    out = tc._tmdb_lookup_cached("Film", None, "key", "DE", "de")
    assert "release_dates" in calls[1][1]["append_to_response"]
    assert out["fsk"] == "16"
    assert out["trailer_key"] == "ja-trailer"
    assert [path for path, _ in calls] == ["/search/multi", "/movie/7", "/movie/7/videos"]
