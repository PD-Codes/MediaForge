"""TMDB client for the MediaCalendar plugin.

Reuses MediaForge's *existing* CineInfo TMDB credential
(app_settings key ``cineinfo_tmdb_api_key``, a v3 API key sent as the
``api_key`` query param) and region (``cineinfo_country``) instead of
asking the user to configure TMDB a second time -- see
:func:`is_configured`. This is the one deliberate coupling point outside
this folder the user asked for explicitly ("das Haupt TMDB Connection...
entsprechend eine Prüfung erstellen beim Enabling ob das konfiguriert
ist"); everything else this module needs (discover filtering, keyword
search, watch providers, season/episode detail) lives here rather than
touching MediaForge's core ``web/tmdb_cache.py``, whose helpers
(``_tmdb_lookup_cached`` and friends) are shaped for single-title lookups
(provider pills, calendar sync) and don't cover discover/genre/keyword
browsing at all.

Caching strategy: reference data that changes rarely and isn't
calendar-specific (genre list, watch-provider list, keyword search,
title/season detail) is cached via MediaForge's *generic* shared
``provider_cache`` table (``..db``'s ``get_provider_cache``/
``set_provider_cache`` -- the exact same reuse mechanism
web/thirdparties/anime_seasons/service.py uses for Jikan), namespaced so
it can never collide with another integration's cache entries. The
*calendar-specific* discover results this feeds into are cached
separately, per calendar, in this module's own database (see db.py's
cached_releases table) -- a plain namespaced key/value cache isn't a good
fit for "all discover results for calendar #7," but it's exactly right
for "TMDB's movie genre list."
"""

import threading
import time

import requests

from ...db import get_provider_cache, set_provider_cache, get_setting
from ....logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 15
_USER_AGENT = "MediaForge/1.0 (+https://github.com/PD-Codes/MediaForge) MediaCalendar"
_NAMESPACE = "mediacalendar_tmdb"
_CACHE_SCHEMA_VERSION = 1

TMDB_SETTING_KEY = "cineinfo_tmdb_api_key"
REGION_SETTING_KEY = "cineinfo_country"


class TmdbNotConfigured(Exception):
    """Raised when a call is attempted without a TMDB key configured.
    Callers (service.py/routes.py) turn this into a clear user-facing
    message rather than letting a raw request exception bubble up."""


class TmdbError(Exception):
    """Wraps a TMDB HTTP/network failure with a short, loggable message."""


# --- Rate limiting -----------------------------------------------------
# Same token-bucket shape as web/tmdb_cache.py's _TmdbRateLimiter and
# web/thirdparties/anime_seasons/service.py's _JikanRateLimiter -- kept as
# its own small copy (rather than importing tmdb_cache's private class)
# so this module has zero behavioural coupling to core TMDB code beyond
# the shared credential/region settings.
class _RateLimiter:
    def __init__(self, rate: float):
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                time.sleep(max(wait, 0))
                self._tokens = 0
            else:
                self._tokens -= 1


_rl = _RateLimiter(rate=30.0)


def is_configured() -> bool:
    """True iff MediaForge's CineInfo TMDB key is set -- the single
    "enable" precondition this whole plugin is gated on."""
    return bool((get_setting(TMDB_SETTING_KEY, "") or "").strip())


def get_region() -> str:
    return (get_setting(REGION_SETTING_KEY, "DE") or "DE").strip() or "DE"


def _api_key() -> str:
    key = (get_setting(TMDB_SETTING_KEY, "") or "").strip()
    if not key:
        raise TmdbNotConfigured(
            "TMDB ist nicht konfiguriert -- unter Einstellungen -> Integrationen -> "
            "CineInfo einen TMDB API-Key hinterlegen."
        )
    return key


