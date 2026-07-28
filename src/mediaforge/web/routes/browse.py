"""Browse lists + prefetch worker.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...home_feed import iter_home_feed_sources
from ...providers import resolve_provider
from ...search import fetch_hanime_new
from ...search import fetch_hanime_trending
from ...search import fetch_megakino_new_movies
from ...search import fetch_megakino_new_series
from ...search import fetch_megakino_popular_movies
from ...search import fetch_megakino_popular_series
from ...search import fetch_new_animes
from ...search import fetch_new_series
from ...search import fetch_popular_animes
from ...search import fetch_popular_series
from ...search import random_anime
from ..db import get_browse_cache_stale
from ..db import get_custom_paths
from ..db import get_setting
from ..lang_folders import LANG_FOLDERS
from ..db import get_tmdb_cache
from ..db import set_browse_cache
from ..queue_worker import _hanime_enabled
from ..queue_worker import _is_filmpalast_url
from flask import jsonify
from flask import request
import os
import threading
import time
from ..tmdb_cache import _tmdb_lookup_cached
from .image_proxy import _img_pool
from .image_proxy import _precache_image_bg
from .image_proxy import _poster_proxy
from .image_proxy import _proxy_result_list
from ...logger import get_logger


logger = get_logger(__name__)


import time as _time
from collections import OrderedDict as _OD

_BROWSE_CACHE_MAX = 50     # hard cap; evicts LRU entry when exceeded
_browse_cache: "_OD" = _OD()
_BROWSE_TTL = 3600  # 1 hour

# Bump when the SHAPE of a cached card changes -- a renamed field, a different
# source for one of them, anything that makes an old entry wrong rather than
# merely outdated. The value becomes part of the cache key, so stale entries
# are simply never read again instead of being served for up to an hour from
# memory and, worse, immediately after every restart from SQLite (the DB half
# is stale-while-revalidate on purpose).
#
# v2: hanime cards take their artwork from cover_url; poster_url turned into a
#     1920x1080 scene still on the new catalogue backend.
# v3: hanime listings are filled up to a full grid after franchise grouping
#     and the censored/uncensored filter.
_CARD_SCHEMA = "v3"
_browse_refresh_locks: dict = {}
_browse_refresh_mutex = threading.Lock()

# Background prefetch worker cadence (moved from create_app).
_PREFETCH_INTERVAL = 15 * 60   # seconds between cycles
_PREFETCH_STARTUP  = 3         # initial delay to let server fully start
_PREFETCH_RATE     = 0.4       # seconds between per-entry TMDB calls


def _browse_cache_set(k, v):
    """Insert/update key with LRU eviction when the cap is reached."""
    _browse_cache.pop(k, None)      # move to end on update
    _browse_cache[k] = v
    while len(_browse_cache) > _BROWSE_CACHE_MAX:
        _browse_cache.popitem(last=False)  # evict oldest


def _cached_browse(key, fetch_fn):
    # Every lookup and every write goes through the versioned key (see
    # _CARD_SCHEMA), so a card-shape change invalidates the old entries
    # everywhere at once -- memory and DB.
    key = _CARD_SCHEMA + ":" + key
    now = _time.time()
    # 1. In-memory fast path
    entry = _browse_cache.get(key)
    if entry and now - entry[0] < _BROWSE_TTL:
        return entry[1]

    # 2. If nothing in memory, try SQLite (survives restarts)
    if entry is None:
        db_row = get_browse_cache_stale(key)
        if db_row:
            data, cached_at = db_row
            _browse_cache_set(key, (cached_at, data))
            entry = _browse_cache[key]

    # 3. Still fresh after DB load?
    if entry and now - entry[0] < _BROWSE_TTL:
        return entry[1]

    # 4. Stale or missing — avoid duplicate concurrent refreshes
    with _browse_refresh_mutex:
        already_refreshing = key in _browse_refresh_locks
        if not already_refreshing:
            _browse_refresh_locks[key] = True

    if entry is not None:
        # Stale-while-revalidate: serve old data immediately, refresh in background
        if not already_refreshing:
            def _bg_refresh(k=key, fn=fetch_fn):
                try:
                    results = fn()
                    if results:
                        _browse_cache_set(k, (_time.time(), results))
                        set_browse_cache(k, results)
                finally:
                    with _browse_refresh_mutex:
                        _browse_refresh_locks.pop(k, None)
            threading.Thread(target=_bg_refresh, daemon=True,
                             name=f"browse-refresh-{key}").start()
        return entry[1]

    # 5. Cold start — no cached data at all; fetch in a background thread and
    #    wait up to 10 s so the request thread is not blocked indefinitely.
    _cold_done = threading.Event()
    _cold_result = [None]

    def _cold_fetch(k=key, fn=fetch_fn, ev=_cold_done, out=_cold_result):
        try:
            r = fn()
            if r is not None:
                _browse_cache_set(k, (_time.time(), r))
                set_browse_cache(k, r)
                out[0] = r
        finally:
            ev.set()
            with _browse_refresh_mutex:
                _browse_refresh_locks.pop(k, None)

    threading.Thread(target=_cold_fetch, daemon=True,
                     name=f"browse-cold-{key}").start()
    _cold_done.wait(timeout=10)
    return _cold_result[0]


def _fetch_new_movies():
    """Scrape the FilmPalast homepage for new movies (filters out SxxExx series episodes)."""
    import re as _re2
    import requests as _req
    series_re = _re2.compile(r"\bS\d{2}E\d{2}\b", _re2.IGNORECASE)
    try:
        resp = _req.get(
            "https://filmpalast.to/",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Encoding": "gzip, deflate",
                "Accept-Language": "de-DE,de;q=0.9",
            },
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("FilmPalast new-movies scrape failed: %s", exc)
        return None

    anchors = _re2.findall(
        r'<a\s+href="//filmpalast\.to/stream/([a-zA-Z0-9\-]+)"\s+title="([^"]+)"',
        html,
    )
    imgs = _re2.findall(r'<img\s+src="(/files/movies/[^"]+)"', html)

    results = []
    seen = set()
    for i, (slug, title) in enumerate(anchors):
        if series_re.search(title):
            continue  # skip series episodes like "Show S04E01"
        url = f"https://filmpalast.to/stream/{slug}"
        if url in seen:
            continue
        seen.add(url)
        poster = f"https://filmpalast.to{imgs[i]}" if i < len(imgs) else ""
        results.append({"title": title, "url": url, "poster_url": poster, "genre": ""})
    return results


def _hanime_censorship_prefs():
    """Current censored/uncensored display prefs, and a short cache-key
    suffix so each filter combination gets its own browse-cache entry —
    otherwise toggling "Censored" in Settings would keep serving whatever
    combination happened to be cached first (see fetch_new/fetch_trending
    in hanime_tv/scraper.py, which now filter + backfill server-side)."""
    show_censored = get_setting("source_show_censored_hanime", "1") != "0"
    show_uncensored = get_setting("source_show_uncensored_hanime", "1") != "0"
    suffix = "_c%d_u%d" % (int(show_censored), int(show_uncensored))
    return show_censored, show_uncensored, suffix


def _prefetch_cycle():
    """One full pass: warm browse lists → pre-cache posters → fetch TMDB data."""
    api_key = get_setting("cineinfo_tmdb_api_key", "")
    country = get_setting("cineinfo_country", "DE")
    tmdb_on = bool(api_key)

    # Collect all cards from every browse category (uses in-process cache)
    browse_sources = [
        ("new_animes",     fetch_new_animes),
        ("popular_animes", fetch_popular_animes),
        ("new_series",     fetch_new_series),
        ("popular_series", fetch_popular_series),
        ("new_movies",     _fetch_new_movies),
        ("megakino_new_movies",    fetch_megakino_new_movies),
        ("megakino_popular_movies", fetch_megakino_popular_movies),
        ("megakino_new_series",    fetch_megakino_new_series),
        ("megakino_popular_series", fetch_megakino_popular_series),
    ]
    all_entries = []
    for bkey, fn in browse_sources:
        try:
            results = _cached_browse(bkey, fn)
            if results:
                all_entries.extend(results)
        except Exception as exc:
            logger.debug("[Prefetch] Browse %r failed: %s", bkey, exc)

    # Deduplicate by URL
    seen, unique = set(), []
    for e in all_entries:
        url = e.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(e)

    logger.info("[Prefetch] Warming cache for %d cards (TMDB: %s)", len(unique), tmdb_on)

    for entry in unique:
        url        = entry.get("url", "")
        title      = entry.get("title", "")
        poster_url = entry.get("poster_url", "")

        # Fire-and-forget poster pre-cache
        if poster_url:
            _img_pool.submit(_precache_image_bg, poster_url)

        if not (tmdb_on and title):
            continue

        # Skip if TMDB data already cached (title key, default de) and up to
        # date — get_tmdb_cache() already enforces the 24h TTL, so any
        # non-None row here is fresh. Don't force a live re-fetch just
        # because an older row predates trailer_key/recommendations; those
        # backfill for free once the row's TTL naturally expires (mirrors
        # the same fix in tmdb_cache.py's _tmdb_lookup_cached()).
        cached = get_tmdb_cache(title + "|||" + country + "|||de")
        if cached is not None:
            continue

        # Try to get IMDB ID from the series page for accurate matching
        imdb_id = None
        if not _is_filmpalast_url(url):
            try:
                prov   = resolve_provider(url)
                series = prov.series_cls(url=url)
                imdb_id = getattr(series, "imdb", None) or None
                # Also check the imdb_id-keyed cache entry
                if imdb_id and get_tmdb_cache(imdb_id + "|||" + country + "|||de") is not None:
                    time.sleep(_PREFETCH_RATE)
                    continue
            except Exception:
                pass  # fall through to title-only lookup

        _tmdb_lookup_cached(title, imdb_id, api_key, country)
        time.sleep(_PREFETCH_RATE)

    logger.info("[Prefetch] Cycle complete.")


def _prefetch_worker():
    time.sleep(_PREFETCH_STARTUP)
    while True:
        try:
            _prefetch_cycle()
        except Exception as exc:
            logger.warning("[Prefetch] Worker cycle error: %s", exc)
        time.sleep(_PREFETCH_INTERVAL)


def ensure_prefetch_worker():
    """Start the background browse/TMDB prefetch worker thread."""
    _pt = threading.Thread(target=_prefetch_worker, daemon=True, name="browse-prefetch")
    _pt.start()
    logger.info("[Prefetch] Background worker started (interval=%d min)", _PREFETCH_INTERVAL // 60)


# ── Home feed (the new home page) ────────────────────────────────────────
# The feed page used to fetch all eleven browse lists separately and assemble
# the rows in JavaScript. That cost eleven round-trips per visit and, worse,
# made the row layout a hardcoded list inside home_feed.js -- a module that
# registers a content source could never show up on the home page. Both have
# the same fix: build the rows here, from a registry (see
# mediaforge/home_feed.py's register_home_feed_source).

_FEED_ROW_ORDER = ("new", "popular", "movies")
_FEED_LIMIT_DEFAULT = 30
_FEED_LIMIT_MAX = 60

# id -> (label, chip colour). Only the built-ins; module sources bring their
# own label/colour through the registry.
_FEED_BUILTIN_META = (
    ("aniworld",   "AniWorld",     "#6aa9ff"),
    ("sto",        "SerienStream", "#8b7dff"),
    ("filmpalast", "FilmPalast",   "#ffb454"),
    ("megakino",   "MegaKino",     "#4ade80"),
    ("hanime",     "hanime",       "#ff6b9d"),
)


# ── Home feed layout (Settings -> Start Page) ────────────────────────────
# Two levels, on purpose: the admin sets what a fresh account sees, and every
# user may then overrule it for themselves. The rows are personal (one user's
# "Continue watching" says nothing to another), but the instance owner should
# still be able to decide that, say, the calendar row is off by default
# because the calendar is not configured.

# Every row the feed knows, and where its data comes from. The "hint" is what
# the heading shows next to its title ("from your favourites"), and "link" is
# where that hint points -- a row nobody can trace back to a page is a row
# people distrust.
_FEED_ROW_SOURCES = {
    "continue":  {"hint": "playback", "link": "/library"},
    "library":   {"hint": "library",  "link": "/library"},
    "watchlist": {"hint": "favourites", "link": "/favourites"},
    "upcoming":  {"hint": "calendar", "link": "/calendar"},
    "new":       {"hint": "sources",  "link": ""},
    "popular":   {"hint": "sources",  "link": ""},
    "movies":    {"hint": "sources",  "link": ""},
}
_FEED_PERSONAL_ROWS = ("continue", "library", "watchlist", "upcoming")
# The default reading order: discovery first (a fresh install has no
# playback history and an empty library, so leading with "Continue watching"
# greets a new user with two blank rows), then the two rows that are really
# other pages in miniature (watchlist, calendar), then the personal rows.
_FEED_DEFAULT_ORDER = ("new", "popular", "movies", "watchlist", "upcoming",
                       "continue", "library")
_FEED_CARDS_CHOICES = (10, 20, 30, 40, 60)


def _feed_clean_order(raw, fallback=None):
    """Parse a stored order string into a complete, duplicate-free row list.

    Anything unknown is dropped and anything missing is appended, so a stored
    order from an older build (or a hand-edited setting) can never make a row
    disappear silently -- it just ends up last.
    """
    out = []
    for part in str(raw or "").split(","):
        row = part.strip().lower()
        if row in _FEED_ROW_SOURCES and row not in out:
            out.append(row)
    for row in (fallback or _FEED_DEFAULT_ORDER):
        if row not in out:
            out.append(row)
    return out


def _feed_clean_list(raw, allowed):
    """Comma-separated ids, filtered against *allowed*."""
    out = []
    for part in str(raw or "").split(","):
        value = part.strip().lower()
        if value in allowed and value not in out:
            out.append(value)
    return out


def _feed_clean_limit(raw, fallback=_FEED_LIMIT_DEFAULT):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value in _FEED_CARDS_CHOICES else fallback


def feed_global_defaults():
    """The instance defaults an admin set under Settings -> Start Page."""
    return {
        "order": _feed_clean_order(get_setting("home_rows_order", "")),
        "hidden": _feed_clean_list(get_setting("home_rows_hidden", ""), _FEED_ROW_SOURCES),
        "limit": _feed_clean_limit(get_setting("home_cards_per_row", "")),
        "sources_off": _feed_clean_list(get_setting("home_default_sources_off", ""),
                                        _feed_known_source_ids()),
        "types_off": _feed_clean_list(get_setting("home_default_types_off", "adult"),
                                      ("series", "movies", "adult")),
    }


def _feed_user_prefs():
    """This user's stored UI preferences, or {} when nobody is logged in."""
    try:
        from flask import session as _session
        from ..db import get_user_ui_prefs
        uid = _session.get("user_id")
        if uid is None:
            return {}
        return get_user_ui_prefs(uid) or {}
    except Exception:
        return {}


