"""Jikan (unofficial MyAnimeList v4 REST API) seasonal-anime service.

Fetches "what's airing this season" listings from our self-hosted jikan-rest
instance (see _BASE_URL below) for the current season plus the three
preceding ones, normalizes each entry
into the shape routes.py's API needs, and persists results in the shared
``provider_cache`` table (see ..db — the same generic cache
Crunchyroll/Fernsehserien.de use) so a restart doesn't lose the day's data
and Jikan's public rate limit is never hammered by concurrent page loads.

Season order within a year is always Winter -> Spring -> Summer -> Fall
(matches Jikan's own ``season`` field). The *current* season is always
fetched from /seasons/now rather than computed-and-fetched via
/seasons/{year}/{season}, because Jikan's ``now`` endpoint reflects whatever
MAL currently considers the active season — naive month-based math could be
a few days off right at a season boundary.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date

import requests

from ...db import get_provider_cache, set_provider_cache
from ....logger import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://apis.domekologe.eu/jikan/v4"
_NAMESPACE = "jikan_seasons"
_CACHE_TTL = 12 * 3600   # seasonal data barely changes within a day
_MAX_PAGES = 4           # cap: 4 pages * 25/page = up to 100 titles per season
_REQUEST_TIMEOUT = 15
_USER_AGENT = "MediaForge/1.0 (+https://github.com/PD-Codes/MediaForge)"

# Bumped whenever a change to _normalize_entry()/_fetch_pages() alters the
# shape or correctness of a cached season's items (e.g. adding a field, or
# fixing a dedup bug) — folded into the cache key below so already-cached
# (now-stale-shaped) entries are treated as a cache miss immediately,
# instead of continuing to serve the old data for up to _CACHE_TTL. Bump
# this again the next time _normalize_entry/_fetch_pages meaningfully change.
#
# v6: no local shape change, but jikan-rest's "is_franchise_debut" values
# themselves were wrong upstream (an unrelated jikan-rest bug — it checked
# related['Prequel'] on a list-shaped field, always empty) for however long
# that bug was live. Bumping forces every season slug to be re-fetched
# instead of serving is_new=true entries that were cached while jikan-rest
# was still returning the broken value, since a restart alone doesn't clear
# this persisted (DB-backed, not in-memory) cache.
_CACHE_SCHEMA_VERSION = 6


def _cache_key(slug: str) -> str:
    return f"{slug}:v{_CACHE_SCHEMA_VERSION}"

SEASON_ORDER = ["winter", "spring", "summer", "fall"]
# English source words, translated at the call site via flask_babel gettext.
SEASON_LABELS = {"winter": "Winter", "spring": "Spring", "summer": "Summer", "fall": "Fall"}

# Markers that indicate a title is a sequel/continuation rather than a
# franchise's first entry, e.g. "Youjo Senki II", "Attack on Titan Season 2",
# "Demon Slayer: ... 2nd Season", "... Part 2", "... Cour 2". Whichever
# pattern matches earliest (leftmost) wins, and everything from that match
# onward gets cut off — not just a *trailing* marker: MAL titles like
# "Mushoku Tensei III: Isekai Ittara Honki Dasu" put the marker BEFORE a
# colon-subtitle, not at the very end, so the roman-numeral pattern below
# uses a word boundary (\b) rather than requiring end-of-string ($) — found
# via a real "Mushoku Tensei III: ..." card wrongly showing the "New" pill,
# since the old $-anchored pattern never matched and is_sequel stayed False.
_SEQUEL_MARKER_PATTERNS = [
    re.compile(r"\s+\d+(?:st|nd|rd|th)\s+Season\b", re.IGNORECASE),
    re.compile(r"\s+Season\s+\d+\b", re.IGNORECASE),
    re.compile(r"\s+(?:Part|Cour)\s+(?:\d+|[IVX]{1,4})\b", re.IGNORECASE),
    re.compile(r"\s+[IVX]{1,4}\b"),  # standalone roman numeral, e.g. " II" or " III: Subtitle"
    re.compile(r"\s+[2-9]$"),        # trailing standalone digit 2-9, e.g. " 2"
]


def _split_sequel_marker(title: str):
    """Return (base_title, is_sequel).

    base_title has any trailing season/sequel marker stripped — this is what
    should be used to query TMDB with, since TMDB almost always groups every
    season/cour of an anime under one base-title entry instead of a separate
    one per season/part, unlike MyAnimeList (see routes.py's use of this for
    the TMDB batch enrichment call).

    is_sequel is True when a marker was found, i.e. this is *not* the
    franchise's first season — used (inverted) for the "New" pill; see
    get_season_slots()'s caller in routes.py.
    """
    if not title:
        return title, False
    for pattern in _SEQUEL_MARKER_PATTERNS:
        m = pattern.search(title)
        if m:
            base = title[:m.start()].rstrip()
            if base:
                return base, True
    return title, False


class _JikanRateLimiter:
    """Token-bucket limiter, same shape as tmdb_cache._TmdbRateLimiter, tuned
    to Jikan's public documented limit (60 req/min *and* 3 req/sec) — capped
    at 1 req/s here to stay comfortably under both at once."""

    def __init__(self, rate: float = 1.0):
        self._rate = rate
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                wait = 0.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
        if wait > 0:
            time.sleep(wait)


_jikan_rl = _JikanRateLimiter(rate=1.0)

# In-flight de-duplication: if two requests race on a cold cache entry for
# the same slug, only one actually calls Jikan; the other waits for it.
_refresh_locks: dict = {}
_refresh_mutex = threading.Lock()


def _season_for_month(month: int) -> str:
    if month in (1, 2, 3):
        return "winter"
    if month in (4, 5, 6):
        return "spring"
    if month in (7, 8, 9):
        return "summer"
    return "fall"


def _prev_season(year: int, season: str):
    idx = SEASON_ORDER.index(season)
    if idx == 0:
        return year - 1, "fall"
    return year, SEASON_ORDER[idx - 1]


def get_season_slots(today: "date | None" = None) -> list:
    """Return the 4 season descriptors to show as tiles, newest first:
    ``[{slug, year, season, is_current}, ...]``.

    "slug" is what the /anime-seasons/<slug> page route and the
    /api/anime-seasons/<slug> API route use to identify a season; the
    current season's slug is always "now" (see module docstring)."""
    today = today or date.today()
    year, season = today.year, _season_for_month(today.month)
    slots = [{"slug": "now", "year": year, "season": season, "is_current": True}]
    for _ in range(3):
        year, season = _prev_season(year, season)
        slots.append({
            "slug": f"{year}-{season}",
            "year": year,
            "season": season,
            "is_current": False,
        })
    return slots


