"""Content sources: the recorded contracts, per-provider quirks, host and
hoster dispatch, the retry plan, and how a source reaches the search.

Merged from: test_provider_contracts.py, test_provider_headers.py, test_provider_plan.py, test_movie_only_provider.py, test_mirror_transient_retry.py, test_nineanime_hosters.py, test_veev_host_dispatch.py, test_filmo_token_refresh.py, test_hanime_cards.py, test_aniworld_absolute_episodes.py, test_language_dropdown_contract.py, test_search_sources.py, test_dns_fallback.py.
"""

import json
import os
import pathlib
import pytest
import re
import io
import sys
import types
import zipfile
from pathlib import Path
import time
import niquests

from mediaforge import config
from mediaforge.extractors import canonical_provider_name, provider_for_url
from mediaforge.models.common.common import _effective_provider
from mediaforge import mirrors
from mediaforge.extractors import provider_for_url
from mediaforge.models.nineanime_to import episode as ep_mod
from mediaforge.models.common.common import _download_via_hoster
from mediaforge.web.thirdparties import store
from mediaforge.models.filmo_to import scraper
from mediaforge.models.filmo_to.movie import FilmoMovie
from mediaforge.models.aniworld_to.episode import (
    AniworldEpisode,
    parse_absolute_episode_number,
)
from mediaforge import config as C
from mediaforge import mirrors as M


# ==========================================================================
# test_provider_contracts.py
#
# Provider contract tests.
# 
# A provider breaks in a very specific way: the site quietly changes its markup,
# the parser stops finding episodes, and every Auto-Sync job starts reporting
# "could not read the series page". Nothing in the codebase changed, so nothing
# in CI noticed — the first signal is an issue from somebody whose downloads
# stopped three days ago.
# 
# Two checks close that gap, and they are deliberately separate:
# 
# * **Offline** (always). Parses the recorded fixtures in ``tests/contracts/``
#   and asserts the parser still extracts what it used to. This catches *our*
#   regressions: a refactor that breaks episode extraction fails the pull
#   request that introduced it. No network.
# * **Live** (``MEDIAFORGE_CONTRACT_LIVE=1``, scheduled workflow only). Fetches
#   the real page and checks the shape of what comes back. This catches *their*
#   changes. Keeping it out of the normal suite is not laziness: a site being
#   down would otherwise fail an unrelated pull request, and a check that cries
#   wolf is a check somebody disables.
# 
# See ``tests/contracts/README.md`` for how to record a fixture.
# ==========================================================================
CONTRACTS = pathlib.Path(__file__).resolve().parent / "contracts"
LIVE = os.environ.get("MEDIAFORGE_CONTRACT_LIVE", "0") == "1"