def _get(path: str, params: "dict | None" = None) -> dict:
    params = dict(params or {})
    params["api_key"] = _api_key()
    _rl.acquire()
    try:
        resp = requests.get(
            f"{_BASE_URL}{path}", params=params, timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
    except requests.RequestException as exc:
        raise TmdbError(f"TMDB nicht erreichbar: {exc}") from exc
    if not resp.ok:
        raise TmdbError(f"TMDB {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise TmdbError(f"TMDB lieferte keine gültige Antwort: {exc}") from exc


def _cache_key(*parts) -> str:
    return f"v{_CACHE_SCHEMA_VERSION}:" + ":".join(str(p) for p in parts)


def _cached(cache_key: str, ttl: float, fetch):
    cached = get_provider_cache(_NAMESPACE, cache_key, ttl=ttl)
    if cached is not None:
        return cached.get("data")
    data = fetch()
    set_provider_cache(_NAMESPACE, cache_key, {"data": data})
    return data


# --- Reference data (cached) --------------------------------------------

def get_genres(media_type: str, lang: str = "de-DE") -> list:
    """[{id, name}, ...] for movie or tv genres."""
    return _cached(
        _cache_key("genres", media_type, lang), 7 * 86400,
        lambda: _get(f"/genre/{media_type}/list", {"language": lang}).get("genres", []),
    )


def search_keywords(query: str, lang: str = "de-DE") -> list:
    """[{id, name}, ...] -- live autocomplete against TMDB's keyword
    search (in place of the Android app's weekly bulk keyword-id export +
    local table, which needs a background scheduler this plugin
    deliberately doesn't add -- a short-TTL cache per query keeps repeat
    keystrokes cheap without that infrastructure)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    return _cached(
        _cache_key("kw", query.lower()), 6 * 3600,
        lambda: _get("/search/keyword", {"query": query}).get("results", []),
    )


def get_watch_providers(media_type: str, region: "str | None" = None) -> list:
    """[{provider_id, provider_name, logo_path}, ...] available in `region`."""
    region = region or get_region()
    return _cached(
        _cache_key("providers", media_type, region), 7 * 86400,
        lambda: _get(f"/watch/providers/{media_type}", {"watch_region": region}).get("results", []),
    )


def get_detail(media_type: str, tmdb_id: int, lang: str = "de-DE") -> dict:
    """Movie/TV detail. TV includes next_episode_to_air/status/seasons."""
    return _cached(
        _cache_key("detail", media_type, tmdb_id, lang), 6 * 3600,
        lambda: _get(f"/{media_type}/{tmdb_id}", {
            "language": lang, "append_to_response": "alternative_titles",
        }),
    )


def get_season_episodes(tmdb_id: int, season_number: int, lang: str = "de-DE") -> list:
    """[{episode_number, name, air_date, overview}, ...] for one season."""
    data = _cached(
        _cache_key("season", tmdb_id, season_number, lang), 12 * 3600,
        lambda: _get(f"/tv/{tmdb_id}/season/{season_number}", {"language": lang}),
    )
    return data.get("episodes", []) if data else []


def get_title_providers(media_type: str, tmdb_id: int, region: "str | None" = None) -> list:
    """[{provider_id, provider_name, logo_path}, ...] flatrate/free/ads
    providers this specific title is available on, in `region`."""
    region = region or get_region()
    data = _cached(
        _cache_key("title_providers", media_type, tmdb_id), 24 * 3600,
        lambda: _get(f"/{media_type}/{tmdb_id}/watch/providers"),
    )
    by_region = (data or {}).get("results", {}).get(region, {})
    seen, out = set(), []
    for bucket in ("flatrate", "free", "ads"):
        for p in by_region.get(bucket, []):
            if p["provider_id"] not in seen:
                seen.add(p["provider_id"])
                out.append(p)
    return out


# --- Search / discover (not cached here -- calendar-level caching is
# service.py's job via db.cached_releases) ------------------------------

def search_multi(query: str, lang: str = "de-DE") -> list:
    """Free-text title search across movies and TV, used for manually
    adding/excluding titles on a calendar or a list."""
    query = (query or "").strip()
    if not query:
        return []
    results = _get("/search/multi", {"query": query, "language": lang}).get("results", [])
    return [r for r in results if r.get("media_type") in ("movie", "tv")]


def discover(media_type: str, *, date_from: "str | None" = None, date_to: "str | None" = None,
             genres: "list | None" = None, keywords: "list | None" = None,
             providers: "list | None" = None, provider_mode: str = "include",
             region: "str | None" = None, lang: str = "de-DE", max_pages: int = 3,
             air_date: bool = False) -> list:
    """Raw TMDB discover/<movie|tv> results across up to max_pages pages,
    sorted by release date ascending. `air_date=True` switches TV
    discovery to `air_date.gte/lte` instead of `first_air_date.gte/lte`
    -- needed to surface next-episode dates for shows already running
    (their *premiere* date is in the past, so a first_air_date filter
    would never match them again), mirroring the Android app's
    discoverTvAiring() distinct from its plain discover/tv call.
    """
    region = region or get_region()
    date_field = "primary_release_date" if media_type == "movie" else (
        "air_date" if air_date else "first_air_date")
    params = {
        "language": lang,
        "sort_by": f"{date_field}.asc",
        "watch_region": region,
    }
    if date_from:
        params[f"{date_field}.gte"] = date_from
    if date_to:
        params[f"{date_field}.lte"] = date_to
    if genres:
        params["with_genres"] = ",".join(str(g) for g in genres)
    if keywords:
        params["with_keywords"] = ",".join(str(k) for k in keywords)
    if providers:
        params["with_watch_providers"] = ",".join(str(p) for p in providers)
        params["with_watch_monetization_type"] = "flatrate|free|ads|rent|buy"
    # provider_mode="exclude" isn't directly expressible as a TMDB discover
    # param (TMDB only supports an include-list) -- service.py applies the
    # exclusion client-side after fetching, by dropping any result whose
    # own watch-provider list intersects the excluded set.

    out = []
    for page in range(1, max_pages + 1):
        data = _get(f"/discover/{media_type}", {**params, "page": page})
        out.extend(data.get("results", []))
        if page >= data.get("total_pages", 1):
            break
    return out