def _feed_parse_layout(raw):
    """"o:<order>;h:<hidden>;n:<limit>" -> dict of the parts that were set.

    Only the parts actually present are returned, so a user who reordered the
    rows still follows the instance default for everything else.
    """
    out = {}
    for part in str(raw or "").split(";"):
        key, _, value = part.partition(":")
        key = key.strip()
        if key == "o" and value.strip():
            out["order"] = _feed_clean_order(value)
        elif key == "h":
            out["hidden"] = _feed_clean_list(value, _FEED_ROW_SOURCES)
        elif key == "n" and value.strip():
            out["limit"] = _feed_clean_limit(value)
    return out


def feed_effective_config():
    """What this user's home page actually looks like: the instance defaults
    with the user's own overrides applied on top. `overridden` names the parts
    the user changed, so the Start Page settings can say so."""
    cfg = feed_global_defaults()
    layout = _feed_parse_layout(_feed_user_prefs().get("home_feed_layout"))
    overridden = sorted(layout)
    cfg.update(layout)
    cfg["overridden"] = overridden
    cfg["rows"] = [
        {
            "id": row,
            "hint": _FEED_ROW_SOURCES[row]["hint"],
            "link": _FEED_ROW_SOURCES[row]["link"],
            "personal": row in _FEED_PERSONAL_ROWS,
            "visible": row not in cfg["hidden"],
        }
        for row in cfg["order"]
    ]
    return cfg