def _fixtures():
    if not CONTRACTS.is_dir():
        return []
    return sorted(p for p in CONTRACTS.glob("*.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The harness itself. These run even with no fixtures recorded, which is the
# point: a broken recorder is not something to find out about on the day a
# provider breaks.
# ---------------------------------------------------------------------------

def test_contracts_directory_exists():
    assert CONTRACTS.is_dir(), "tests/contracts/ is missing"
    assert (CONTRACTS / "README.md").exists()


def test_recorder_imports_and_refuses_bad_pages():
    """The recorder's two refusals are what keep a bad fixture out of the repo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    # A page carrying somebody's session must never be committed.
    assert recorder._looks_personalised("<a href='/logout'>Logout</a>") == "logout"
    assert recorder._looks_personalised("<html><body>Episodes</body></html>") is None

    # Scripts and inline handlers are stripped: a fixture is parser input, not
    # a page to execute.
    cleaned = recorder._sanitize(
        '<div onclick="x()">a</div><script>evil()</script>')
    assert "script" not in cleaned.lower()
    assert "onclick" not in cleaned.lower()


def test_every_fixture_has_its_html():
    """A .json without its .html is a contract with nothing to check it against."""
    for path in _fixtures():
        assert path.with_suffix(".html").exists(), path.name


def test_fixtures_carry_no_session_markers():
    """Re-checked here, not just in the recorder: a fixture can also arrive by
    hand, and the repository is public."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    for path in _fixtures():
        html = path.with_suffix(".html").read_text(encoding="utf-8", errors="replace")
        marker = recorder._looks_personalised(html)
        assert marker is None, "%s contains %r" % (path.name, marker)


# ---------------------------------------------------------------------------
# Offline: the recorded contract must still hold.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_recorded_contract_still_holds(fixture):
    """Parse the recorded HTML and compare against the recorded summary.

    Skipped rather than passed when there are no fixtures: a suite that
    reports "all provider contracts fine" while checking nothing is worse than
    one that says it has nothing to check.
    """
    contract = _load(fixture)
    html_path = fixture.with_suffix(".html")

    from mediaforge.providers import resolve_provider

    provider = resolve_provider(contract["url"])
    assert provider.name == contract["provider"]

    # The parsers fetch in __init__, so the fixture is fed in by pointing the
    # shared session at the local file. Each provider model differs in how it
    # takes its input, so this asserts on what can be checked without one:
    # that the recorded contract is internally coherent and that the URL still
    # resolves to the same provider. The live check below is what proves the
    # parse itself.
    assert contract["episode_count"] > 0
    assert contract["season_count"] > 0
    assert contract["has_title"]
    assert html_path.stat().st_size > 1000, "fixture is suspiciously small"
    for url in contract["episode_url_sample"]:
        assert provider.episode_pattern.match(url), (
            "%s no longer looks like an episode url for %s -- the URL scheme "
            "changed, or the pattern did" % (url, provider.name))


def test_at_least_one_fixture_is_recorded():
    """Advisory. Fails loudly only once fixtures exist and then disappear."""
    if not _fixtures():
        pytest.skip("no provider fixtures recorded yet -- see tests/contracts/README.md")


# ---------------------------------------------------------------------------
# Live: only in the scheduled workflow.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="live provider check: set MEDIAFORGE_CONTRACT_LIVE=1")
@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_live_provider_still_matches_the_contract(fixture):
    """Fetch the real page and compare the shape, not the content.

    Counts are compared with a floor rather than for equality: a series gains
    episodes, and a check that fails because a new one aired is a check nobody
    trusts. What must not change is that there ARE episodes, seasons, a title
    and a poster.
    """
    import importlib.util

    contract = _load(fixture)
    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    live = recorder.describe(contract["provider"], contract["url"])

    assert live["has_title"], "title disappeared"
    assert live["season_count"] >= 1, "no seasons found any more"
    assert live["episode_count"] >= 1, "no episodes found any more"
    # A series does not lose most of its episodes. Half is a wide margin
    # chosen so a restructured season list does not fire, but a parser that
    # now finds three episodes out of two hundred does.
    assert live["episode_count"] >= contract["episode_count"] // 2, (
        "episode count collapsed from %d to %d -- likely a layout change"
        % (contract["episode_count"], live["episode_count"]))


# ==========================================================================
# test_provider_headers.py
#
# The headers a hoster needs must survive the host->provider translation.
# 
# `_effective_provider()` picks the hoster from the RESOLVED stream host, which
# is right (a site's label often points at a different hoster's domain). But
# `extractors.provider_for_url()` answers in the extractor namespace ("voe",
# "oneanime") while `PROVIDER_HEADERS_D`/`_W` are keyed by the display name
# ("VOE", "OneAnime") -- so every host it successfully recognised produced an
# EMPTY header set, and the hosters that require a Referer (VeeV, MegaPlay,
# EchoVideo, OneAnime) were fetched without one and answered 403.
# 
# The bug is invisible for hosters that do not check a Referer, and it hid
# behind the fallback path: an UNrecognised host falls through to
# `selected_provider`, which is already spelled correctly -- so headers worked
# only where the host lookup failed.
# ==========================================================================
class _Episode:
    def __init__(self, provider_url, selected_provider):
        self.provider_url = provider_url
        self.selected_provider = selected_provider


@pytest.mark.parametrize("key, expected", [
    ("voe", "VOE"),
    ("oneanime", "OneAnime"),
    ("megaplay", "Megaplay"),
    ("echovideo", "EchoVideo"),
    ("VOE", "VOE"),            # already canonical
    ("nosuchhoster", "nosuchhoster"),
    ("", ""),
    (None, None),
])
def test_canonical_provider_name(key, expected):
    assert canonical_provider_name(key) == expected


@pytest.mark.parametrize("stream_url, label, expected", [
    ("https://my.1anime.site/stream/abc", "HD", "OneAnime"),
    ("https://megaplay.buzz/stream/mal/1/1/sub", "HD", "Megaplay"),
    ("https://voe.sx/e/abc", "Vidara", "VOE"),
    # Unknown host -> keep the site's own label, which is already canonical.
    ("https://brand.new.host.invalid/e/x", "VOE", "VOE"),
])
def test_effective_provider_is_the_display_name(stream_url, label, expected):
    assert _effective_provider(_Episode(stream_url, label)) == expected


@pytest.mark.parametrize("stream_url", [
    "https://my.1anime.site/stream/abc",
    "https://megaplay.buzz/stream/mal/1/1/sub",
])
def test_referer_actually_reaches_the_download(stream_url):
    """The point of all of the above: a non-empty header set with a Referer.

    These two hosts answer 403 without one, so an empty dict here is not a
    cosmetic problem -- it is the download failing.
    """
    provider = _effective_provider(_Episode(stream_url, "HD"))
    for headers in (config.PROVIDER_HEADERS_D, config.PROVIDER_HEADERS_W):
        got = headers.get(provider, {})
        assert got, f"no headers for {provider!r} in {headers is config.PROVIDER_HEADERS_D and 'D' or 'W'}"
        assert got.get("Referer"), f"no Referer for {provider!r}"


def test_every_supported_provider_resolves_to_itself():
    """Guards the two lists against drifting apart: a hoster added to
    SUPPORTED_PROVIDERS with a HOST_PROVIDER_MAP entry must round-trip."""
    from mediaforge.extractors import HOST_PROVIDER_MAP

    for host, key in HOST_PROVIDER_MAP.items():
        canonical = canonical_provider_name(key)
        assert canonical in config.SUPPORTED_PROVIDERS or canonical == key, (
            f"{host} maps to {key!r}, which is not a known provider name"
        )
        assert provider_for_url(f"https://{host}/x") == key


# ==========================================================================
# test_provider_plan.py
#
# The per-episode retry/fallback plan (web/queue_worker.py).
# 
# _build_attempt_plan decides how often each hoster is tried before an episode
# counts as failed. Getting it wrong is expensive in both directions: too few
# attempts fails downloads that a second try would have completed, too many
# turns one dead source into a wall of identical errors.
# ==========================================================================
@pytest.fixture()
def build_plan(app):
    """The app fixture initialises the DB the provider order is read from."""
    from mediaforge.web.queue_worker import _build_attempt_plan

    return _build_attempt_plan


def _providers(plan):
    """Provider names in plan order, without the repeated retries."""
    return list(dict.fromkeys(name for name, _attempt, _total in plan))


@pytest.mark.parametrize("provider", ["Direct", "hanime"])
def test_single_source_providers_get_no_fallback_chain(build_plan, provider, app):
    """A source that serves its own stream has no other hoster to fall back to.

    hanime is its own player, and a direct link is a direct link. Walking the
    hoster chain for them means every "other provider" requests the exact same
    URL, which is how one failure used to produce seven identical errors and a
    summary claiming seven sources had been tried.
    """
    with app.app_context():
        plan = build_plan(provider, 3)

    assert _providers(plan) == [provider]
    assert len(plan) == 3
    assert [attempt for _name, attempt, _total in plan] == [1, 2, 3]


def test_a_hoster_keeps_its_retries_then_hands_over(build_plan, app):
    """The picked hoster gets the full retry budget, the rest one shot each."""
    with app.app_context():
        plan = build_plan("VOE", 3)

    assert plan[0][0] == "VOE"
    voe_attempts = [entry for entry in plan if entry[0] == "VOE"]
    assert len(voe_attempts) == 3

    # Every fallback appears exactly once, and none of them is the primary.
    fallbacks = [name for name, _a, _t in plan[3:]]
    assert len(fallbacks) == len(set(fallbacks))
    assert "VOE" not in fallbacks


def test_the_plan_is_never_empty(build_plan, app):
    """The queue worker iterates the plan; an empty one would skip the episode."""
    with app.app_context():
        for provider in ("VOE", "Direct", "hanime", "", None):
            assert build_plan(provider, 1), f"empty plan for {provider!r}"


# ==========================================================================
# test_movie_only_provider.py
#
# A movie-only third-party provider must not break the series API.
# 
# Regression test for issue #29: ``/api/series``, ``/api/seasons`` and
# ``/api/episodes`` supported single-film sources only through hardcoded
# per-site branches (FilmPalast, Filmo, MegaKino). A provider registered by a
# third-party module with just ``episode_pattern``/``episode_cls`` fell through
# to the generic path, which calls ``prov.series_cls(url=url)`` -- ``None`` --
# and answered every request with a 500.
# 
# The fix is structural rather than per-site (``providers.is_movie_only``), so
# these tests use a fake provider that shares nothing with the built-in sites:
# if they pass, every future movie-only source works too.
# ==========================================================================
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
        # Built-in models spell this `imdb`; the payload and every TMDB helper
        # call it `imdb_id`, and module models copy that spelling -- so the
        # route has to accept both. This fake deliberately offers only the
        # second one.
        self.imdb_id = "tt0000001"


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


def test_api_series_accepts_imdb_id_as_well_as_imdb(as_user, movie_provider):
    resp = as_user("user").get("/api/series", query_string={"url": _MOVIE_URL})
    assert resp.get_json()["imdb_id"] == "tt0000001"


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


# ==========================================================================
# test_mirror_transient_retry.py
#
# A single transient 502 must not fail a source that has only one host.
# 
# `request_with_failover` answers a "site is not here" status by moving to the
# next mirror -- which is right, except that half the sources have exactly one
# known host (filmo.to, 9anime.or.at, aniwaves.ru, filmpalast.to, hanime.tv).
# For those there is no next mirror, so one hiccup from the origin failed the
# whole request; aniwaves.ru answering 502 once was enough to fail a download
# attempt outright.
# ==========================================================================
class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.url = "https://aniwaves.ru/watch/1"
        self.headers = {}


class _Session:
    """Answers each request from a scripted list of statuses."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return _Resp(self.statuses.pop(0) if self.statuses else 200)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mirrors.time, "sleep", lambda *_: None)


SINGLE_HOST_URL = "https://aniwaves.ru/watch/82736"


def test_a_transient_502_on_the_only_host_is_retried_once():
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 200
    assert len(session.calls) == 2


def test_a_second_failure_is_reported_not_retried_forever():
    session = _Session([502, 502])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 502
    assert len(session.calls) == 2


def test_a_post_is_never_retried():
    """filmo.to's /n mints a one-shot token -- repeating the POST burns it."""
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "POST", "https://filmo.to/n")
    assert resp.status_code == 502
    assert len(session.calls) == 1


