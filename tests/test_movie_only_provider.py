"""A movie-only third-party provider must not break the series API.

Regression test for issue #29: ``/api/series``, ``/api/seasons`` and
``/api/episodes`` supported single-film sources only through hardcoded
per-site branches (FilmPalast, Filmo, MegaKino). A provider registered by a
third-party module with just ``episode_pattern``/``episode_cls`` fell through
to the generic path, which calls ``prov.series_cls(url=url)`` -- ``None`` --
and answered every request with a 500.

The fix is structural rather than per-site (``providers.is_movie_only``), so
these tests use a fake provider that shares nothing with the built-in sites:
if they pass, every future movie-only source works too.
"""

import re

import pytest

_MOVIE_URL = "https://movies.invalid/film/test-movie"
_MOVIE_PATTERN = re.compile(r"^https?://movies\.invalid/film/[a-z0-9\-]+/?$")


class _FakeMovie:
    """The minimum a movie-only model exposes -- no network, no series."""

    def __init__(self, url, **kwargs):
        if not _MOVIE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid test movie URL: {url}")
        self.url = url
        self.title = "Test Movie"
        self.description = "A film that exists only in this test."
        self.genres = ["Drama"]
        self.image_url = "/poster.jpg"          # relative on purpose
        self.release_year = 2026
        self.available_languages = ["German Dub", "English Dub"]
        self.available_providers = ["VOE"]


@pytest.fixture()
def movie_provider():
    """Register a movie-only provider for the duration of one test."""
    from mediaforge.providers import Provider, register_provider, unregister_provider

    register_provider(
        "test-movie-item",
        Provider(
            name="TestMovies",
            episode_pattern=_MOVIE_PATTERN,
            episode_cls=_FakeMovie,
        ),
    )
    try:
        yield
    finally:
        unregister_provider("test-movie-item")


# ── The registry helpers the routes are built on ────────────────────────────
def test_is_movie_only_recognizes_the_shape(movie_provider):
    from mediaforge.providers import is_movie_only, movie_only_provider_for, resolve_provider

    assert is_movie_only(resolve_provider(_MOVIE_URL))
    assert movie_only_provider_for(_MOVIE_URL) is not None


def test_is_movie_only_says_no_to_series_providers_and_junk():
    from mediaforge.providers import movie_only_provider_for

    # A regular series provider is not movie-only...
    assert movie_only_provider_for("https://aniworld.to/anime/stream/naruto") is None
    # ...and an unknown URL must not raise, just answer "no".
    assert movie_only_provider_for("https://nothing.invalid/whatever") is None
    assert movie_only_provider_for("") is None


# ── The three routes from the report ────────────────────────────────────────
def test_api_series_returns_movie_metadata(as_user, movie_provider):
    resp = as_user("user").get("/api/series", query_string={"url": _MOVIE_URL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["title"] == "Test Movie"
    assert data["is_movie"] is True
    assert data["release_year"] == "2026"
    assert data["available_providers"] == ["VOE"]
    # A relative poster path must be resolved against the film's own host
    # before it is handed to the image proxy (which URL-encodes it).
    from urllib.parse import unquote

    assert "https://movies.invalid/poster.jpg" in unquote(data["poster_url"])


def test_api_seasons_returns_one_synthetic_season(as_user, movie_provider):
    resp = as_user("user").get("/api/seasons", query_string={"url": _MOVIE_URL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    seasons = resp.get_json()["seasons"]
    assert len(seasons) == 1
    assert seasons[0]["season_number"] == 1
    assert seasons[0]["episode_count"] == 1
    assert seasons[0]["are_movies"] is True
    assert seasons[0]["is_single_movie"] is True


def test_api_episodes_returns_the_film_as_one_episode(as_user, movie_provider):
    resp = as_user("user").get("/api/episodes", query_string={"url": _MOVIE_URL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    episodes = resp.get_json()["episodes"]
    assert len(episodes) == 1
    assert episodes[0]["episode_number"] == 1
    assert episodes[0]["season_number"] == 1
    assert episodes[0]["title_de"] == "Test Movie"
    # Multi-language movie sources must report every language they offer,
    # not a hardcoded German single value.
    assert episodes[0]["languages"] == ["German Dub", "English Dub"]


def test_queue_notification_treats_it_as_a_movie(movie_provider):
    """The download notification wording follows the provider, not a hostname."""
    from mediaforge.web.queue_worker import _is_movie_url

    assert _is_movie_url(_MOVIE_URL) is True
    assert _is_movie_url("https://aniworld.to/anime/stream/naruto/staffel-1/episode-1") is False