def _feed_known_source_ids():
    """Built-in plus module-registered source ids -- used to validate the
    stored "off by default" list."""
    ids = {sid for sid, _label, _color in _FEED_BUILTIN_META}
    for src in iter_home_feed_sources():
        ids.add(src["source_id"])
    return ids


def _feed_builtin_entries():
    """(source_id, row, media_type, cache_key, fetch_fn) for every built-in
    list. The cache keys are the same ones the single-list routes above use,
    so one scrape feeds both home pages instead of two."""
    show_c, show_u, suffix = _hanime_censorship_prefs()
    return [
        ("aniworld",   "new",     "series", "new_animes",              fetch_new_animes),
        ("aniworld",   "popular", "series", "popular_animes",          fetch_popular_animes),
        ("sto",        "new",     "series", "new_series",              fetch_new_series),
        ("sto",        "popular", "series", "popular_series",          fetch_popular_series),
        ("filmpalast", "new",     "movies", "new_movies",              _fetch_new_movies),
        ("megakino",   "new",     "movies", "megakino_new_movies",     fetch_megakino_new_movies),
        ("megakino",   "popular", "movies", "megakino_popular_movies", fetch_megakino_popular_movies),
        ("megakino",   "new",     "series", "megakino_new_series",     fetch_megakino_new_series),
        ("megakino",   "popular", "series", "megakino_popular_series", fetch_megakino_popular_series),
        ("hanime",     "new",     "adult",  "hanime_new" + suffix,
         lambda: fetch_hanime_new(show_censored=show_c, show_uncensored=show_u)),
        ("hanime",     "popular", "adult",  "hanime_trending" + suffix,
         lambda: fetch_hanime_trending(show_censored=show_c, show_uncensored=show_u)),
    ]