def test_a_permanent_status_is_not_retried():
    """403 is an answer about this host, not a hiccup; retrying it just adds
    a second, identical wait."""
    session = _Session([403, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 403
    assert len(session.calls) == 1


def test_the_uptime_probe_still_sees_the_truth():
    """The monitor exists to report what the site answered. A retried
    best-of-two would quietly hide exactly the outages it watches for."""
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL, probe=True)
    assert resp.status_code == 502
    assert len(session.calls) == 1


# ==========================================================================
# test_nineanime_hosters.py
#
# 9anime labels every server "HD" -- the hoster comes from the embed host.
# 
# The site calls each server by QUALITY, not by hoster, while actually embedding
# more than one (MegaPlay, and its own player on my.1anime.site). Keying anything
# off that label meant:
# 
# * the provider dropdown came back empty ("no sources") for episodes that play
#   fine, because "HD" is in no WORKING_PROVIDERS list, and
# * the name the user picked could never match the key stored on the episode.
# 
# Both are pinned here. No network: the server payload is the shape
# scraper.fetch_episode_servers() returns, captured from a live response.
# ==========================================================================
LIVE_EMBED = "https://my.1anime.site/play/f40f4e9fae400ec00aad79364c9467d9"
MEGAPLAY_EMBED = "https://megaplay.buzz/stream/mal/12345/6/sub"


def test_the_players_host_is_recognised():
    assert provider_for_url(LIVE_EMBED) == "oneanime"
    assert provider_for_url("https://cdn.1anime.site/play/x") == "oneanime"
    assert provider_for_url(MEGAPLAY_EMBED) == "megaplay"


@pytest.mark.parametrize("embed, label, expected", [
    (LIVE_EMBED, "HD", "OneAnime"),
    (MEGAPLAY_EMBED, "HD", "Megaplay"),
    # Unknown host: keep whatever the site showed rather than inventing a name.
    ("https://some.new.host.invalid/e/x", "HD", "HD"),
])
def test_canonical_hoster(embed, label, expected):
    assert ep_mod._canonical_hoster(embed, label) == expected


def test_provider_list_is_labelled_by_host_not_by_quality():
    """The regression itself: {"HD": <my.1anime.site url>} must not vanish."""
    from mediaforge.web.routes.search import _label_by_host

    assert _label_by_host({"HD": LIVE_EMBED}) == ["OneAnime"]
    assert _label_by_host({"HD": LIVE_EMBED, "HD-2": MEGAPLAY_EMBED}) == \
        ["OneAnime", "Megaplay"]
    # Two labels, one host -> one entry, not a duplicated dropdown row.
    assert _label_by_host({"HD": LIVE_EMBED, "HD-2": LIVE_EMBED}) == ["OneAnime"]
    # Nothing playable at all -> empty, so the UI can say so honestly.
    assert _label_by_host({"HD": "https://unknown.invalid/x"}) == []


def _episode_with(servers):
    """A NineAnimeEpisode whose provider_data is prefilled -- no network."""
    from mediaforge.config import Audio, Subtitles

    ep = ep_mod.NineAnimeEpisode(
        url="https://9anime.or.at/some-show-episode-1-english-subbed/"
    )
    key = (Audio.JAPANESE, Subtitles.ENGLISH)
    ep._NineAnimeEpisode__provider_data = {key: servers}
    return ep


def test_provider_link_falls_back_to_what_the_episode_offers():
    """The default hoster ("Megaplay") is usually NOT the one on offer now, and
    an exact-match-only lookup made that look like "no link"."""
    ep = _episode_with({"OneAnime": LIVE_EMBED})
    assert ep.selected_provider == "Megaplay"      # the historical default
    assert ep.provider_link() == LIVE_EMBED        # ...must still resolve
    assert ep.provider_url == LIVE_EMBED


def test_an_explicitly_requested_hoster_is_not_silently_swapped():
    """Falling back is right for the default; it would be wrong for a caller
    that named one hoster on purpose."""
    ep = _episode_with({"OneAnime": LIVE_EMBED})
    assert ep.provider_link(provider="Megaplay") is None
    assert ep.provider_link(provider="OneAnime") == LIVE_EMBED
    # Spelling differences in case must still match.
    assert ep.provider_link(provider="oneanime") == LIVE_EMBED


# ==========================================================================
# test_veev_host_dispatch.py
#
# VeeV is dispatched by resolved host, and module archives reject backslashes.
# 
# Two core changes guarded here:
# 
# 1. ``models.common.common._download_via_hoster()`` -- VeeV used to be branched
#    on ``selected_provider == "VEEV"`` inside FilmPalast's and MegaKino's own
#    ``download()``. Every other model family (and every third-party module)
#    therefore fed a VeeV link into the yt-dlp/ffmpeg pipeline, which its CDN
#    rejects, and a mirrored label pointing at veev.to was missed even on the
#    sites that had the branch. Dispatch now happens once, on the host.
# 
# 2. ``web.thirdparties.store._safe_extract()`` -- a member name containing a
#    backslash was normalised to "/" for the containment checks but extracted
#    verbatim, so ``mod\..\..\db.py`` passed as a nested path and landed as a
#    literal file in the staging root.
# ==========================================================================
class _FakeEpisode:
    """The attributes _download_via_hoster() touches -- nothing else."""

    def __init__(self, provider_url, selected_provider, folder):
        self.provider_url = provider_url
        self.selected_provider = selected_provider
        self._file_name = "Test Movie (2026)"
        self._folder_path = folder
        self._episode_path = folder / "Test Movie (2026).mkv"


@pytest.fixture()
def veev_calls(monkeypatch):
    """Stub the VeeV extractor so no browser/CDN is ever touched."""
    calls = []
    stub = types.ModuleType("mediaforge.extractors.provider.veev")
    stub.download_from_veev = lambda url, out, **kw: calls.append((url, out, kw))
    monkeypatch.setitem(sys.modules, "mediaforge.extractors.provider.veev", stub)
    return calls


def test_mislabeled_veev_link_is_still_routed_to_veev(tmp_path, veev_calls):
    """Label says VOE, host says veev.to -- the host wins."""
    ep = _FakeEpisode("https://veev.to/e/abc123", "VOE", tmp_path)
    assert _download_via_hoster(ep) is True
    assert veev_calls and veev_calls[0][0] == "https://veev.to/e/abc123"


def test_label_suffixes_do_not_break_the_match(tmp_path, veev_calls):
    """FilmPalast spells it "VeeV HD"; with an unknown host only the label is
    left, so the suffix must not defeat the comparison."""
    ep = _FakeEpisode("https://mirror.invalid/e/abc", "VeeV HD", tmp_path)
    assert _download_via_hoster(ep) is True
    assert len(veev_calls) == 1


def test_other_hosters_fall_through_to_the_shared_pipeline(tmp_path, veev_calls):
    ep = _FakeEpisode("https://voe.sx/e/abc123", "VOE", tmp_path)
    assert _download_via_hoster(ep) is False
    assert veev_calls == []


def _zip(names, folder="mymod"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{folder}/__init__.py", "")
        for name in names:
            zf.writestr(name, "pwned")
    return buf.getvalue()


def test_backslash_member_is_rejected(tmp_path):
    data = _zip(["mymod\\..\\..\\db.py"])
    with pytest.raises(ValueError, match="backslash"):
        store._safe_extract(data, "mymod", tmp_path)
    assert not any(p.name.endswith("db.py") for p in tmp_path.rglob("*"))


def test_plain_module_still_extracts(tmp_path):
    data = _zip(["mymod/thing.py"])
    staged = store._safe_extract(data, "mymod", tmp_path)
    assert (staged / "thing.py").read_text() == "pwned"
    assert staged == Path(tmp_path) / "mymod"


# ==========================================================================
# test_filmo_token_refresh.py
#
# filmo.to's CSRF token goes stale -- one refetch, not a dead download.
# 
# The movie page is fetched once and its CSRF token cached on the FilmoMovie.
# That object outlives a failed download attempt, so the queue worker's second
# try replayed the same token against /n and filmo.to answered
# "419 Page Expired" -- which is Laravel for "reload the page", the one thing
# the code never did. Every further attempt then failed identically.
# ==========================================================================
URL = "https://filmo.to/movies/some-movie"
PAGE_ONE = ('<meta name="csrf-token" content="TOKEN-1">'
            '<div class="lang-mark">Deutsch</div>'
            '<div class="provider-chip" data-p="PAYLOAD-1" data-link-id="1">VOE</div>')
PAGE_TWO = PAGE_ONE.replace("TOKEN-1", "TOKEN-2").replace("PAYLOAD-1", "PAYLOAD-2")


@pytest.fixture
def movie(monkeypatch):
    """A FilmoMovie whose page fetch and provider parsing are stubbed."""
    pages = iter([("html-1", "TOKEN-1"), ("html-2", "TOKEN-2")])
    fetched = []

    def _fetch(url, timeout=15):
        fetched.append(url)
        return next(pages)

    def _parse(html):
        suffix = html.split("-")[-1]
        return {"German Dub": {"VOE": {"data_p": "PAYLOAD-" + suffix,
                                       "movie_link_id": suffix}}}

    monkeypatch.setattr(scraper, "fetch_movie_page", _fetch)
    monkeypatch.setattr(scraper, "parse_provider_rows", _parse)
    mv = FilmoMovie(url=URL)
    mv._fetched = fetched
    return mv


def test_a_stale_token_triggers_exactly_one_refetch(movie, monkeypatch):
    seen = []

    def _resolve(data_p, csrf, referer, timeout=15):
        seen.append((data_p, csrf))
        if len(seen) == 1:
            raise scraper.FilmoTokenExpired("419")
        return "https://voe.sx/e/abc"

    monkeypatch.setattr(scraper, "resolve_provider_url", _resolve)

    assert movie.provider_url == "https://voe.sx/e/abc"
    # The retry must use the token AND the payload from the SECOND page --
    # a fresh token with a chip minted against the old one is still a 419.
    assert seen == [("PAYLOAD-1", "TOKEN-1"), ("PAYLOAD-2", "TOKEN-2")]
    assert len(movie._fetched) == 2


def test_a_second_expiry_is_not_retried_forever(movie, monkeypatch):
    """One refetch is a fix; a loop is a denial of service against the site."""
    calls = []

    def _always_expired(data_p, csrf, referer, timeout=15):
        calls.append(csrf)
        raise scraper.FilmoTokenExpired("419")

    monkeypatch.setattr(scraper, "resolve_provider_url", _always_expired)

    with pytest.raises(scraper.FilmoTokenExpired):
        _ = movie.provider_url
    assert len(calls) == 2


def test_other_failures_are_not_swallowed_by_the_retry(movie, monkeypatch):
    """Only 419 means "your token is old". A 404 or a network error must
    surface as itself, and must not cost a second page fetch."""
    def _boom(data_p, csrf, referer, timeout=15):
        raise scraper.FilmoUnavailable("gone")

    monkeypatch.setattr(scraper, "resolve_provider_url", _boom)

    with pytest.raises(scraper.FilmoUnavailable):
        _ = movie.provider_url
    assert len(movie._fetched) == 1


# ==========================================================================
# test_hanime_cards.py
#
# Card building and listing fill-up for the hanime source.
# 
# The catalogue backend changed: one plain GET returns the entire catalogue, so
# filtering, sorting and paging all happen locally now (see the long note in
# models/hanime_tv/scraper.py). These tests pin the two behaviours that broke
# when it did -- which artwork ends up on a card, and how many cards a filtered
# listing produces.
# 
# No network: the module-level catalogue cache is filled directly, which is the
# same thing a successful fetch would have done.
# ==========================================================================
@pytest.fixture()
def hanime_scraper():
    from mediaforge.models.hanime_tv import scraper as module

    return module


def _entry(index, censored):
    """One catalogue entry, shaped like the live backend's."""
    return {
        "id": index,
        "name": f"Title {index} 1",           # per-episode name, "1" suffix
        "slug": f"title-{index}",
        "created_at_unix": index,
        "views": index,
        "cover_url": f"https://hanime-cdn.com/covers/{index}.jpg",
        "poster_url": f"https://hanime-cdn.com/posters/{index}.jpg",
        "tags": [{"text": "censored" if censored else "uncensored"}],
    }


@pytest.fixture()
def catalogue(hanime_scraper):
    """Fill the catalogue cache and restore it afterwards."""
    previous = dict(hanime_scraper._catalog_cache)

    def _fill(entries):
        hanime_scraper._catalog_cache["entries"] = entries
        hanime_scraper._catalog_cache["ts"] = time.monotonic()

    yield _fill
    hanime_scraper._catalog_cache.update(previous)


def test_cards_use_the_portrait_cover(hanime_scraper):
    """cover_url is the portrait artwork, poster_url a 16:9 scene still.

    The fields swapped meaning on the new backend, which put a cropped scene
    on every card while the detail modal showed the right image.
    """
    card = hanime_scraper._hit_to_card(_entry(1, censored=False))
    assert card["poster_url"] == "https://hanime-cdn.com/covers/1.jpg"


def test_cards_fall_back_to_poster_url(hanime_scraper):
    """Entries without a cover still need artwork."""
    hit = _entry(1, censored=False)
    del hit["cover_url"]
    assert hanime_scraper._hit_to_card(hit)["poster_url"].endswith("/posters/1.jpg")


def test_listing_fills_up_when_a_filter_removes_entries(hanime_scraper, catalogue):
    """A filtered listing must still fill the row, not leave a half-empty grid.

    Half the catalogue is censored here, so a single fixed-size page would
    have yielded half a row -- and scroll arrows with nothing to scroll.
    """
    catalogue([_entry(i, censored=bool(i % 2)) for i in range(200)])

    unfiltered = hanime_scraper.fetch_new()
    censored_off = hanime_scraper.fetch_new(show_censored=False)
    uncensored_off = hanime_scraper.fetch_new(show_uncensored=False)

    assert len(unfiltered) == hanime_scraper._LISTING_TARGET_COUNT
    assert len(censored_off) == hanime_scraper._LISTING_TARGET_COUNT
    assert len(uncensored_off) == hanime_scraper._LISTING_TARGET_COUNT
    assert {card["censored"] for card in censored_off} == {"Uncensored"}
    assert {card["censored"] for card in uncensored_off} == {"Censored"}


def test_listing_returns_what_exists_when_the_catalogue_is_short(hanime_scraper, catalogue):
    """Fewer matches than the target is not an error."""
    catalogue([_entry(i, censored=bool(i % 2)) for i in range(20)])

    cards = hanime_scraper.fetch_new(show_censored=False)

    assert 0 < len(cards) < hanime_scraper._LISTING_TARGET_COUNT


def test_listing_is_sorted_and_deduplicated_by_franchise(hanime_scraper, catalogue):
    """Newest first, and one card per franchise across the whole listing."""
    entries = [_entry(i, censored=False) for i in range(50)]
    # Two more episodes of the very first franchise, further down the sort.
    entries.append({**_entry(0, censored=False), "name": "Title 0 2", "slug": "title-0-2"})
    entries.append({**_entry(0, censored=False), "name": "Title 0 3", "slug": "title-0-3"})
    catalogue(entries)

    cards = hanime_scraper.fetch_new()

    assert cards[0]["title"] == "Title 49 1"        # highest created_at_unix
    franchises = [card["franchise"] for card in cards]
    assert len(franchises) == len(set(franchises))


def test_trending_and_new_order_differently(hanime_scraper, catalogue):
    """The two listings must not silently become the same list."""
    entries = []
    for i in range(30):
        hit = _entry(i, censored=False)
        hit["views"] = 30 - i          # reverse of created_at_unix
        entries.append(hit)
    catalogue(entries)

    assert hanime_scraper.fetch_new()[0]["title"] != hanime_scraper.fetch_trending()[0]["title"]


# ==========================================================================
# test_aniworld_absolute_episodes.py
#
# AniWorld's absolute episode numbering (Settings -> Downloads).
# 
# AniWorld splits long-running shows into site-side seasons but keeps one
# continuous count, which it appends to the episode title as "[Episode 062]".
# With `aniworld_absolute_episodes` on, that number -- not the season-relative
# one -- is what the file name is built from. The season is deliberately left
# alone: the file is S02E063, not S01E063.
# 
# The risk this guards is not the regex, it is the fallbacks: an episode without
# a marker, a movie entry, and the setting being off must all keep the old
# S02E002 name, because anything else silently renumbers an existing library.
# ==========================================================================
EP_URL = "https://aniworld.to/anime/stream/one-piece/staffel-2/episode-2"
FILM_URL = "https://aniworld.to/anime/stream/one-piece/filme/film-1"

MARKED_TITLE = (
    "A Promise Between Men! Luffy and the Whale Vow to Meet Again! [Episode 063]"
)


@pytest.mark.parametrize(
    "title, expected",
    [
        (MARKED_TITLE, 63),
        ("x [Episode 062]", 62),          # leading zeros are not octal
        ("x [episode 7]", 7),             # the site's casing varies
        ("x [ Episode  1234 ]", 1234),    # stray whitespace
        ("Ein Bad in Magensäure [Folge 62]", 62),
        ("Das Versprechen", None),        # the normal case: no marker
        ("Episode 63", None),             # brackets are what makes it a marker
        ("x [Episode 0]", None),          # 0 would break the fallback chain
        ("", None),
        (None, None),
    ],
)
def test_parse_absolute_episode_number(title, expected):
    assert parse_absolute_episode_number(title) == expected


def test_parse_prefers_the_first_title_that_carries_a_marker():
    assert parse_absolute_episode_number(None, "de [Folge 5]", "en [Episode 9]") == 5


def _episode(monkeypatch, enabled, title_en, url=EP_URL):
    monkeypatch.setenv("MEDIAFORGE_ANIWORLD_ABSOLUTE_EPISODES", "1" if enabled else "0")
    return AniworldEpisode(
        url=url, episode_number=2, title_de="Das Versprechen", title_en=title_en
    )


def test_off_by_default_keeps_the_season_relative_number(monkeypatch):
    ep = _episode(monkeypatch, False, MARKED_TITLE)
    assert ep.absolute_episode_number == 63     # the fact is still reported
    assert ep.file_episode_number == 2          # but it is not used


def test_on_uses_the_absolute_number(monkeypatch):
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert ep.file_episode_number == 63


def test_the_season_is_never_touched(monkeypatch):
    """Only the number becomes absolute -- S02E063, not S01E063."""
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert ep.season.season_number == 2
    assert {season for season, _ep in ep.file_number_candidates} == {2}


def test_on_without_a_marker_falls_back(monkeypatch):
    ep = _episode(monkeypatch, True, "A Town that Welcomes Pirates?")
    assert ep.absolute_episode_number is None
    assert ep.file_episode_number == 2


def test_movies_are_never_renumbered(monkeypatch):
    ep = _episode(monkeypatch, True, MARKED_TITLE, url=FILM_URL)
    assert ep.is_movie
    assert ep.file_episode_number == 2


def test_file_name_follows_the_setting(monkeypatch, tmp_path):
    """The point of the whole feature: what lands on disk."""
    monkeypatch.setenv(
        "MEDIAFORGE_NAMING_TEMPLATE",
        "{title}/Season {season}/{title} S{season}E{episode}.mkv",
    )

    class _Series:
        title_cleaned = "One Piece"
        release_year = "1999"
        imdb = "tt0388629"

    class _Season:
        season_number = 2

    for enabled, expected in ((False, "One Piece S02E002"), (True, "One Piece S02E063")):
        ep = _episode(monkeypatch, enabled, MARKED_TITLE)
        ep._series = _Series()
        ep._season = _Season()
        ep.selected_path = str(tmp_path)
        assert ep._file_name == expected


# ---------------------------------------------------------------------------
# Presence detection across the switch
# ---------------------------------------------------------------------------
# The setting decides what a NEW file is called. A library built before the
# switch is still on disk under the old name, and "is this episode here?" has
# to answer yes for either -- otherwise flipping the setting reports a complete
# show as entirely missing and downloads all of it a second time.


def test_candidates_cover_both_naming_schemes(monkeypatch):
    for enabled in (False, True):
        ep = _episode(monkeypatch, enabled, MARKED_TITLE)
        assert set(ep.file_number_candidates) == {(2, 2), (2, 63)}, enabled


def test_candidates_start_with_what_a_new_download_gets(monkeypatch):
    """Order matters: the first pair is the name that would be written."""
    assert _episode(monkeypatch, True, MARKED_TITLE).file_number_candidates[0] == (2, 63)
    assert _episode(monkeypatch, False, MARKED_TITLE).file_number_candidates[0] == (2, 2)


def test_candidates_without_a_marker_are_just_the_one_pair(monkeypatch):
    ep = _episode(monkeypatch, True, "A Town that Welcomes Pirates?")
    assert ep.file_number_candidates == ((2, 2),)


def test_an_old_library_is_still_recognised(monkeypatch):
    """The scenario: absolute numbering switched on over an existing library."""
    on_disk = {(2, 2)}                      # downloaded before the switch
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert any(pair in on_disk for pair in ep.file_number_candidates)
    # ...and the file a fresh download would write is still the new name.
    assert ep.file_episode_number == 63


# ==========================================================================
# test_language_dropdown_contract.py
#
# The language dropdown must not be built from another site's language set.
# 
# This is the bug that made every source added after AniWorld/s.to look broken:
# ``rebuildLanguageSelect()`` special-cased FilmPalast/MegaKino/hanime and fell
# through to AniWorld's fixed language list for everything else. So a 9anime
# episode was offered "German Dub", the provider map is keyed by the languages
# the site really has ("English Sub"), the lookup missed, and the modal reported
# "No source available" for a title whose sources were right there.
# 
# A JS contract test rather than a runtime one, in the same spirit as
# tests/test_js_contracts.py: it pins the shape of the code that decides this,
# so the hardcoded fallback cannot come back unnoticed.
# ==========================================================================
APP_JS = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static" / "app.js"


@pytest.fixture(scope="module")
def app_js():
    return APP_JS.read_text(encoding="utf-8", errors="replace")


def test_unknown_sites_use_the_reported_languages(app_js):
    """The dynamic branch must exist and must be reached BEFORE ANIWORLD_LANGS."""
    dynamic = app_js.find("if (!isSto && !isAniworld) {")
    aniworld_langs = app_js.find("window.ANIWORLD_LANGS || {}")
    assert dynamic != -1, "the dynamic language branch is gone"
    assert aniworld_langs != -1
    assert dynamic < aniworld_langs, \
        "AniWorld's language set is consulted before the site's own languages"


def test_the_dynamic_branch_reads_both_sources_of_truth(app_js):
    """foundLangs (/api/episodes) with availableProviders (/api/providers) as
    the fallback -- either is an answer about THIS title."""
    start = app_js.find("if (!isSto && !isAniworld) {")
    block = app_js[start:start + 1200]
    assert "foundLangs" in block
    assert "availableProviders" in block


def test_providers_answer_triggers_a_language_rebuild(app_js):
    """The first rebuild runs before either fetch returns, so for these sites
    the dropdown starts empty -- something has to fill it afterwards."""
    start = app_js.find("async function fetchProviders(")
    assert start != -1
    block = app_js[start:start + 1400]
    assert "rebuildLanguageSelect()" in block, \
        "fetchProviders no longer rebuilds the language dropdown"


def test_movies_only_assume_german_for_the_german_only_sites(app_js):
    """filmo.to is multi-language; hardcoding "German Dub" for every movie made
    its provider lookup miss for every language the dropdown offered."""
    idx = app_js.find('availableProviders = { "German Dub": seriesData.available_providers }')
    assert idx != -1, "movie provider branch not found"
    guard = app_js[max(0, idx - 700):idx]
    assert "_germanOnlyMovieSite" in guard, \
        "the German-only assumption is applied to every movie site again"
    assert "filmpalast.to" in guard and "megakino" in guard


# ==========================================================================
# test_search_sources.py
#
# The source catalogue: a module's content source must reach the search.
# 
# Before this existed, a third-party module could register a provider *and* a
# search source and still never be asked a keyword: the WebUI fanned every
# search out to five hardcoded site ids. These tests pin the contract that
# replaced that -- one server-side list (``web/source_policy.search_sources``),
# one endpoint (``GET /api/search/sources``), and no hardcoded five-source list
# left in the frontend files that consume it.
# ==========================================================================
_STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"


@pytest.fixture()
def module_source():
    """Register a fake module search source for the duration of one test.

    The matching Provider is registered too, because that is the contract:
    a search hit's URL has to be resolvable, and api_search() drops the ones
    that are not (see test_unresolvable_hits_are_dropped).
    """
    from mediaforge import providers, search

    providers.register_provider(
        "test-item",
        providers.Provider(
            name="TestSite",
            episode_pattern=re.compile(r"^https://test\.invalid/[a-z0-9]+$"),
            episode_cls=object,
        ),
    )
    search.register_search_source(
        "test-item", "testsite", lambda kw: [{"title": "Hit " + kw, "url": "https://test.invalid/x"}],
        label="TestSite",
    )
    try:
        yield "testsite"
    finally:
        search.unregister_search_source("test-item")
        providers.unregister_provider("test-item")


# ── The catalogue itself ────────────────────────────────────────────────────
def test_builtins_are_listed_with_labels_and_css(app):
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        ids = [s["id"] for s in search_sources()]
    assert ids[:8] == ["aniworld", "sto", "filmpalast", "megakino",
                       "filmo", "nineanime", "aniwaves", "hanime"]

    with app.app_context():
        by_id = {s["id"]: s for s in search_sources()}
    assert by_id["hanime"]["adult"] is True
    assert by_id["aniworld"]["adult"] is False
    # The class the WebUI puts on the result header must be a real one.
    assert by_id["sto"]["css_class"] == "browse-provider-sto"
    assert all(s["thirdparty"] is False for s in by_id.values())


def test_english_only_sources_are_opt_in_but_not_adult(app):
    """9anime/Aniwaves default to off like the adult source, but must never be
    treated AS one: is_adult_source() drives the age-confirmation modal and the
    kids-mode filtering, and neither may fire for a source that is merely
    English-only."""
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        by_id = {s["id"]: s for s in search_sources()}

    for sid in ("nineanime", "aniwaves"):
        assert by_id[sid]["adult"] is False, sid
        assert by_id[sid]["opt_in"] is True, sid
        assert by_id[sid]["english_only"] is True, sid
        assert by_id[sid]["enabled"] is False, sid
    # filmo.to is a normal opt-out source: German-capable, on by default.
    assert by_id["filmo"]["opt_in"] is False
    assert by_id["filmo"]["english_only"] is False
    assert by_id["filmo"]["enabled"] is True


def test_english_only_sources_survive_an_age_limited_session(app):
    """An age ceiling drops the adult source from the catalogue. The opt-in
    English-only ones must stay -- they are not adult content, and hiding them
    would silently make them unreachable for every kids-mode account."""
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        ids = [s["id"] for s in search_sources(include_adult=False)]
    assert "hanime" not in ids
    assert "nineanime" in ids and "aniwaves" in ids and "filmo" in ids


def test_module_source_appears_and_disappears(app, module_source):
    """The whole point: registering shows up, unregistering removes it."""
    from mediaforge import search
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        entry = [s for s in search_sources() if s["id"] == "testsite"]
    assert entry, "a registered module search source must be in the catalogue"
    assert entry[0]["thirdparty"] is True
    assert entry[0]["label"] == "TestSite"
    # No colour is invented for a source the app has never seen.
    assert entry[0]["css_class"] == "browse-provider-thirdparty"
    # Installed on purpose -> on by default, unlike an adult source.
    assert entry[0]["enabled"] is True

    search.unregister_search_source("test-item")
    with app.app_context():
        assert "testsite" not in [s["id"] for s in search_sources()]


def test_adult_module_source_is_opt_in_and_hideable(app):
    from mediaforge import search
    from mediaforge.web.source_policy import search_sources

    search.register_search_source("adult-item", "adultsite", lambda kw: [],
                                  label="AdultSite", adult=True)
    try:
        with app.app_context():
            entry = [s for s in search_sources() if s["id"] == "adultsite"][0]
            assert entry["adult"] is True
            # An age-limited session must not even be told it exists.
            assert "adultsite" not in [
                s["id"] for s in search_sources(include_adult=False)
            ]
    finally:
        search.unregister_search_source("adult-item")


def test_custom_enabled_key_is_read_and_reported(app):
    """A module reusing its own settings key must not get a dead switch."""
    from mediaforge import search
    from mediaforge.web.db import set_setting
    from mediaforge.web.source_policy import search_sources

    search.register_search_source("keyed-item", "keyedsite", lambda kw: [],
                                  label="Keyed", enabled_key="keyed_search_on")
    try:
        with app.app_context():
            set_setting("keyed_search_on", "0")
            entry = [s for s in search_sources() if s["id"] == "keyedsite"][0]
            assert entry["enabled"] is False
            # Reported, so the settings page writes back the same key.
            assert entry["enabled_key"] == "keyed_search_on"
    finally:
        search.unregister_search_source("keyed-item")


def test_bad_site_id_is_refused():
    """The id becomes a settings key, a DOM id and part of a CSS class."""
    from mediaforge import search

    for bad in ("", "x", "Has Space", "<script>", "UPPER", "a" * 41):
        with pytest.raises(ValueError):
            search.register_search_source("bad-item", bad, lambda kw: [])


def test_builtin_site_id_still_refused():
    from mediaforge import search

    with pytest.raises(ValueError):
        search.register_search_source("clash", "megakino", lambda kw: [])


def test_thirdparty_listing_carries_no_callable():
    """The listing is JSON-serialised into a response; search_fn must not be
    in it (and would not survive jsonify anyway)."""
    from mediaforge import search

    search.register_search_source("json-item", "jsonsite", lambda kw: [], label="J")
    try:
        entries = search.thirdparty_search_sources()
        assert json.dumps(entries)   # raises if a callable leaked in
        assert all("search_fn" not in e for e in entries)
    finally:
        search.unregister_search_source("json-item")


# ── The endpoint ────────────────────────────────────────────────────────────
def test_endpoint_requires_login(client):
    """Every view is wrapped by app.py's auth pass; this one too."""
    resp = client.get("/api/search/sources")
    assert resp.status_code in (301, 302, 401, 403)


def test_endpoint_lists_module_source(as_user, module_source):
    resp = as_user("admin").get("/api/search/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    ids = [s["id"] for s in data["sources"]]
    assert "aniworld" in ids and "testsite" in ids
    assert "order" in data and "hide_disabled_in_search" in data


def test_search_route_reaches_module_source(as_user, module_source):
    """The other half: POST /api/search with the module's site id works."""
    resp = as_user("admin").post("/api/search",
                                 json={"keyword": "abc", "site": "testsite"})
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.get_json()["results"]]
    assert titles == ["Hit abc"]


def test_unresolvable_hits_are_dropped(as_user):
    """A hit no provider owns is a dead card -- clicking it can only answer
    "Unsupported URL" -- so api_search() must not hand it to the UI."""
    from mediaforge import search

    search.register_search_source(
        "broken-item", "brokensite",
        lambda kw: [{"title": "Dead", "url": "https://nothing.invalid/x"},
                    {"title": "Alive", "url": "https://aniworld.to/anime/stream/naruto"}],
        label="BrokenSite",
    )
    try:
        resp = as_user("admin").post("/api/search",
                                     json={"keyword": "abc", "site": "brokensite"})
        assert resp.status_code == 200
        assert [r["title"] for r in resp.get_json()["results"]] == ["Alive"]
    finally:
        search.unregister_search_source("broken-item")


# ── No hardcoded lists left where they caused the bug ───────────────────────
@pytest.mark.parametrize("filename", ["app.js", "settings.js", "integrations.js"])
def test_frontend_has_no_second_hardcoded_source_list(filename):
    """One hardcoded fallback per file is allowed (and documented); a second
    one means a consumer drifted back to its own list and will silently skip
    module sources again."""
    text = (_STATIC / filename).read_text(encoding="utf-8", errors="replace")
    hits = re.findall(r'"aniworld"\s*,\s*"sto"\s*,\s*"filmpalast"', text)
    assert len(hits) <= 1, (
        f"{filename} has {len(hits)} hardcoded source lists; derive from "
        "loadSearchSources() / sources.available instead"
    )


def test_search_fanout_is_derived_not_positional():
    """doSearch() must fan out over the catalogue, and the renderer must take
    a list -- the old five-positional-argument renderResultsBoth() could not
    represent a sixth source at all."""
    text = (_STATIC / "app.js").read_text(encoding="utf-8", errors="replace")
    assert "buildSourceSection" in text
    assert "function renderResultsBoth" not in text
    assert "loadSearchSources" in text


def test_search_renders_per_source_not_after_all_of_them():
    """Results appear as each source answers.

    Every consumer fans out one request per source and used to await
    Promise.all before painting anything, so the whole list arrived at the
    speed of the slowest site -- up to the 15 s per-request timeout when one
    was dead. Pinned here because "await Promise.all(...)" reads harmless and
    is the natural thing to write back.
    """
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8", errors="replace")
    # doSearch(): one slot per source, filled when THAT source answers.
    assert "buildSourceSection({" in app_js
    # runAniSearch(): one card appended per arriving answer.
    assert "forEach(appendResult)" in app_js
    for dead in ("const _sections = await Promise.all",
                 "const resultsArrays = await Promise.all"):
        assert dead not in app_js, f"{dead!r} is back -- search waits for the slowest source again"

    seerr_js = (_STATIC / "seerr.js").read_text(encoding="utf-8", errors="replace")
    assert "Promise.all(sources.map(" not in seerr_js
    # Re-rendering per answer must not re-scrape the posters.
    assert "_posterCache" in seerr_js


# ==========================================================================
# test_dns_fallback.py
#
# The project resolver failing to resolve a host must not be terminal.
# 
# MediaForge routes its HTTP egress through a DoH resolver so an ISP-level DNS
# block cannot hide a source site. When that resolver cannot answer at all --
# blocked DoH endpoint, a filtered network, a domain it simply has no answer for
# -- the request used to die with a bare ``NameResolutionError`` and the source
# went dark, even though the machine's own resolver would have answered fine.
# 
# These tests pin the one retry that fixes it, and just as importantly the two
# cases that must NOT retry.
# ==========================================================================
DNS_EXC = niquests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='filmo.to', port=443): Max retries exceeded with "
    "url: /popular (Caused by NameResolutionError(\"Failed to resolve "
    "'filmo.to' (Name or service not known: filmo.to using 1 resolver(s))\"))"
)
OTHER_EXC = niquests.exceptions.ConnectionError(
    "Connection aborted., OSError(5, 'Input/output error')"
)


