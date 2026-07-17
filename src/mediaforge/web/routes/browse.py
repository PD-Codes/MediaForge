"""Browse lists + prefetch worker.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...providers import resolve_provider
from ...search import fetch_burningseries_new_series
from ...search import fetch_burningseries_popular_series
from ...search import fetch_cineby_new_movies
from ...search import fetch_cineby_popular_movies
from ...search import fetch_cineby_trending_series
from ...search import fetch_hanime_new
from ...search import fetch_hanime_trending
from ...search import fetch_kinox_new_movies
from ...search import fetch_kinox_popular_movies
from ...search import fetch_megakino_new_movies
from ...search import fetch_megakino_new_series
from ...search import fetch_megakino_popular_movies
from ...search import fetch_megakino_popular_series
from ...search import fetch_mangafire_new
from ...search import fetch_mangafire_trending
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
from ..queue_worker import _mangafire_enabled
from ..queue_worker import _is_filmpalast_url
from flask import jsonify
from flask import request
import os
import threading
import time
from ..tmdb_cache import _tmdb_lookup_cached
from .image_proxy import _img_pool
from .image_proxy import _precache_image_bg
from .image_proxy import _proxy_result_list
from ...logger import get_logger


logger = get_logger(__name__)


import time as _time
from collections import OrderedDict as _OD

_BROWSE_CACHE_MAX = 50     # hard cap; evicts LRU entry when exceeded
_browse_cache: "_OD" = _OD()
_BROWSE_TTL = 3600  # 1 hour
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
        ("burningseries_new", fetch_burningseries_new_series),
        ("burningseries_popular", fetch_burningseries_popular_series),
        ("kinox_new", fetch_kinox_new_movies),
        ("kinox_popular", fetch_kinox_popular_movies),
        ("cineby_new", fetch_cineby_new_movies),
        ("cineby_popular", fetch_cineby_popular_movies),
        ("cineby_trending", fetch_cineby_trending_series),
        ("mangafire_new", fetch_mangafire_new),
        ("mangafire_trending", fetch_mangafire_trending),
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


def register_browse_routes(app):
    """Register all browse/discovery routes (anime, series, movie listings,
    hanime, and the local downloaded-folders lookup) on the Flask app."""
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
        return jsonify({"error": "Failed to fetch random anime"}), 500
    @app.route("/api/new-animes")
    def api_new_animes():
        """Return the cached "new animes" browse list. GET /api/new-animes.

        Called from static/app.js's `loadAniworldBrowse()`."""
        results = _cached_browse("new_animes", fetch_new_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch new animes"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/popular-animes")
    def api_popular_animes():
        """Return the cached "popular animes" browse list. GET /api/popular-animes.

        Called from static/app.js's `loadAniworldBrowse()`."""
        results = _cached_browse("popular_animes", fetch_popular_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch popular animes"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/new-series")
    def api_new_series():
        """Return the cached "new series" browse list (S.TO). GET /api/new-series.

        Called from static/app.js's `loadStoBrowse()`."""
        results = _cached_browse("new_series", fetch_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch new series"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/popular-series")
    def api_popular_series():
        """Return the cached "popular series" browse list (S.TO). GET /api/popular-series.

        Called from static/app.js's `loadStoBrowse()`."""
        results = _cached_browse("popular_series", fetch_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch popular series"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/new-movies")
    def api_new_movies():
        """Return the cached "new movies" browse list (FilmPalast). GET /api/new-movies.

        Called from static/app.js's `loadFilmPalastBrowse()`."""
        results = _cached_browse("new_movies", _fetch_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch new movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/new-movies")
    def api_megakino_new_movies():
        """Return the cached Megakino "new movies" browse list. GET /api/megakino/new-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_new_movies", fetch_megakino_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/popular-movies")
    def api_megakino_popular_movies():
        """Return the cached Megakino "popular movies" browse list. GET /api/megakino/popular-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_popular_movies", fetch_megakino_popular_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino popular movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/new-series")
    def api_megakino_new_series():
        """Return the cached Megakino "new series" browse list. GET /api/megakino/new-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_new_series", fetch_megakino_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino series"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/megakino/popular-series")
    def api_megakino_popular_series():
        """Return the cached Megakino "popular series" browse list. GET /api/megakino/popular-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        results = _cached_browse("megakino_popular_series", fetch_megakino_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch megakino popular series"}), 500
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
            return jsonify({"error": "Failed to fetch hanime new"}), 500
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
            return jsonify({"error": "Failed to fetch hanime trending"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/burningseries/new")
    @app.route("/api/burningseries/new-series")
    def api_burningseries_new():
        results = _cached_browse("burningseries_new", fetch_burningseries_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch burningseries new"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/burningseries/popular")
    @app.route("/api/burningseries/popular-series")
    def api_burningseries_popular():
        results = _cached_browse("burningseries_popular", fetch_burningseries_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch burningseries popular"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/kinox/new")
    @app.route("/api/kinox/new-movies")
    def api_kinox_new():
        results = _cached_browse("kinox_new", fetch_kinox_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch kinox new"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/kinox/popular")
    @app.route("/api/kinox/popular-movies")
    def api_kinox_popular():
        results = _cached_browse("kinox_popular", fetch_kinox_popular_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch kinox popular"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/cineby/new-movies")
    def api_cineby_new_movies():
        results = _cached_browse("cineby_new", fetch_cineby_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch cineby new movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/cineby/popular-movies")
    def api_cineby_popular_movies():
        results = _cached_browse("cineby_popular", fetch_cineby_popular_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch cineby popular movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/cineby/trending-series")
    def api_cineby_trending_series():
        results = _cached_browse("cineby_trending", fetch_cineby_trending_series)
        if results is None:
            return jsonify({"error": "Failed to fetch cineby trending series"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/mangafire/new")
    def api_mangafire_new():
        if not _mangafire_enabled():
            return jsonify({"results": []})
        results = _cached_browse("mangafire_new", fetch_mangafire_new)
        if results is None:
            return jsonify({"error": "Failed to fetch mangafire new"}), 500
        return jsonify({"results": _proxy_result_list(results)})
    @app.route("/api/mangafire/trending")
    def api_mangafire_trending():
        if not _mangafire_enabled():
            return jsonify({"results": []})
        results = _cached_browse("mangafire_trending", fetch_mangafire_trending)
        if results is None:
            return jsonify({"error": "Failed to fetch mangafire trending"}), 500
        return jsonify({"results": _proxy_result_list(results)})
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
