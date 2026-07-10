"""Calendar & list resolution engine for MediaCalendar.

Turns a calendar's saved filter (db.get_calendar()) into a concrete list
of releases -- the same "calendar = saved query, resolved on demand and
cached" model the Android app uses (see CalendarViewModel.kt /
CachedMediaEntity). Three sources (mirroring CalendarSource):

  - "discover"  TMDB discover/movie + discover/tv (+ a second air_date-based
                discover/tv pass so already-running shows' next episodes
                surface too, not just brand-new premieres).
  - "list"      one or more Lists' items (+ optionally folded together with
                a "discover" pass using the calendar's own filter, when
                combine_list_with_discover is set).
  - "library"   everything already in "your media library", from *either*
                of MediaForge's two independent library-tracking systems:
                MediaScan's Jellyfin/Plex snapshot (read-only reuse of
                mediascan_cache via db.get_mediascan_ids()) when a server
                is connected, and/or MediaForge's own native, file-based
                library scanner (web/routes/library.py's library_cache)
                via a folder-name -> TMDB search match, cached in this
                module's own DB (see _native_library_tmdb_ids_by_type()).
                Both are unioned -- see _library_tmdb_ids_by_type().

Regardless of source: positive_list_ids are folded in, negative_list_ids
and individually excluded titles are dropped, individually "manual"
titles are always included. Library-in-catalogue and Seerr-request status
are then attached (and optionally filtered on) by reading MediaForge's
*existing* mediaplayer/mediascan/Seerr settings and data -- read-only,
nothing here writes back into core tables.

Simplifications versus the Android app, noted here once rather than
scattered through the code: a "library" source is capped to a bounded
number of titles per refresh to keep a large library from turning into
hundreds of detail lookups on every cache refresh; the TMDB provider
"exclude" mode isn't a discover API param (TMDB only offers an
include-list), so it's applied client-side after fetching.

TV shows resolve to one release row *per matching episode* inside the
calendar's [date_from, date_to] window (today - lookback_weeks() through
today + lookahead_weeks()), via _tv_episode_releases()'s one-extra-call
season lookup -- not just the single `next_episode_to_air` TMDB's
show-detail endpoint exposes, which used to hide a weekly show's later
episodes within the same window.
"""

import threading
import time
from datetime import date, timedelta

from . import db as mcdb
from . import tmdb_client as tmdb
from ....logger import get_logger

logger = get_logger(__name__)

_LIBRARY_SOURCE_CAP = 150  # titles per refresh, see module docstring
_LIBRARY_MATCH_SEARCH_CAP = 40  # new (never-before-seen) folder titles TMDB-searched per refresh, see _native_library_tmdb_ids_by_type()

# --- Settings (generic extra_settings keys, read via the shared
# /api/settings/thirdparty/mediacalendar GET/PUT the registry wires up
# automatically -- see __init__.py's register_thirdparty() call) --------

SETTING_KEY = "mediacalendar_enabled"
_LOOKAHEAD_KEY = "mediacalendar_lookahead_weeks"
_LOOKBACK_KEY = "mediacalendar_lookback_weeks"
_CACHE_HOURS_KEY = "mediacalendar_cache_hours"
_USE_LIBRARY_KEY = "mediacalendar_use_library"
_NOTIFY_ENABLED_KEY = "mediacalendar_notify_enabled"
_NOTIFY_LEAD_DAYS_KEY = "mediacalendar_notify_lead_days"


def _get_setting(key, default):
    from ...db import get_setting
    return get_setting(key, default)


def lookahead_weeks() -> int:
    try:
        return max(1, min(26, int(_get_setting(_LOOKAHEAD_KEY, "8"))))
    except (TypeError, ValueError):
        return 8


def lookback_weeks() -> int:
    """How many weeks *before* today calendars also resolve releases for,
    in addition to the forward-looking lookahead window -- off (0) by
    default so existing calendars keep their exact prior behaviour (today
    onward only) unless explicitly opted into via Settings. Requested
    after a user noticed a just-aired episode from a couple weeks back had
    already scrolled out of a calendar's window entirely."""
    try:
        return max(0, min(12, int(_get_setting(_LOOKBACK_KEY, "0"))))
    except (TypeError, ValueError):
        return 0


def cache_hours() -> int:
    try:
        return int(_get_setting(_CACHE_HOURS_KEY, "12"))
    except (TypeError, ValueError):
        return 12


def module_enabled() -> bool:
    """Whether Media Kalender itself is currently switched on (the
    Modulmanager/Integrations master toggle, SETTING_KEY). Checked at the
    top of every background worker's per-tick function (_check_and_notify,
    _auto_refresh_calendars, _check_planned_downloads) -- previously only
    the HTTP routes checked this (via routes.py's _require_enabled_json()),
    so toggling the module off stopped API access immediately but the
    background threads (all three started unconditionally in __init__.py's
    register(app), and living for the process's whole lifetime) kept
    silently doing real work -- TMDB calls, DB writes, notifications, even
    AutoSync job creation -- regardless. Checking this here means a
    disabled module goes fully quiet within one worker tick (well under a
    minute for planned-downloads' hourly loop) instead of needing an app
    restart to actually stop."""
    return (_get_setting(SETTING_KEY, "0") or "0") == "1"


def use_library() -> bool:
    return (_get_setting(_USE_LIBRARY_KEY, "1") or "0") == "1"


def notify_enabled() -> bool:
    return (_get_setting(_NOTIFY_ENABLED_KEY, "0") or "0") == "1"


def notify_lead_days() -> int:
    try:
        return max(0, int(_get_setting(_NOTIFY_LEAD_DAYS_KEY, "1")))
    except (TypeError, ValueError):
        return 1


# --- Library / Seerr enrichment (read-only reuse of core settings/data) --