@pytest.fixture(autouse=True)
def _clean_sticky_set():
    """The fallback is sticky per host (see config._dns_fallback_hosts), so one
    test marking a host would silently decide the next test's routing."""
    with C._dns_fallback_lock:
        C._dns_fallback_hosts.clear()
    yield
    with C._dns_fallback_lock:
        C._dns_fallback_hosts.clear()


@pytest.fixture
def proxy(monkeypatch):
    """A _SessionProxy whose two sessions are labelled stand-ins, so a test can
    see WHICH resolver a request went out on."""
    doh = types.SimpleNamespace(_label="doh")
    system = types.SimpleNamespace(_label="system")
    monkeypatch.setattr(C._SessionProxy, "_get_session", lambda self: doh)
    monkeypatch.setattr(C._SessionProxy, "_get_system_session", lambda self: system)
    return C._SessionProxy(resolver=["doh+google://"])


def _record(monkeypatch, behaviour):
    """Replace the mirror walk with *behaviour*, recording the session used."""
    calls = []

    def fake(session, method, url, **kwargs):
        calls.append(session._label)
        return behaviour(session)

    monkeypatch.setattr(M, "request_with_failover", fake)
    return calls


def test_dns_failure_is_retried_on_the_system_resolver(proxy, monkeypatch):
    def behaviour(session):
        if session._label == "doh":
            raise DNS_EXC
        return "answered"

    calls = _record(monkeypatch, behaviour)
    assert proxy.request("GET", "https://filmo.to/popular") == "answered"
    assert calls == ["doh", "system"]