def slot_for_slug(slug: str):
    """Return the matching slot dict from get_season_slots() for *slug*, or
    None if it's not one of the 4 currently-valid slugs (e.g. a stale
    bookmark from a previous season)."""
    for slot in get_season_slots():
        if slot["slug"] == slug:
            return slot
    return None


def _normalize_entry(raw: dict) -> dict:
    images = (raw.get("images") or {}).get("jpg") or {}
    title = raw.get("title") or raw.get("title_english") or ""
    tmdb_query, is_sequel = _split_sequel_marker(title)
    return {
        "mal_id":        raw.get("mal_id"),
        "url":           raw.get("url") or "",
        "title":         title,
        "title_english": raw.get("title_english") or "",
        # German (or whatever TMDB_TRANSLATOR_LANGUAGE our self-hosted
        # jikan-rest instance is configured with) title, already resolved
        # server-side by jikan-rest's own TMDB translator — see
        # app/Services/TmdbTranslatorService.php in that project. Empty
        # string (not None) when jikan-rest has no TMDB key configured or
        # found no distinct localized title. Passed through to
        # openAniSearchModal() as an extra search variant so AniWorld/S.to
        # matching works even without MediaForge's own TMDB integration.
        "title_localized": raw.get("title_localized") or "",
        # Base title with any trailing "II"/"2nd Season"/"Part 2" marker
        # stripped — use this (not "title") when querying TMDB; see
        # _split_sequel_marker()'s docstring.
        "tmdb_query":    tmdb_query,
        # Drives the "New" pill in the UI. Prefer jikan-rest's own
        # "is_franchise_debut" (based on whether MAL lists a "Prequel"
        # relation for this entry — see AnimeResource::toArray() in the
        # jikan-rest project) over the title-regex heuristic below: the API
        # field is authoritative and doesn't break on titles MAL formats
        # inconsistently (e.g. "Mushoku Tensei III: Isekai Ittara Honki
        # Dasu" — regex has to guess, the API just checks the actual MAL
        # relation data). Falls back to the regex-based is_sequel only if
        # talking to an older self-hosted jikan-rest instance that doesn't
        # send this field yet (raw.get returns None, not True/False).
        "is_new":        raw.get("is_franchise_debut") if raw.get("is_franchise_debut") is not None else (not is_sequel),
        "poster":        images.get("large_image_url") or images.get("image_url") or "",
        "type":          raw.get("type") or "",
        "episodes":      raw.get("episodes"),
        "status":        raw.get("status") or "",
        # Not shown in the UI directly -- only used by is_adult_entry() below
        # to filter this item out unless "show adult content" is enabled.
        "rating":        raw.get("rating") or "",
        "airing":        bool(raw.get("airing")),
        "aired":         (raw.get("aired") or {}).get("string") or "",
        "score":         raw.get("score"),
        "synopsis":      raw.get("synopsis") or "",
        "genres":        [g.get("name") for g in (raw.get("genres") or []) if g.get("name")],
        "studios":       [s.get("name") for s in (raw.get("studios") or []) if s.get("name")],
        "season":        raw.get("season") or "",
        "year":          raw.get("year"),
    }