def _library_tmdb_ids() -> set:
    """tmdb ids MediaScan already knows are in the connected Jellyfin/Plex
    library. Empty set (never an error) if MediaScan isn't enabled/populated
    -- see web/mediascan.py / web/routes/integrations.py's
    /api/mediascan/library, which this mirrors by reading the same table."""
    if not use_library():
        return set()
    try:
        from ...db import get_mediascan_ids
        ids = get_mediascan_ids().get("tmdb_ids", set())
        return {str(i) for i in ids}
    except Exception:
        logger.exception("[MediaCalendar] Failed to read MediaScan library snapshot")
        return set()


def _library_tmdb_ids_by_type(media_type: str) -> set:
    """Same as _library_tmdb_ids(), but scoped to one media_type -- needed
    because a movie id and a tv id can coincidentally collide (they're
    separate TMDB namespaces), so _resolve_library() must not query every
    library id against both /movie/<id> and /tv/<id>; see
    web/db.py's get_mediascan_ids_by_type() for why that helper exists.

    MediaScan's own schema (web/mediascan.py's _mediascan_fetch_jellyfin/
    _mediascan_fetch_plex) stores TV entries with media_type="show" -- its
    own Jellyfin/Plex-facing convention -- not "tv" (the TMDB/mediacalendar
    convention this whole module otherwise uses). Querying with the wrong
    literal silently returned zero rows for every TV library calendar
    ("Mediathek lädt nicht" for anime/series) while movies still worked, so
    it went unnoticed until reported -- translate at this one boundary
    rather than changing either side's established convention.

    Unioned with _native_library_tmdb_ids_by_type(): MediaScan only ever
    has data if a Jellyfin/Plex server is connected, but plenty of users
    (reported case: no Jellyfin/Plex at all, just MediaForge's own
    built-in "Mediathek" file scanner) never populate it, which used to
    mean "My media library" calendars stayed permanently empty for them
    regardless of anything else being fixed here. Both sources return
    plain tmdb id strings so merging is a simple set union; a title
    present in both just dedupes for free."""
    if not use_library():
        return set()
    ids = set()
    try:
        from ...db import get_mediascan_ids_by_type
        scan_media_type = "show" if media_type == "tv" else media_type
        ids |= {str(i) for i in get_mediascan_ids_by_type(scan_media_type)}
    except Exception:
        logger.exception("[MediaCalendar] Failed to read MediaScan library snapshot (by type)")
    try:
        ids |= _native_library_tmdb_ids_by_type(media_type)
    except Exception:
        logger.exception("[MediaCalendar] Failed to read native library_cache snapshot (by type)")
    return ids


def _clean_library_folder_title(folder: str) -> str:
    """Best-effort search query from a raw native-library folder name.
    MediaForge's own file scanner (web/routes/library.py's _lib_scan_base)
    has no TMDB linkage at all -- it only ever sees whatever the user (or
    their download client / Sonarr-style organizer) named the folder -- so
    this strips the common junk a release/organizer name can still carry
    (a trailing year, bracketed tags, quality/source/codec keywords) before
    handing the rest to TMDB's free-text search. Deliberately conservative:
    a folder that's already a clean title (the common case for an
    organized library) passes through basically unchanged."""
    import re
    s = folder
    s = re.sub(r"[\.\_]+", " ", s)  # dots/underscores used as word separators
    s = re.sub(r"\[[^\]]*\]", "", s)  # [tags]
    s = re.sub(
        r"\b(1080p|720p|480p|2160p|4k|bluray|blu-ray|web-?dl|webrip|hdtv|dvdrip|"
        r"x264|x265|h ?264|h ?265|hevc|aac|dts|remux|amzn|nf|dual audio|multi)\b",
        "", s, flags=re.IGNORECASE)
    # Trailing (2013) year -- run *after* the quality-tag strip above, since
    # a raw folder like "Some.Movie.(2013).BluRay.x264" only has the year at
    # the very end once "BluRay.x264" has already been removed.
    s = re.sub(r"\(\d{4}\)\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -_.")
    return s or folder  # never search an empty string -- fall back to the raw name


def _native_library_folders_by_type(media_type: str) -> list:
    """Distinct folder names from MediaForge's own native library scan
    (web/routes/library.py's library_cache -- populated by the file-system
    scanner behind the "Mediathek" page, independent of MediaScan/Jellyfin/
    Plex), filtered to `media_type` via each entry's own is_movie flag.

    Each library_cache row's "data" is *not* a plain list of titles -- it's
    the dict web/routes/library.py's api_library_refresh() builds per scan
    target: {"label", "custom_path_id", "lang_folders", "titles"}, where
    exactly one of "titles" (a flat list of title dicts) or "lang_folders"
    (a list of {"name", "titles"} per language subfolder, used when the
    scan target has language-separated subfolders -- see the `lang_sep`
    branch in api_library_refresh) is populated and the other is None.
    Treating "data" itself as the title list (an earlier version of this
    function did) iterates the wrapper dict's *keys* instead -- plain
    strings like "label"/"titles" -- which blew up as soon as .get("is_movie")
    was called on one of those strings."""
    from ...db import get_all_library_cache
    want_movie = media_type == "movie"
    names = set()

    def _collect(title_list):
        for title in title_list or []:
            if bool(title.get("is_movie")) == want_movie and title.get("folder"):
                names.add(title["folder"])

    try:
        for cache_entry in get_all_library_cache().values():
            data = cache_entry.get("data") or {}
            _collect(data.get("titles"))
            for lang_folder in data.get("lang_folders") or []:
                _collect(lang_folder.get("titles"))
    except Exception:
        logger.exception("[MediaCalendar] Failed to read native library_cache")
        return []
    return sorted(names)