def _feed_source_enabled(source_id):
    """Same rule the settings page writes: every source is opt-out except the
    adult one, which is opt-in. Module sources default to on -- a module that
    registered a source was installed on purpose."""
    default = "0" if source_id == "hanime" else "1"
    return get_setting("source_enabled_" + source_id, default) != "0"


def _feed_norm_title(value):
    """Loose title key, so "Re:Zero" and "Re Zero" from two sites collapse
    into one card instead of two."""
    return "".join(c for c in str(value or "").lower() if c.isalnum())


def _feed_collect(bucket, order_ids, limit, taken, index, labels):
    """Round-robin one row out of {source_id: [items]}.

    Round-robin so a row never opens with twenty AniWorld cards. `taken`
    carries across rows, which is what keeps a title from appearing in "New"
    *and* "Popular" *and* "Movies" -- it lands in the first row it qualifies
    for and nowhere else. A title that several sources have becomes one card
    that names the others in `also`, so the click can still go elsewhere.
    """
    out = []
    depth = 0
    while len(out) < limit:
        progressed = False
        for sid in order_ids:
            items = bucket.get(sid) or []
            if depth >= len(items):
                continue
            progressed = True
            item = items[depth]
            key = _feed_norm_title(item.get("title")) + "|" + item.get("media_type", "")
            if key in taken:
                first = index.get(key)
                if first is not None and first.get("source") != sid:
                    if all(a.get("source") != sid for a in first["also"]):
                        first["also"].append({
                            "source": sid,
                            "label": labels.get(sid, sid),
                            "url": item.get("url", ""),
                        })
                continue
            taken.add(key)
            card = dict(item)
            card["also"] = []
            index[key] = card
            out.append(card)
            if len(out) >= limit:
                break
        if not progressed:
            break
        depth += 1
    return out