def is_adult_entry(item: dict) -> bool:
    """True if *item* (an already-normalized entry, see _normalize_entry)
    is explicit/adult content that should be hidden unless the user has
    turned on "show adult content" for this integration (see routes.py's
    ADULT_SETTING_KEY). Mirrors jikan-rest's own server-side
    exceptItemsWithAdultRating() check (Hentai genre or an "Rx" MAL rating)
    rather than also excluding "Ecchi", which MAL uses for plenty of
    otherwise-mainstream shows and isn't what a "hide adult content" toggle
    is expected to hide."""
    if "Hentai" in (item.get("genres") or []):
        return True
    return (item.get("rating") or "").startswith("Rx")


def _title_dedup_key(title: str) -> str:
    """Case/whitespace-normalized title, used as a *secondary* dedup key
    (see _fetch_pages) — Jikan occasionally lists what is visibly the same
    show twice under two different mal_id values (not just the same mal_id
    repeated across pages), e.g. a main entry and a near-duplicate data-entry
    error. Two entries with an identical displayed title are never useful to
    show twice in a "browse this season" grid, so the first one wins."""
    return re.sub(r"\s+", " ", (title or "").strip()).casefold()


def _fetch_pages(path: str) -> list:
    """GET every page of a Jikan season listing up to _MAX_PAGES, rate-limited.
    Best-effort: a failure mid-pagination just returns whatever pages already
    succeeded instead of raising (a partial season list beats an empty page).

    Deduplicates two ways: by mal_id (Jikan occasionally repeats the exact
    same entry across pages of the same season listing), and by normalized
    title (Jikan occasionally lists the same show under two different mal_id
    values instead) — see _title_dedup_key(). Either one alone left visible
    duplicates in the grid."""
    out = []
    seen_ids = set()
    seen_titles = set()
    page = 1
    while page <= _MAX_PAGES:
        _jikan_rl.acquire()
        try:
            resp = requests.get(
                f"{_BASE_URL}{path}",
                params={"page": page},
                timeout=_REQUEST_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("[Jikan] Fetch failed for %s (page %d): %s", path, page, exc)
            break
        data = payload.get("data") or []
        for entry in data:
            mal_id = entry.get("mal_id")
            if mal_id is not None:
                if mal_id in seen_ids:
                    continue
                seen_ids.add(mal_id)
            title_key = _title_dedup_key(entry.get("title") or entry.get("title_english") or "")
            if title_key:
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
            out.append(_normalize_entry(entry))
        has_next = bool((payload.get("pagination") or {}).get("has_next_page"))
        if not has_next:
            break
        page += 1
    return out


def _fetch_season(slug: str, year: int, season: str) -> list:
    path = "/seasons/now" if slug == "now" else f"/seasons/{year}/{season}"
    return _fetch_pages(path)


def get_season(slug: str, year: int, season: str):
    """Cached season anime list (list of normalized dicts), or None if the
    cache is cold and the live fetch also failed. Never raises."""
    cache_key = _cache_key(slug)
    cached = get_provider_cache(_NAMESPACE, cache_key, ttl=_CACHE_TTL)
    if cached is not None:
        return cached.get("items", [])

    with _refresh_mutex:
        already_refreshing = slug in _refresh_locks
        if not already_refreshing:
            _refresh_locks[slug] = True

    if already_refreshing:
        # Someone else is already fetching this slug — wait for their result
        # instead of firing a duplicate burst of requests at Jikan.
        for _ in range(20):
            time.sleep(0.5)
            cached = get_provider_cache(_NAMESPACE, cache_key, ttl=_CACHE_TTL)
            if cached is not None:
                return cached.get("items", [])
        return None

    try:
        items = _fetch_season(slug, year, season)
        if items:
            set_provider_cache(_NAMESPACE, cache_key, {"items": items})
        return items or None
    finally:
        with _refresh_mutex:
            _refresh_locks.pop(slug, None)