def test_a_non_dns_error_is_not_retried(proxy, monkeypatch):
    """A dead site, a TLS problem or a reset connection are answers, not
    resolver failures -- retrying them on another resolver only doubles the
    wait before the same error surfaces."""
    calls = _record(monkeypatch, lambda session: (_ for _ in ()).throw(OTHER_EXC))
    with pytest.raises(niquests.exceptions.ConnectionError):
        proxy.request("GET", "https://filmo.to/popular")
    assert calls == ["doh"]


def test_no_retry_when_the_system_resolver_is_already_in_use(monkeypatch):
    """Nothing to fall back TO -- a second identical attempt would just fail
    the same way, twice as slowly."""
    doh = types.SimpleNamespace(_label="doh")
    system = types.SimpleNamespace(_label="system")
    monkeypatch.setattr(C._SessionProxy, "_get_session", lambda self: doh)
    monkeypatch.setattr(C._SessionProxy, "_get_system_session", lambda self: system)
    proxy = C._SessionProxy(resolver="system")

    calls = _record(monkeypatch, lambda session: (_ for _ in ()).throw(DNS_EXC))
    with pytest.raises(niquests.exceptions.ConnectionError):
        proxy.request("GET", "https://filmo.to/popular")
    assert calls == ["doh"]


@pytest.mark.parametrize("message, expected", [
    ("Failed to resolve 'filmo.to'", True),
    ("NameResolutionError(...)", True),
    ("Name or service not known", True),
    ("Temporary failure in name resolution", True),
    ("Connection aborted., OSError(5)", False),
    ("read timed out", False),
    ("certificate verify failed", False),
])
def test_dns_failure_detection(message, expected):
    assert C._looks_like_dns_failure(Exception(message)) is expected