def _native_library_tmdb_ids_by_type(media_type: str) -> set:
    """Same shape as _library_tmdb_ids_by_type() (a set of tmdb ids for one
    media_type) but sourced from MediaForge's own native, file-based
    library scan instead of MediaScan's Jellyfin/Plex snapshot -- needed
    for anyone whose "Mediathek" is this built-in scanner rather than a
    connected Jellyfin/Plex server, for whom mediascan_cache is always
    empty (reported: "Mediathek Kalender zeigt immer noch nichts an", MediaScan
    logging showed 0 ids from either type even though the native library
    clearly wasn't empty -- see the "257 Serien / 179 Filme" screenshot).

    Folder names are TMDB-searched once and the result cached forever in
    mcdb.library_title_matches (get/set_library_title_match) -- a library
    can easily have hundreds of titles, and re-searching all of them on
    every refresh/auto-refresh tick would be slow and needlessly hammer
    TMDB. Only up to _LIBRARY_MATCH_SEARCH_CAP never-before-seen folders
    are searched per call; the rest are picked up on a later refresh (the
    30-min auto-refresh worker eventually works through a large backlog a
    few dozen titles at a time instead of blocking one request for
    minutes)."""
    if not use_library():
        return set()
    folders = _native_library_folders_by_type(media_type)
    if not folders:
        return set()
    known = mcdb.get_library_title_matches(media_type)
    ids = set()
    searched = 0
    for folder in folders:
        cached = known.get(folder)
        if cached is not None:
            if cached.get("tmdb_id"):
                ids.add(str(cached["tmdb_id"]))
            continue
        if searched >= _LIBRARY_MATCH_SEARCH_CAP:
            continue  # backlog -- resolved on a later refresh instead
        searched += 1
        query = _clean_library_folder_title(folder)
        match_id, match_title = None, None
        try:
            results = [r for r in tmdb.search_multi(query) if r.get("media_type") == media_type]
            if results:
                # Prefer a normalized-title exact match over TMDB's plain
                # popularity ranking -- a folder named e.g. "Railgun" would
                # otherwise happily "match" the first, most popular, totally
                # unrelated result search/multi returns for that query.
                folder_norm = _normalize_title(folder)
                exact = next(
                    (r for r in results if _normalize_title(r.get("title") or r.get("name") or "") == folder_norm),
                    None,
                )
                best = exact or results[0]
                match_id = best.get("id")
                match_title = best.get("title") or best.get("name")
        except Exception:
            logger.debug("[MediaCalendar] TMDB search failed for library folder %r (non-fatal)", folder, exc_info=True)
        mcdb.set_library_title_match(folder, media_type, match_id, match_title)
        if match_id:
            ids.add(str(match_id))
    return ids