def register_browse_routes(app):
    """Register all browse/discovery routes (anime, series, movie listings,
    hanime, and the local downloaded-folders lookup) on the Flask app."""
    # Upstream failures answer 502, not 500: none of these routes is broken
    # when they fire -- the third-party site is unreachable or answered with
    # something unusable. 500 would claim MediaForge itself failed, which also
    # made the route smoke test (tests/test_routes_smoke.py) go red whenever a
    # source site had a bad day. The frontend only looks at the payload
    # ("results" present or not), so the status change is invisible to it.
    @app.route("/api/random")
    def api_random():
        """Return a random anime URL. GET /api/random.

        Backed by ``random_anime()``; not supported for the S.TO provider
        (query param ``site=sto`` is rejected with 400). No confirmed
        frontend caller was found in static/templates."""
        site = request.args.get("site", "aniworld").strip()
        if site == "sto":
            return jsonify({"error": "Random is not available for S.TO"}), 400
        url = random_anime()
        if url:
            return jsonify({"url": url})
        return jsonify({"error": "Failed to fetch random anime"}), 502
    @app.route("/api/new-animes")
    def api_new_animes():
        """Return the cached "new animes" browse list. GET /api/new-animes.

        Called from static/app.js's `loadAniworldBrowse()`."""
        results = _cached_browse("new_animes", fetch_new_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch new animes"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/popular-animes")
    def api_popular_animes():
        """Return the cached "popular animes" browse list. GET /api/popular-animes.

        Called from static/app.js's `loadAniworldBrowse()`."""
        results = _cached_browse("popular_animes", fetch_popular_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch popular animes"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/new-series")
    def api_new_series():
        """Return the cached "new series" browse list (S.TO). GET /api/new-series.

        Called from static/app.js's `loadStoBrowse()`."""
        results = _cached_browse("new_series", fetch_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch new series"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/popular-series")
    def api_popular_series():
        """Return the cached "popular series" browse list (S.TO). GET /api/popular-series.

        Called from static/app.js's `loadStoBrowse()`."""
        results = _cached_browse("popular_series", fetch_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch popular series"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/new-movies")
    def api_new_movies():
        """Return the cached "new movies" browse list (FilmPalast). GET /api/new-movies.

        Called from static/app.js's `loadFilmPalastBrowse()`."""
        results = _cached_browse("new_movies", _fetch_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch new movies"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/new-movies")
    def api_megakino_new_movies():
        """Return the cached Megakino "new movies" browse list. GET /api/megakino/new-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_new_movies", fetch_megakino_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino movies"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/popular-movies")
    def api_megakino_popular_movies():
        """Return the cached Megakino "popular movies" browse list. GET /api/megakino/popular-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_popular_movies", fetch_megakino_popular_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino popular movies"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/new-series")
    def api_megakino_new_series():
        """Return the cached Megakino "new series" browse list. GET /api/megakino/new-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_new_series", fetch_megakino_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino series"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/popular-series")
    def api_megakino_popular_series():
        """Return the cached Megakino "popular series" browse list. GET /api/megakino/popular-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_popular_series", fetch_megakino_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino popular series"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/hanime/new")
    def api_hanime_new():
        """Return the cached "new hanime" browse list, filtered by the
        censored/uncensored display prefs. GET /api/hanime/new.

        Returns an empty list unless the adult hanime source is explicitly
        enabled. Called from static/app.js's `loadHanimeBrowse()`."""
        # Adult source: only serve data when the user has explicitly enabled it.
        if not _hanime_enabled():
            return jsonify({"results": []})
        show_censored, show_uncensored, suffix = _hanime_censorship_prefs()
        results = _cached_browse(
            "hanime_new" + suffix,
            lambda: fetch_hanime_new(show_censored=show_censored, show_uncensored=show_uncensored),
        )
        if results is None:
            return jsonify({"error": "Failed to fetch hanime new"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/hanime/trending")
    def api_hanime_trending():
        """Return the cached "trending hanime" browse list, filtered by the
        censored/uncensored display prefs. GET /api/hanime/trending.

        Returns an empty list unless the adult hanime source is explicitly
        enabled. Called from static/app.js's `loadHanimeBrowse()`."""
        if not _hanime_enabled():
            return jsonify({"results": []})
        show_censored, show_uncensored, suffix = _hanime_censorship_prefs()
        results = _cached_browse(
            "hanime_trending" + suffix,
            lambda: fetch_hanime_trending(show_censored=show_censored, show_uncensored=show_uncensored),
        )
        if results is None:
            return jsonify({"error": "Failed to fetch hanime trending"}), 502
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/home-feed")
    def api_home_feed():
        """Return the complete new-home-page feed in one answer.
        GET /api/home-feed?adult=0|1&limit=30.

        Called from static/home_feed.js, which used to build this itself out
        of eleven separate requests. Rows are assembled server-side because
        that is the only place that knows which sources exist -- built-ins
        plus whatever modules registered (see mediaforge/home_feed.py).

        `adult=1` is what actually fetches the 18+ source; with the chip off
        the request never touches it, instead of fetching and hiding it.

        A source that fails upstream is reported in `errors` rather than
        silently contributing nothing -- "the site is down" and "nothing
        matches your filters" look identical otherwise, and only one of them
        is the user's doing.
        """
        want_adult = request.args.get("adult", "0") == "1"
        config = feed_effective_config()
        # An explicit ?limit wins (the settings page previews with it); the
        # configured cards-per-row is what the home page itself uses.
        if request.args.get("limit"):
            try:
                limit = int(request.args.get("limit"))
            except (TypeError, ValueError):
                limit = config["limit"]
        else:
            limit = config["limit"]
        limit = max(1, min(limit, _FEED_LIMIT_MAX))
        hidden = set(config["hidden"])

        # 1. Everything that could contribute: built-ins + module sources.
        entries = list(_feed_builtin_entries())
        meta = {}
        for sid, label, color in _FEED_BUILTIN_META:
            meta[sid] = {"id": sid, "label": label, "color": color,
                         "types": set(), "builtin": True}
        for src in iter_home_feed_sources():
            sid = src["source_id"]
            meta.setdefault(sid, {"id": sid, "label": src["label"],
                                  "color": src["color"], "types": set(),
                                  "builtin": False})
            for row, fn in src["fetchers"].items():
                entries.append((sid, row, src["media_type"],
                                "tp_%s_%s" % (sid, row), fn))

        for sid, _row, mtype, _key, _fn in entries:
            if sid in meta:
                meta[sid]["types"].add(mtype)

        # 2. Source order: the user's own (Settings -> Sources), unknown ids
        #    appended so a freshly installed module is visible without the
        #    user having to re-save the order first.
        order_raw = get_setting("home_source_order", "") or ""
        order = [s.strip().lower() for s in order_raw.split(",") if s.strip()]
        order = [s for s in order if s in meta]
        for sid in meta:
            if sid not in order:
                order.append(sid)

        enabled = {sid: _feed_source_enabled(sid) for sid in meta}

        def _wanted(sid, mtype):
            if not enabled.get(sid):
                return False
            if mtype == "adult" and not want_adult:
                return False
            return True

        # 3. Fetch in parallel. _cached_browse() needs no request context (it
        #    only touches the browse cache and the scrapers), so a worker pool
        #    is safe here and turns a cold start from "eleven timeouts in a
        #    row" into one.
        todo = [e for e in entries if _wanted(e[0], e[2])]
        fetched = {}
        failures = {}
        if todo:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(todo)),
                                    thread_name_prefix="home-feed") as pool:
                futures = {
                    pool.submit(_cached_browse, key, fn): (sid, row, mtype)
                    for (sid, row, mtype, key, fn) in todo
                }
                for fut, (sid, row, mtype) in futures.items():
                    try:
                        results = fut.result()
                    except Exception as exc:
                        logger.warning("[HomeFeed] %s/%s failed: %s", sid, row, exc)
                        results = None
                    if results is None:
                        failures.setdefault(sid, []).append(row)
                        continue
                    fetched.setdefault((row, sid), []).extend(
                        dict(r, source=sid, media_type=mtype) for r in results
                    )

        # 4. Proxy posters + inline cached TMDB, once per source list (both
        #    read settings, so this stays in the request thread).
        for key, items in fetched.items():
            fetched[key] = _proxy_result_list(items)

        # 5. Rows. `taken` spans all three, so nothing is shown twice.
        labels = {sid: meta[sid]["label"] for sid in meta}
        taken, index = set(), {}
        rows = {}
        for row in _FEED_ROW_ORDER:
            if row in hidden:
                # A row nobody sees is a row nobody has to pay for.
                rows[row] = []
                continue
            if row == "movies":
                bucket = {}
                for (r, sid), items in fetched.items():
                    movies = [i for i in items if i.get("media_type") == "movies"]
                    if movies:
                        bucket.setdefault(sid, []).extend(movies)
            else:
                bucket = {sid: items for (r, sid), items in fetched.items() if r == row}
            rows[row] = _feed_collect(bucket, order, limit, taken, index, labels)

        return jsonify({
            "rows": rows,
            "sources": [
                {
                    "id": sid,
                    "label": meta[sid]["label"],
                    "color": meta[sid]["color"],
                    "enabled": bool(enabled.get(sid)),
                    "types": sorted(meta[sid]["types"]),
                    "builtin": meta[sid]["builtin"],
                    "error": sid in failures,
                }
                for sid in order
            ],
            "errors": [
                {"source": sid, "label": labels.get(sid, sid), "rows": sorted(set(rws))}
                for sid, rws in failures.items()
            ],
            "adult": want_adult,
            "config": config,
            "generated_at": _time.time(),
        })

    @app.route("/api/home-feed/sources")
    def api_home_feed_sources():
        """Every source the feed knows, without fetching anything.
        GET /api/home-feed/sources.

        The Start Page settings need the list (built-ins plus whatever
        modules registered) to offer "off by default" switches; asking
        /api/home-feed for it would scrape five sites to build a checkbox
        list."""
        out = []
        for sid, label, color in _FEED_BUILTIN_META:
            out.append({"id": sid, "label": label, "color": color, "builtin": True,
                        "enabled": _feed_source_enabled(sid)})
        for src in iter_home_feed_sources():
            out.append({"id": src["source_id"], "label": src["label"],
                        "color": src["color"], "builtin": False,
                        "enabled": _feed_source_enabled(src["source_id"])})
        config = feed_effective_config()
        return jsonify({"sources": out, "rows": config["rows"], "config": config,
                        "defaults": feed_global_defaults()})

    @app.route("/api/home-feed/personal")
    def api_home_feed_personal():
        """Return the personal home rows. GET /api/home-feed/personal.

        Four questions the discovery rows cannot answer, all from data
        MediaForge already has: what was I watching, what did I mark, what
        arrived in my library, what airs next. Every one of them is per user
        and every one degrades to an empty list rather than an error -- a
        home page must not break because the calendar has no API key.

        Called from static/home_feed.js.
        """
        from ..request_context import get_current_user_info
        try:
            username, _is_admin = get_current_user_info()
        except Exception:
            username = None

        out = {"continue": [], "watchlist": [], "library": [], "upcoming": []}
        hidden = set(feed_effective_config()["hidden"])
        # Reading the whole library to fill a row the user switched off is
        # exactly the kind of work a home page should not be doing.
        if all(row in hidden for row in _FEED_PERSONAL_ROWS):
            return jsonify(out)

        # --- the library, once: both "continue watching" (which needs to turn
        #     a file path back into a title) and "new in your library" read it.
        lib_titles = []
        by_path = {}
        try:
            if "continue" in hidden and "library" in hidden:
                raise StopIteration
            from .library import _lib_active_path_keys
            from ..db import get_all_library_cache
            active = _lib_active_path_keys()
            for path_key, entry in (get_all_library_cache() or {}).items():
                if path_key not in active:
                    continue          # leftover of a removed scan target
                for title in entry.get("data") or []:
                    lib_titles.append(title)
                    for skey, eps in (title.get("seasons") or {}).items():
                        for ep in eps:
                            if ep.get("path"):
                                by_path[ep["path"]] = (title, skey, ep)
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] library lookup failed", exc_info=True)

        # --- Continue watching
        try:
            if "continue" in hidden:
                raise StopIteration
            from ..db import get_recent_watch_progress
            for prog in get_recent_watch_progress(username=username, limit=15):
                path = prog["file_path"]
                title, skey, ep = by_path.get(path, (None, None, None))
                if title is None:
                    continue          # file was deleted or moved since
                duration = float(prog.get("duration_seconds") or 0)
                position = float(prog.get("position_seconds") or 0)
                percent = (position / duration * 100) if duration > 0 else 0
                out["continue"].append({
                    "title": title.get("folder", ""),
                    "path": path,
                    "file": ep.get("file", ""),
                    "season": None if skey == "movies" else skey,
                    "episode": ep.get("episode"),
                    "is_movie": bool(title.get("is_movie")),
                    "position": position,
                    "duration": duration,
                    "percent": round(percent, 1),
                })
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] continue-watching lookup failed", exc_info=True)

        # --- New in your library
        try:
            if "library" in hidden:
                raise StopIteration
            recent = sorted(lib_titles, key=lambda t: t.get("added_at") or 0, reverse=True)
            for title in recent[:20]:
                if not title.get("added_at"):
                    continue
                out["library"].append({
                    "title": title.get("folder", ""),
                    "is_movie": bool(title.get("is_movie")),
                    "episodes": title.get("total_episodes") or 0,
                    "added_at": title.get("added_at"),
                })
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] library row failed", exc_info=True)

        # --- Watchlist (favourites)
        try:
            if "watchlist" in hidden:
                raise StopIteration
            from ..db import get_favourites
            for fav in (get_favourites(added_by=username) or [])[:20]:
                poster = fav.get("poster_url") or ""
                if poster and not poster.startswith("/api/img"):
                    poster = _poster_proxy(poster)
                out["watchlist"].append({
                    "title": fav.get("title", ""),
                    "url": fav.get("series_url", ""),
                    "poster_url": poster,
                    "provider": fav.get("provider") or "",
                    "media_type": fav.get("media_type") or "",
                })
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] watchlist row failed", exc_info=True)

        # --- Airing next (calendar). Only when the calendar feature is on and
        #     configured -- collect_calendar_events() is the expensive one here,
        #     and without an API key it has nothing to say anyway.
        try:
            if "upcoming" not in hidden and get_setting("cineinfo_calendar", "0") == "1":
                api_key = (get_setting("cineinfo_tmdb_api_key", "") or "").strip()
                if api_key:
                    from datetime import date, timedelta
                    from flask import session as _session
                    from .calendar_routes import collect_calendar_events
                    from ..request_context import get_current_user_info as _uinfo
                    _user, _admin = _uinfo()
                    events, _meta = collect_calendar_events(
                        api_key, get_setting("cineinfo_country", "DE"),
                        _session.get("ui_language", "en"), _user, _admin)
                    today = date.today()
                    horizon = today + timedelta(days=14)
                    upcoming = []
                    for ev in events or []:
                        raw = str(ev.get("air_date") or "")[:10]
                        try:
                            when = date.fromisoformat(raw)
                        except ValueError:
                            continue
                        if today <= when <= horizon:
                            upcoming.append((when, ev))
                    upcoming.sort(key=lambda pair: pair[0])
                    for when, ev in upcoming[:20]:
                        art = ev.get("still") or ev.get("poster") or ""
                        out["upcoming"].append({
                            "title": ev.get("title", ""),
                            "name": ev.get("name", ""),
                            "season": ev.get("season"),
                            "episode": ev.get("episode"),
                            "air_date": when.isoformat(),
                            "is_movie": bool(ev.get("is_movie")),
                            "poster_url": _poster_proxy(
                                "https://image.tmdb.org/t/p/w300" + art) if art else "",
                        })
        except Exception:
            logger.debug("[HomeFeed] upcoming row failed", exc_info=True)

        return jsonify(out)

    @app.route("/api/downloaded-folders")
    def api_downloaded_folders():
        """List folder names present under the download root(s) (and any
        custom paths), used to flag already-downloaded titles in browse
        views. GET /api/downloaded-folders.

        Called from static/app.js's `loadDownloadedFolders()`."""
        from pathlib import Path
        # If MediaScan is active and using a media-server source,
        # signal the frontend to skip the folder check entirely.
        ms_enabled = get_setting("mediascan_enabled", "0") == "1"
        ms_source  = get_setting("mediascan_source",  "") or ""
        if ms_enabled and ms_source and ms_source != "folders":
            return jsonify({"folders": [], "source": "mediascan", "mediascan_source": ms_source})


        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            dl_path = p
        else:
            dl_path = Path.home() / "Downloads"

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        lang_folders = LANG_FOLDERS

        # Collect all paths to scan (default + custom)
        scan_roots = [dl_path]
        for cp in get_custom_paths():
            cp_path = Path(cp["path"]).expanduser()
            if not cp_path.is_absolute():
                cp_path = Path.home() / cp_path
            scan_roots.append(cp_path)

        folders = set()
        for root in scan_roots:
            if lang_sep:
                bases = [root / lf for lf in lang_folders]
            else:
                bases = [root]
            for base in bases:
                if not base.is_dir():
                    continue
                for entry in base.iterdir():
                    if entry.is_dir():
                        folders.add(entry.name)
        return jsonify({"folders": sorted(folders)})