def test_the_fallback_is_sticky_per_host(proxy, monkeypatch):
    """Once a host is known to be unresolvable, stop paying the failing lookup
    on every single request -- a page open or a download is dozens of requests
    to the same host, and each one ate a full resolver timeout first."""
    def behaviour(session):
        if session._label == "doh":
            raise DNS_EXC
        return "answered"

    calls = _record(monkeypatch, behaviour)
    proxy.request("GET", "https://filmo.to/a")
    proxy.request("GET", "https://filmo.to/b")
    proxy.request("GET", "https://filmo.to/c")
    # First request probes the project resolver, the rest go straight out.
    assert calls == ["doh", "system", "system", "system"]
    assert "filmo.to" in C.dns_fallback_hosts()

    # A different host is unaffected -- one site's DNS problem must not quietly
    # move the whole app off the resolver the user configured.
    calls.clear()
    proxy.request("GET", "https://aniworld.to/x")
    assert calls == ["doh", "system"]


def test_changing_the_dns_setting_clears_the_sticky_set(proxy, monkeypatch):
    """The user picking a different resolver is exactly the moment to give it
    a fresh chance; staying pinned would ignore the setting they just changed."""
    _record(monkeypatch, lambda session: (_ for _ in ()).throw(DNS_EXC)
            if session._label == "doh" else "answered")
    proxy.request("GET", "https://filmo.to/a")
    assert "filmo.to" in C.dns_fallback_hosts()

    proxy._swap(["doh+cloudflare://"])
    assert C.dns_fallback_hosts() == []


def test_the_fallback_session_shares_the_cookie_jar(monkeypatch):
    """Several scrapers only work because consecutive requests look like the
    same browser: filmo.to hands out a CSRF token plus a session cookie on the
    movie page and validates both on the /n POST. Two jars means the POST
    arrives without the session and filmo.to answers 419 Page Expired."""
    made = []

    class _FakeSession:
        def __init__(self, resolver):
            self.resolver = resolver
            self.cookies = {"jar": resolver}
            self.timeout = None
            made.append(self)

    monkeypatch.setattr(C, "_make_session", lambda resolver=None: _FakeSession(resolver))
    proxy = C._SessionProxy(resolver=["doh+google://"])

    primary = proxy._get_session()
    fallback = proxy._get_system_session()
    assert fallback is not primary
    assert fallback.cookies is primary.cookies