def _normalize_title(s: "str | None") -> str:
    """Same shape of normalization web/routes/autosync.py's
    find_site_candidates() uses for its own fuzzy matching -- lowercase,
    strip everything but alphanumerics down to single spaces. Used here to
    cheaply check "is there already an AutoSync job for this title" (the
    "In-Sync" pill) without needing a tmdb_id column on autosync_jobs
    (there isn't one -- AutoSync jobs are keyed by scraped series_url/title
    only, see that module's docstring)."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _autosync_titles() -> set:
    """Normalized titles of every currently enabled AutoSync job -- best
    string-based approximation of "is this release already covered by
    AutoSync" available (see _normalize_title's docstring for why this
    isn't a tmdb_id lookup). Empty set (never an error) if AutoSync has no
    jobs or the lookup fails -- this is enrichment, never a hard
    dependency, same convention as _library_tmdb_ids/_seerr_requested_tmdb_ids
    above."""
    try:
        from ...db import get_autosync_jobs
        return {_normalize_title(j.get("title")) for j in get_autosync_jobs() if j.get("enabled")}
    except Exception:
        logger.exception("[MediaCalendar] Failed to read AutoSync jobs for In-Sync enrichment")
        return set()


def _seerr_requested_tmdb_ids() -> set:
    """tmdb ids with a pending or approved Seerr request. Empty set if
    Seerr isn't configured or unreachable -- this is enrichment, never a
    hard dependency."""
    from ...db import get_setting
    seerr_url = (get_setting("seerr_url", "") or "").rstrip("/")
    seerr_key = get_setting("seerr_api_key", "") or ""
    if not seerr_url or not seerr_key:
        return set()
    import requests
    out = set()
    try:
        for media_type in ("movie", "tv"):
            resp = requests.get(
                f"{seerr_url}/api/v1/request",
                params={"filter": "all", "take": 500, "skip": 0},
                headers={"X-Api-Key": seerr_key}, timeout=10,
            )
            if not resp.ok:
                continue
            for r in resp.json().get("results", []):
                tmdb_id = (r.get("media") or {}).get("tmdbId")
                if tmdb_id:
                    out.add(str(tmdb_id))
    except Exception:
        logger.debug("[MediaCalendar] Seerr enrichment lookup failed (non-fatal)", exc_info=True)
    return out


# --- TMDB-shape helpers ---------------------------------------------------

def _movie_to_release(m: dict) -> dict:
    return {
        "tmdb_id": m.get("id"), "media_type": "movie",
        "title": m.get("title") or m.get("original_title") or "",
        "overview": m.get("overview") or "",
        "poster_path": m.get("poster_path"),
        "release_date": m.get("release_date") or "",
        "season_number": -1, "episode_number": -1, "episode_title": None,
        "genre_ids": m.get("genre_ids") or [],
    }


def _tv_to_release(t: dict, episode: "dict | None" = None) -> dict:
    release_date = t.get("first_air_date") or ""
    season_number, episode_number, episode_title = -1, -1, None
    if episode:
        release_date = episode.get("air_date") or release_date
        season_number = episode.get("season_number", -1)
        episode_number = episode.get("episode_number", -1)
        episode_title = episode.get("name")
    return {
        "tmdb_id": t.get("id"), "media_type": "tv",
        "title": t.get("name") or t.get("original_name") or "",
        "overview": t.get("overview") or "",
        "poster_path": t.get("poster_path"),
        "release_date": release_date,
        "season_number": season_number, "episode_number": episode_number,
        "episode_title": episode_title,
        "genre_ids": t.get("genre_ids") or [],
    }


def _within(date_str: str, date_from: str, date_to: str) -> bool:
    return bool(date_str) and date_from <= date_str <= date_to


def _tv_episode_releases(detail: dict, date_from: str, date_to: str, *, strict: bool = True) -> list:
    """One release per episode of `detail` (a TV show's own detail blob)
    whose air_date falls inside [date_from, date_to] -- not just the
    single next_episode_to_air TMDB's show-detail endpoint exposes. A
    weekly show can easily have 2-4 episodes airing inside an 8-week
    lookahead window (or more once lookback_weeks() is also in play), and
    only ever showing the very next one hid the rest (reported: "zeigt nur
    eine Folge an, anstatt auch die folgenden"). Looks up the season
    next_episode_to_air (or, once a season has fully aired, the last
    known season) belongs to via get_season_episodes() -- one extra,
    12h-cached TMDB call per show, not per episode.

    `strict=True` (discover/list-dynamic): if nothing in the season falls
    inside the window and next_episode_to_air itself doesn't either,
    returns [] -- matches the old behaviour of dropping shows with no
    upcoming match entirely, so a plain Discover-filtered calendar doesn't
    suddenly grow non-matching rows.
    `strict=False` (library, manually-added lists/titles): those sources
    always include the show regardless of date (see _resolve_library's
    docstring), so this always returns at least one row -- the matching
    episodes if any, else the same single next_episode_to_air/first_air_date
    fallback row the old code produced.
    """
    nxt = detail.get("next_episode_to_air")
    season_number = (nxt or {}).get("season_number")
    if season_number is None:
        last = detail.get("last_episode_to_air") or {}
        season_number = last.get("season_number")
    episodes = []
    if season_number is not None:
        try:
            episodes = tmdb.get_season_episodes(detail["id"], season_number)
        except Exception:
            episodes = []
    matches = [ep for ep in episodes if _within(ep.get("air_date") or "", date_from, date_to)]
    if matches:
        return [_tv_to_release(detail, episode=ep) for ep in matches]
    if strict:
        if nxt and _within(nxt.get("air_date", ""), date_from, date_to):
            return [_tv_to_release(detail, episode=nxt)]
        return []
    return [_tv_to_release(detail, episode=nxt)]


# --- Source resolution -----------------------------------------------------

def _keyword_ids(keywords: list) -> list:
    """Normalizes a calendar/list's stored `keywords` into the plain numeric
    TMDB keyword ids tmdb.discover()'s with_keywords param needs. Rows are
    {id, name} dicts since schema version 3 (db.py's calendar_keywords/
    list_keywords gained keyword_id -- see that migration's comment); a
    bare int/str is also accepted so any caller that hasn't been touched
    yet still works. id=0/None (pre-migration rows never re-picked) is
    dropped rather than sent to TMDB, since 0 isn't a real keyword id and
    would just silently zero out the whole discover result set."""
    out = []
    for kw in keywords or []:
        kw_id = kw.get("id") if isinstance(kw, dict) else kw
        if kw_id:
            out.append(kw_id)
    return out


def _resolve_discover(media_types: list, genres: list, keywords: list, providers: list,
                       provider_mode: str, date_from: str, date_to: str) -> list:
    out = []
    keyword_ids = _keyword_ids(keywords)
    if "movie" in media_types:
        for m in tmdb.discover("movie", date_from=date_from, date_to=date_to,
                                genres=genres, keywords=keyword_ids, providers=providers):
            out.append(_movie_to_release(m))
    if "tv" in media_types:
        seen_show_ids = set()
        # Pass 1: brand-new premieres in the window. Expanded to every
        # episode of the premiering season that also falls in the window
        # (not just the pilot), same reasoning as pass 2 below.
        for t in tmdb.discover("tv", date_from=date_from, date_to=date_to,
                                genres=genres, keywords=keyword_ids, providers=providers):
            seen_show_ids.add(t["id"])
            try:
                detail = tmdb.get_detail("tv", t["id"])
            except Exception:
                out.append(_tv_to_release(t))
                continue
            out.extend(_tv_episode_releases(detail, date_from, date_to, strict=False))
        # Pass 2: shows with *any* episode airing in the window (mostly
        # already-running shows whose premiere is long past) -- expanded to
        # every matching episode in the window, see module docstring and
        # _tv_episode_releases()'s docstring.
        for t in tmdb.discover("tv", date_from=date_from, date_to=date_to,
                                genres=genres, keywords=keyword_ids, providers=providers,
                                air_date=True):
            if t["id"] in seen_show_ids:
                continue
            seen_show_ids.add(t["id"])
            try:
                detail = tmdb.get_detail("tv", t["id"])
            except Exception:
                continue
            out.extend(_tv_episode_releases(detail, date_from, date_to, strict=True))
    if provider_mode == "exclude" and providers:
        excluded = {int(p) for p in providers}
        out = [r for r in out if not _title_uses_providers(r, excluded)]
    return out


def _title_uses_providers(release: dict, provider_ids: set) -> bool:
    try:
        titles = tmdb.get_title_providers(release["media_type"], release["tmdb_id"])
        return any(p["provider_id"] in provider_ids for p in titles)
    except Exception:
        return False


def _resolve_library(media_types: list, date_from: str, date_to: str) -> list:
    """Every title MediaScan knows is in the connected Jellyfin/Plex
    library, regardless of its release/episode date -- unlike "discover"
    (an inherently forward-looking TMDB query bounded to the lookahead
    window), a "library" calendar is meant to show *everything* you
    already have (see this module's own top-of-file docstring), placed on
    the calendar at its real date so browsing to an earlier month still
    surfaces it. Movies sit on their original release_date; TV shows get
    one row per upcoming episode that falls in [date_from, date_to] (see
    _tv_episode_releases()), or -- if none do, e.g. an ended series, or a
    still-running one with nothing airing in this particular window --
    their bare next-episode/first_air_date row so they never just
    disappear (strict=False).

    Previously this filtered down to only entries whose date fell inside
    the calendar's date_from/date_to window, which -- combined with most
    library content already having a release date in the past -- silently
    reduced most "library" calendars to nothing. See _library_tmdb_ids_by_type()'s
    docstring for why this is looked up per media_type rather than one
    shared id list queried against both /movie/<id> and /tv/<id>.
    """
    out = []
    for media_type in media_types:
        ids = list(_library_tmdb_ids_by_type(media_type))[:_LIBRARY_SOURCE_CAP]
        # INFO (not debug) on purpose -- this is the one place that tells us
        # whether an empty "Mediathek" calendar is an empty MediaScan lookup
        # (0 ids here -- check use_library()/get_mediascan_ids_by_type) or a
        # per-id TMDB failure further down (ids > 0 but out stays empty --
        # check the warnings logged below).
        logger.info("[MediaCalendar] Library resolve: %d %s id(s) from MediaScan", len(ids), media_type)
        failures = 0
        for tmdb_id in ids:
            try:
                detail = tmdb.get_detail(media_type, int(tmdb_id))
            except Exception as exc:
                failures += 1
                logger.warning(
                    "[MediaCalendar] Library resolve: get_detail(%s, %s) failed: %s",
                    media_type, tmdb_id, exc)
                continue
            if media_type == "movie":
                out.append(_movie_to_release(detail))
            else:
                out.extend(_tv_episode_releases(detail, date_from, date_to, strict=False))
        if failures:
            logger.warning(
                "[MediaCalendar] Library resolve: %d/%d %s id(s) failed TMDB lookup",
                failures, len(ids), media_type)
    return out


def _resolve_list_items(list_row: dict, date_from: str, date_to: str) -> list:
    out = []
    for item in list_row.get("items", []):
        media_type = item["media_type"]
        try:
            detail = tmdb.get_detail(media_type, item["tmdb_id"])
        except Exception:
            continue
        if media_type == "movie":
            out.append(_movie_to_release(detail))
        else:
            # A manually-added TV title always stays on the list regardless
            # of date (same convention as _resolve_library) -- strict=False
            # so it still shows a fallback row even with nothing airing in
            # this particular window, but expands to every matching episode
            # when there is one (see _tv_episode_releases()'s docstring).
            out.extend(_tv_episode_releases(detail, date_from, date_to, strict=False))
    return out


def resolve_list_dynamic(list_row: dict, date_from: str, date_to: str) -> list:
    """Discover-style matches for a list's own dynamic filter (genres/
    keywords/providers/media_types) -- used both when a list is displayed
    on its own and when a calendar folds a list in/out."""
    if not list_row.get("dynamic_enabled"):
        return []
    return _resolve_discover(
        list_row.get("media_types") or ["movie", "tv"],
        list_row.get("genres", []), list_row.get("keywords", []),
        list_row.get("providers", []), "include", date_from, date_to,
    )


def _dedupe(releases: list) -> list:
    seen = set()
    out = []
    for r in releases:
        key = (r["tmdb_id"], r["media_type"], r.get("season_number", -1), r.get("episode_number", -1))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _release_key(r: dict) -> tuple:
    return (r["tmdb_id"], r["media_type"])


# --- Top-level calendar resolution -----------------------------------------

def resolve_calendar(calendar_id: int, force_refresh: bool = False) -> dict:
    """Returns {"releases": [...], "error": str|None}. Releases are cached
    per-calendar in mcdb (see db.get_cached_releases/replace_cached_releases)
    for cache_hours() hours, mirroring the Android app's cache model."""
    calendar = mcdb.get_calendar(calendar_id)
    if not calendar:
        return {"releases": [], "error": "Kalender nicht gefunden."}

    if not force_refresh:
        cached = mcdb.get_cached_releases(calendar_id, cache_hours() * 3600)
        if cached is not None:
            return {"releases": _postprocess(calendar, cached, from_cache=True), "error": None}

    if not tmdb.is_configured():
        return {"releases": [], "error": "TMDB ist nicht konfiguriert."}

    today = date.today()
    date_from = (today - timedelta(weeks=lookback_weeks())).isoformat()
    date_to = (today + timedelta(weeks=lookahead_weeks())).isoformat()
    media_types = calendar["media_types"] or ["movie", "tv"]

    try:
        if calendar["source"] == "list":
            releases = []
            for list_id in calendar["list_ids"].get("source", []):
                list_row = mcdb.get_list(list_id)
                if not list_row:
                    continue
                releases.extend(_resolve_list_items(list_row, date_from, date_to))
                releases.extend(resolve_list_dynamic(list_row, date_from, date_to))
            if calendar["combine_list_with_discover"]:
                releases.extend(_resolve_discover(
                    media_types, calendar["genres"], calendar["keywords"],
                    calendar["providers"], calendar["provider_filter_mode"],
                    date_from, date_to))
        elif calendar["source"] == "library":
            releases = _resolve_library(media_types, date_from, date_to)
        else:
            releases = _resolve_discover(
                media_types, calendar["genres"], calendar["keywords"],
                calendar["providers"], calendar["provider_filter_mode"],
                date_from, date_to)
    except tmdb.TmdbNotConfigured as exc:
        return {"releases": [], "error": str(exc)}
    except tmdb.TmdbError as exc:
        logger.warning("[MediaCalendar] TMDB error resolving calendar %s: %s", calendar_id, exc)
        return {"releases": [], "error": str(exc)}

    # Fold in positive lists, subtract negative lists.
    for list_id in calendar["list_ids"].get("positive", []):
        list_row = mcdb.get_list(list_id)
        if list_row:
            releases.extend(_resolve_list_items(list_row, date_from, date_to))
            releases.extend(resolve_list_dynamic(list_row, date_from, date_to))
    negative_keys = set()
    for list_id in calendar["list_ids"].get("negative", []):
        list_row = mcdb.get_list(list_id)
        if list_row:
            negative_keys.update((it["tmdb_id"], it["media_type"]) for it in list_row.get("items", []))

    # Manual include (always present) / excluded (always dropped).
    for ref in calendar["manual"]:
        try:
            detail = tmdb.get_detail(ref["media_type"], ref["tmdb_id"])
            if ref["media_type"] == "movie":
                releases.append(_movie_to_release(detail))
            else:
                # Always-included titles stay on regardless of date (same
                # convention as _resolve_library/_resolve_list_items), but
                # now expand to every matching episode in the window too.
                releases.extend(_tv_episode_releases(detail, date_from, date_to, strict=False))
        except Exception:
            continue
    excluded_keys = {(r["tmdb_id"], r["media_type"]) for r in calendar["excluded"]} | negative_keys

    releases = _dedupe(releases)
    releases = [r for r in releases if _release_key(r) not in excluded_keys]
    releases.sort(key=lambda r: r.get("release_date") or "9999")

    # Attach genres_json/providers_json + library/seerr status for caching.
    library_ids = _library_tmdb_ids()
    requested_ids = _seerr_requested_tmdb_ids() if calendar["seerr_filter"] != "any" else set()
    import json as _json
    for r in releases:
        r["genres_json"] = _json.dumps(r.pop("genre_ids", []))
        try:
            providers = tmdb.get_title_providers(r["media_type"], r["tmdb_id"])
            r["providers_json"] = _json.dumps([
                {"id": p["provider_id"], "name": p["provider_name"], "logo": p.get("logo_path")}
                for p in providers
            ])
        except Exception:
            r["providers_json"] = "[]"
        r["in_library"] = 1 if str(r["tmdb_id"]) in library_ids else 0
        r["requested"] = 1 if str(r["tmdb_id"]) in requested_ids else None

    mcdb.replace_cached_releases(calendar_id, releases)
    return {"releases": _postprocess(calendar, releases, from_cache=False), "error": None}


def _postprocess(calendar: dict, releases: list, from_cache: bool) -> list:
    """Apply the calendar's own library_filter/seerr_filter to the
    (already status-tagged) cached rows, and merge in watched/hidden state,
    plus three more status pills that are deliberately computed fresh here
    rather than persisted into cached_releases (see replace_cached_releases):
    AutoSync coverage and planned-download status can both change without a
    calendar refresh (someone adds/removes an AutoSync job, or the planned-
    download worker finds a match, independently of TMDB), and "is this a
    series premiere" is a pure function of season/episode -- none of the
    three need to survive a cache round-trip, so computing them on every
    call (cache hit or not) keeps them from ever going stale for
    cache_hours() hours at a time. Kept separate from resolve_calendar's
    fetch path so filter changes that don't need a re-fetch (just
    library_filter/seerr_filter) are instant."""
    progress = mcdb.get_all_progress()
    planned = mcdb.get_all_planned_downloads()
    autosync_titles = _autosync_titles()
    out = []
    for r in releases:
        r = dict(r)
        key = (r["tmdb_id"], r["media_type"], r.get("season_number", -1), r.get("episode_number", -1))
        p = progress.get(key, {"watched": False, "hidden": False})
        r["watched"] = p["watched"]
        r["hidden"] = p["hidden"]
        # "Neu" -- a TV series premiere (first episode of season 1), not
        # just any new episode of an already-running show. Movies have no
        # equivalent "premiere vs. later episode" distinction, so this
        # never applies to them.
        r["is_new"] = (
            r["media_type"] == "tv"
            and r.get("season_number", -1) in (1, -1)
            and r.get("episode_number", -1) in (0, 1)
        )
        r["in_autosync"] = _normalize_title(r.get("title")) in autosync_titles
        planned_row = planned.get(key)
        r["planned_download"] = planned_row["status"] if planned_row else None
        # Carried along so the calendar UI can prefill its "Plan auto-
        # download" config modal with the already-chosen language/path when
        # editing an existing planned row, without a second round-trip (see
        # static/mediacalendar.js's McPlanned.openConfig()).
        r["planned_language"] = planned_row["language"] if planned_row else None
        r["planned_custom_path_id"] = planned_row["custom_path_id"] if planned_row else None
        if calendar["library_filter"] == "in_library" and not r.get("in_library"):
            continue
        if calendar["library_filter"] == "missing" and r.get("in_library"):
            continue
        if calendar["seerr_filter"] == "requested" and not r.get("requested"):
            continue
        if calendar["seerr_filter"] == "not_requested" and r.get("requested"):
            continue
        out.append(r)
    return out


# --- Watched / hidden -------------------------------------------------------

def mark_watched(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                  watched: bool) -> None:
    mcdb.set_progress(tmdb_id, media_type, season_number, episode_number, watched=watched)


def mark_hidden(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                 hidden: bool) -> None:
    mcdb.set_progress(tmdb_id, media_type, season_number, episode_number, hidden=hidden)


# --- Background reminder notifications --------------------------------------
# Self-contained periodic thread (started once from __init__.py's
# register(app)) instead of hooking into any core scheduler -- keeps the
# whole feature inside this folder. Mirrors the shape of MediaForge's own
# background workers (autosync_worker.py etc.) just scoped locally.

_worker_started = False
_worker_lock = threading.Lock()
_NOTIFIED_CACHE_MAX = 500
_notified_keys: "set" = set()


def _check_and_notify() -> None:
    if not module_enabled() or not notify_enabled() or not tmdb.is_configured():
        return
    from ....notifications import notify_all
    lead = notify_lead_days()
    target_date = (date.today() + timedelta(days=lead)).isoformat()
    for calendar in mcdb.list_calendars():
        result = resolve_calendar(calendar["id"], force_refresh=False)
        for r in result["releases"]:
            if r.get("release_date") != target_date or r.get("hidden"):
                continue
            key = (r["tmdb_id"], r["media_type"], r.get("season_number", -1), r.get("episode_number", -1))
            if key in _notified_keys:
                continue
            is_episode = r["media_type"] == "tv" and r.get("episode_number", -1) >= 0
            title = r["title"]
            if is_episode and r.get("episode_title"):
                title = f"{r['title']} - {r['episode_title']}"
            try:
                notify_all(
                    title="Media Kalender",
                    body=f"{title} erscheint in {lead} Tag(en) ({r.get('release_date')})",
                    event="mediacalendar_upcoming",
                )
            except Exception:
                logger.exception("[MediaCalendar] notify_all failed for %s", title)
            _notified_keys.add(key)
    if len(_notified_keys) > _NOTIFIED_CACHE_MAX:
        # Drop the oldest half rather than growing forever -- exact
        # eviction order doesn't matter, this is just a de-dup guard.
        for k in list(_notified_keys)[:_NOTIFIED_CACHE_MAX // 2]:
            _notified_keys.discard(k)


def _worker_loop() -> None:
    while True:
        try:
            _check_and_notify()
        except Exception:
            logger.exception("[MediaCalendar] Reminder check failed")
        time.sleep(3600)  # hourly is plenty for a day-granularity reminder


def start_background_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker_loop, name="mediacalendar-reminders", daemon=True).start()


# --- Background calendar auto-refresh ----------------------------------------
# Without this, a calendar's cached_releases only ever gets re-resolved when
# someone actually opens its detail view after cache_hours() has elapsed (or
# hits "Refresh" by hand) -- _check_and_notify() above *does* incidentally
# re-resolve every calendar too, but only runs at all when reminder
# notifications are enabled (mediacalendar_notify_enabled), so anyone who
# doesn't use that feature never got any background refresh at all. This
# runs independently of that setting, on its own shorter cadence, so a
# calendar's cache_hours() expiry gets picked up promptly in the background
# instead of waiting for the next page visit (or, worse, staying stale
# indefinitely if nobody opens that particular calendar for a while).
# force_refresh=False, same as _check_and_notify() -- resolve_calendar()
# itself is the thing that decides whether cache_hours() has actually
# elapsed, so this is a cheap no-op for every calendar not yet due.
_CALENDAR_REFRESH_INTERVAL = 1800  # 30 min


def _auto_refresh_calendars() -> None:
    if not module_enabled() or not tmdb.is_configured():
        return
    for calendar in mcdb.list_calendars():
        try:
            resolve_calendar(calendar["id"], force_refresh=False)
        except Exception:
            logger.exception("[MediaCalendar] Auto-refresh failed for calendar %s", calendar.get("id"))


def _calendar_refresh_worker_loop() -> None:
    while True:
        try:
            _auto_refresh_calendars()
        except Exception:
            logger.exception("[MediaCalendar] Calendar auto-refresh pass failed")
        time.sleep(_CALENDAR_REFRESH_INTERVAL)


_calendar_refresh_worker_started = False
_calendar_refresh_worker_lock = threading.Lock()


def start_calendar_refresh_worker() -> None:
    global _calendar_refresh_worker_started
    with _calendar_refresh_worker_lock:
        if _calendar_refresh_worker_started:
            return
        _calendar_refresh_worker_started = True
        threading.Thread(
            target=_calendar_refresh_worker_loop, name="mediacalendar-auto-refresh", daemon=True,
        ).start()


# --- Planned downloads ("auto-download once available") ---------------------
# A release the user flagged via the "Planned Download" pill/action (see
# db.py's planned_downloads table) isn't found on any site yet at flag time
# -- that's the whole point, it's for something that doesn't exist as a
# scrapeable page yet (e.g. an announced-but-unreleased episode/movie).
# This worker re-checks every pending, due (release_date already reached)
# entry once an hour, reusing web/routes/autosync.py's find_site_candidates()
# (the same fuzzy AniWorld/S.TO/MegaKino/hanime search Auto-Sync's own
# "add from library" flow uses) -- the moment a good-enough match shows up,
# it's turned into a real AutoSync job automatically, no user action needed.

_PLANNED_MATCH_THRESHOLD = 0.72
_PLANNED_MAX_ATTEMPTS = 30  # ~30 hourly attempts (~1.25 days) before giving up


def _current_release_date(row: dict) -> "str | None":
    """The TMDB-authoritative release/air date for a planned-download row
    *right now* -- used to catch a delayed/rescheduled episode (or, more
    rarely, movie) so its stored release_date doesn't stay stuck on
    whatever it was at flag time. Movies use get_detail's release_date;
    TV uses the specific episode out of get_season_episodes() (the same
    per-season lookup _tv_episode_releases() uses for calendar expansion,
    so it's already 12h-cached either way). None if it can't be
    determined (network hiccup, season/episode no longer listed, etc.) --
    callers treat that as "no change", never as "clear the date"."""
    if row["media_type"] == "movie":
        try:
            detail = tmdb.get_detail("movie", row["tmdb_id"])
        except Exception:
            return None
        return detail.get("release_date") or None
    if row.get("season_number", -1) < 0:
        return None
    try:
        episodes = tmdb.get_season_episodes(row["tmdb_id"], row["season_number"])
    except Exception:
        return None
    for ep in episodes:
        if ep.get("episode_number") == row.get("episode_number"):
            return ep.get("air_date") or None
    return None


def _resync_planned_release_dates() -> None:
    """TMDB commonly reschedules an anime/show episode's air_date *after*
    it's already been flagged as a planned download (delays, hiatuses).
    Left unhandled, that's two separate problems: the stored release_date
    stays stale, so _check_planned_downloads()'s hourly site search starts
    (and keeps failing) before the episode is actually out, eventually
    giving up and marking it 'failed' via _PLANNED_MAX_ATTEMPTS -- purely
    because of a wrong date, not a genuine no-match; and once 'failed',
    list_pending_planned_downloads() never looks at it again, so a show
    that later actually airs is silently never auto-downloaded.

    Re-checks every 'pending'/'failed' row's current TMDB date once an
    hour (folded into the same worker loop/cadence as the site search
    itself, see _planned_download_worker_loop()) and, on a genuine change:
    updates the stored release_date either way, and revives a 'failed' row
    back to 'pending' with attempts cleared -- the stale date, not a real
    no-match, may well be why it was given up on. A 'pending' row simply
    getting its date updated self-corrects for free: once the new date is
    in the future, list_pending_planned_downloads()'s own release_date<=
    today gate stops it from being "due" again until the real date
    arrives -- no extra bookkeeping needed here.
    """
    for row in mcdb.list_active_planned_downloads():
        new_date = _current_release_date(row)
        if not new_date or new_date == row.get("release_date"):
            continue
        try:
            mcdb.update_planned_download_date(
                row["tmdb_id"], row["media_type"], row["season_number"], row["episode_number"],
                new_date, revive=(row["status"] == "failed"),
            )
        except Exception:
            logger.exception(
                "[MediaCalendar] Failed to resync release_date for planned download %s", row.get("title"))


def _find_and_activate_autosync(title: str, language: "str | None", custom_path_id) -> "dict | None":
    """Core of "is this title available under the configured conditions,
    and if so, actually start the download" -- shared by the hourly
    automatic worker (_check_planned_downloads(), below) and the manual
    "Add to Auto Sync" button (add_planned_download_to_autosync(), used by
    static/mediacalendar.js's McPlanned "Add to Auto Sync" row action).

    Finds a confident site match, creates (or reuses) an AutoSync job for
    it with the given language/custom_path_id -- "the stated conditions" --
    then immediately runs that job once via _run_autosync_for_job(). That
    function is AutoSync's own real per-episode/language availability check
    plus queueing step (autosync_worker.py -- it decides "is this episode
    actually there yet, in this language" and, if so, hands it to
    add_to_queue(), the normal Downloader), the exact same path a regular
    recurring AutoSync job uses. Running it here rather than only creating
    the job matters: add_autosync_job() deliberately sets last_check to now
    (see its own docstring) so the *next* scheduled AutoSync cycle doesn't
    immediately re-trigger a duplicate -- which otherwise meant a freshly
    matched planned download just sat there until AutoSync's own interval
    came around on its own, instead of downloading right away. Applies
    equally to a whole-series planned entry and a single flagged episode --
    both are just rows in the same table and go through the exact same
    job-run call here.

    Returns {"job_id", "url", "score"} on success, None if no confident
    site match was found (the caller decides what "no match" means for
    its own bookkeeping -- e.g. attempts/failed status)."""
    from ...db import add_autosync_job, find_autosync_by_url, get_autosync_job
    from ...routes.autosync import find_site_candidates
    from ...autosync_worker import _run_autosync_for_job

    try:
        candidates = find_site_candidates(title)
    except Exception:
        logger.exception("[MediaCalendar] Planned-download site search failed for %s", title)
        candidates = []

    best = candidates[0] if candidates else None
    if not best or best["score"] < _PLANNED_MATCH_THRESHOLD:
        return None

    existing = find_autosync_by_url(best["url"])
    if existing:
        job_id = existing["id"]
    else:
        # language/custom_path_id come from whatever the user picked in the
        # "Plan auto-download" / "Planned Downloads" config (see db.py's
        # add_planned_download and static/mediacalendar.js's McPlanned
        # module) -- only applied here, on first job creation; if a job for
        # this series URL already exists (the `if existing:` branch above),
        # its settings win instead, same as everywhere else in this
        # codebase that reuses a job by URL.
        try:
            job_id = add_autosync_job(
                title=title, series_url=best["url"],
                language=language or "German Dub", provider="VOE",
                custom_path_id=custom_path_id, added_by=None,
            )
        except Exception:
            logger.exception("[MediaCalendar] Failed to create AutoSync job for %s", title)
            return None

    job = get_autosync_job(job_id)
    if job:
        try:
            _run_autosync_for_job(job)
        except Exception:
            logger.exception(
                "[MediaCalendar] Immediate Auto-Sync run failed for job %s (%s) -- "
                "will still be picked up on its next regular schedule", job_id, title)

    return {"job_id": job_id, "url": best["url"], "score": best["score"]}


def add_title_to_autosync(title: "str | None", language: "str | None", custom_path_id) -> dict:
    """"Add to Auto Sync" -- the alternative to "Plan auto-download" offered
    on a calendar release (static/mediacalendar.js's mcCalActionsHtml
    "add-autosync" button): instead of flagging the release and waiting
    (checked hourly) until it actually appears on a site, this searches
    AniWorld/S.TO/MegaKino right now and, on a confident match, sets up
    (or reuses) a normal, ongoing Auto-Sync job for the whole series
    immediately -- no planned_downloads row involved at all, since the
    point here is explicitly to skip the "wait for this one release"
    mechanism entirely. Returns {"ok": True, "job_id", "url"} or
    {"ok": False, "error": "no_title" | "no_match"}."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "no_title"}
    result = _find_and_activate_autosync(title, language, custom_path_id)
    if not result:
        return {"ok": False, "error": "no_match"}
    return {"ok": True, "job_id": result["job_id"], "url": result["url"]}


def _check_planned_downloads() -> None:
    if not module_enabled():
        return

    _resync_planned_release_dates()

    for row in mcdb.list_pending_planned_downloads():
        title = (row.get("title") or "").strip()
        if not title:
            continue
        key = (row["tmdb_id"], row["media_type"], row["season_number"], row["episode_number"])
        result = _find_and_activate_autosync(title, row.get("language"), row.get("custom_path_id"))
        if result:
            mcdb.mark_planned_download_result(
                *key, status="queued", autosync_job_id=result["job_id"], increment_attempts=True)
            try:
                from ....notifications import notify_all
                notify_all(
                    title="Media Kalender",
                    body=f"{title} wurde gefunden und automatisch zu Auto-Sync hinzugefügt.",
                    event="mediacalendar_planned_found",
                )
            except Exception:
                logger.exception("[MediaCalendar] notify_all failed for planned download %s", title)
        else:
            attempts = row.get("attempts", 0) + 1
            new_status = "failed" if attempts >= _PLANNED_MAX_ATTEMPTS else None
            mcdb.mark_planned_download_result(*key, status=new_status, increment_attempts=True)


def _planned_download_worker_loop() -> None:
    while True:
        try:
            _check_planned_downloads()
        except Exception:
            logger.exception("[MediaCalendar] Planned-download check failed")
        time.sleep(3600)  # hourly, matching the user-facing "checked hourly from the due date on" behaviour


_planned_worker_started = False
_planned_worker_lock = threading.Lock()


def start_planned_download_worker() -> None:
    global _planned_worker_started
    with _planned_worker_lock:
        if _planned_worker_started:
            return
        _planned_worker_started = True
        threading.Thread(
            target=_planned_download_worker_loop, name="mediacalendar-planned-downloads", daemon=True,
        ).start()
