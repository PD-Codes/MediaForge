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
from ...search import fetch_filmo_new_movies
from ...search import fetch_filmo_popular_movies
from ...search import fetch_nineanime_new
from ...search import fetch_nineanime_popular
from ...search import fetch_aniwaves_new
from ...search import fetch_aniwaves_popular
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
from ..source_policy import source_enabled as _source_enabled
from ..source_policy import is_english_only_source as _is_english_only_source
from ..source_policy import search_sources
from ..source_policy import all_source_ids
from ..queue_worker import _hanime_enabled
from ..queue_worker import _filmo_enabled
from ..queue_worker import _nineanime_enabled
from ..queue_worker import _aniwaves_enabled
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
# "Could be for you" results, per username. Six hours: the row is built
# from caches that barely move, and a home page must not recompute it on
# every reload.
_FORYOU_MEMO: "_OD" = _OD()
_FORYOU_TTL = 6 * 3600

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


class _BrowsePending:
    """Sentinel: "no data yet, but a fetch is running" -- distinct from None,
    which means "this fetch failed". Falsy on purpose so the many
    ``if not results`` checks around the codebase keep behaving, while an
    explicit ``is BROWSE_PENDING`` can tell the two apart."""

    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return "<BROWSE_PENDING>"


BROWSE_PENDING = _BrowsePending()


# One outage report per source per interval. The feed asks several rows of the
# same source in one go and the page reloads hourly, so an unreported ceiling
# would turn one site being down into a steady drip of identical events.
_OUTAGE_REPORT_INTERVAL = 3600.0
_outage_lock = threading.Lock()
_outage_last: dict = {}


def _report_source_outage(source_id):
    """Tell telemetry that a source failed to load. Best-effort and rate
    limited; never raises into the request path."""
    if not source_id:
        return
    try:
        now = _time.time()
        with _outage_lock:
            last = _outage_last.get(source_id, 0.0)
            if now - last < _OUTAGE_REPORT_INTERVAL:
                return
            _outage_last[source_id] = now
        from ...telemetry import client as _tel_client
        from ...telemetry import events as _tel_events
        _tel_client.submit(
            _tel_events.build_network_detail_event("source_unavailable", source_id))
    except Exception:
        logger.debug("[Telemetry] source outage report failed", exc_info=True)


def _browse_json(key, fetch_fn, error_message):
    """The three lines every browse route repeated, in one place.

    - data      -> 200 {"results": [...]}
    - pending   -> 202 {"results": [], "pending": true}  (client retries)
    - failure   -> 502 {"error": ...}

    The 202 is the point: a cold start that simply takes longer than the 10 s
    budget used to be reported as a 502, i.e. as a broken source, and the
    frontend rendered "Error loading" for a list that was still on its way.
    """
    results = _cached_browse(key, fetch_fn)
    if results is BROWSE_PENDING:
        return jsonify({"results": [], "pending": True}), 202
    if results is None:
        return jsonify({"error": error_message}), 502
    return jsonify({"results": _proxy_result_list(results)})


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
                except Exception:
                    # Previously a bare try/finally: a scraper blowing up in
                    # this thread left NO trace at all, so a browse list that
                    # silently went stale looked identical to one that was
                    # simply not refreshed yet.
                    logger.exception("[Browse] Background refresh failed for %s", k)
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
            if r is None:
                # The fetcher itself decided it failed (it returns None rather
                # than raising). Worth a line: this is exactly what the caller
                # turns into a 502, and without it the log stays empty.
                logger.warning("[Browse] Cold fetch for %s returned no data", k)
            else:
                _browse_cache_set(k, (_time.time(), r))
                set_browse_cache(k, r)
                out[0] = r
        except Exception:
            logger.exception("[Browse] Cold fetch failed for %s", k)
        finally:
            ev.set()
            with _browse_refresh_mutex:
                _browse_refresh_locks.pop(k, None)

    threading.Thread(target=_cold_fetch, daemon=True,
                     name=f"browse-cold-{key}").start()
    finished = _cold_done.wait(timeout=10)
    if not finished:
        # Still scraping. The thread keeps running and will fill the cache, so
        # the next request (or the client's retry) gets real data -- reporting
        # a hard failure here would be wrong AND sticky, because the user sees
        # "Error loading" for a list that is about to arrive. BROWSE_PENDING
        # lets the route answer "not yet" instead of "broken".
        logger.info("[Browse] %s still loading after 10s -- answering pending", key)
        return BROWSE_PENDING
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

        # Try to get IMDB ID from the series page for accurate matching.
        # Asked of the provider registry rather than of a hardcoded "is this
        # FilmPalast" test: every movie-only site (FilmPalast, filmo.to, ...)
        # has series_cls=None, and calling it would raise
        # "TypeError: 'NoneType' object is not callable" -- swallowed by the
        # except below, but only after the URL was resolved for nothing. One
        # check that reads the actual reason covers all of them, including any
        # source a module registers.
        imdb_id = None
        try:
            prov = resolve_provider(url)
        except Exception:
            prov = None
        if prov is not None and prov.series_cls is not None:
            try:
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
    """Start the background browse/TMDB prefetch worker thread.

    Skipped under TESTING. The worker scrapes the real sites and writes the
    results into _browse_cache -- the same cache the feed tests clear and then
    fill with stubs. A prefetch landing between the two turned those tests
    into an intermittent failure that looked like a bug in the feed and was
    really a second writer nobody had accounted for.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        logger.debug("[Prefetch] Not starting the worker under test")
        return
    _pt = threading.Thread(target=_prefetch_worker, daemon=True, name="browse-prefetch")
    _pt.start()
    logger.info("[Prefetch] Background worker started (interval=%d min)", _PREFETCH_INTERVAL // 60)


def downloaded_folder_names() -> list:
    """Every folder name under the download root and every custom path.

    Sorted and deduplicated, names only -- the same answer
    /api/downloaded-folders serves. A module-level function rather than inline
    in the route because web/library_aliases.py needs the identical list: two
    answers to "what is on disk" would let the alias table describe folders the
    badge check never sees, and vice versa.

    Non-recursive by design: a series folder is a direct child of a root (or of
    a language folder under it), and walking deeper would list season folders as
    if they were titles.
    """
    from pathlib import Path

    raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        dl_path = p
    else:
        dl_path = Path.home() / "Downloads"

    lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"

    scan_roots = [dl_path]
    for cp in get_custom_paths():
        cp_path = Path(cp["path"]).expanduser()
        if not cp_path.is_absolute():
            cp_path = Path.home() / cp_path
        scan_roots.append(cp_path)

    folders = set()
    for root in scan_roots:
        bases = [root / lf for lf in LANG_FOLDERS] if lang_sep else [root]
        for base in bases:
            try:
                if not base.is_dir():
                    continue
                for entry in base.iterdir():
                    if entry.is_dir():
                        folders.add(entry.name)
            except OSError:
                # An unreadable or vanished root is not an error worth failing
                # the whole listing over -- the other roots are still a useful
                # answer, and this runs on a background thread too.
                continue
    return sorted(folders)


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

# How many cards a row is built from when the caller asks for a reserve pool
# (`?pool=1`). The home page shows `limit` cards, but its source/type chips
# filter client-side -- without a reserve, switching a source off simply made
# the row shorter, which reads as "the page lost content" rather than "that
# source is hidden". With the pool the row keeps its configured length and the
# next-best cards move up. The multiplier is not a guess: with the chips the
# user can hide all but one source, so the pool has to survive the worst case
# while staying bounded, hence the hard ceiling below (a row can never cost
# more than _FEED_POOL_MAX cards of scraping/serialisation).
_FEED_POOL_FACTOR = 4
_FEED_POOL_MAX = 150

# id -> (label, chip colour). Only the built-ins; module sources bring their
# own label/colour through the registry.
_FEED_BUILTIN_META = (
    ("aniworld",   "AniWorld",     "#6aa9ff"),
    ("sto",        "SerienStream", "#8b7dff"),
    ("filmpalast", "FilmPalast",   "#ffb454"),
    ("megakino",   "MegaKino",     "#4ade80"),
    ("filmo",      "filmo.to",     "#e8914a"),
    ("nineanime",  "9anime",       "#f0a020"),
    ("aniwaves",   "Aniwaves",     "#38bdf8"),
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
    # Seasons with holes in them. The data is the statistics page's
    # "incomplete series" list; what makes it a home row is that every card
    # carries an action ("load what is missing") instead of a number.
    "gaps":      {"hint": "library",  "link": "/stats"},
    # "Because you watched X". The only row on this page that is a GUESS, and
    # it names its seed for exactly that reason: a suggestion nobody can trace
    # back to a reason is a suggestion nobody trusts. See web/recommend.py.
    "because":   {"hint": "playback", "link": "/library"},
}
_FEED_PERSONAL_ROWS = ("continue", "library", "watchlist", "upcoming", "gaps",
                       "because")
# The default reading order: what you were doing, what arrived, then what is
# out there -- and the two rows that are really other pages in miniature
# (watchlist, calendar) at the end.
#
# Do not move the personal rows to the back to spare a fresh install two
# empty rows: static/home_feed.js already hides an empty personal row
# (renderPersonal() calls showSection(row, list.length > 0), which sets
# display:none), so a new user never sees a blank "Continue watching" in the
# first place -- while everyone WITH a history would lose the one row they
# actually came for from the top of the page. tests/test_home_feed_smoke.py
# (test_default_row_order_puts_the_borrowed_rows_last) pins both ends of this
# order on purpose.
#
# "gaps" sits directly behind "popular" by request: it is the one row that
# asks something of the user rather than offering something, and putting it
# above the discovery rows would make the home page feel like a chore list.
#
# "because" sits right after "library": both answer "what of mine should I
# open next", and putting a guess in front of the discovery rows is the only
# place it earns its space. Ahead of "continue" it would be presumptuous --
# the thing you already started beats anything this can infer.
_FEED_DEFAULT_ORDER = ("continue", "library", "because", "new", "popular",
                       "gaps", "movies", "watchlist", "upcoming")
_FEED_CARDS_CHOICES = (10, 20, 30, 40, 60)


def _feed_max_fsk():
    """The age ceiling for this account, or None when there is none.

    Thin wrapper around web/age_gate.py, which is the single place that knows
    both ways an account can be limited (the kids ROLE and the per-account
    kids MODE) -- see that module for why this is not decided here.
    """
    from ..age_gate import ceiling
    return ceiling()


def _feed_apply_age_limit(items, max_fsk):
    """Drop cards rated above *max_fsk*. See web/age_gate.py for the rule --
    in particular why an unrated title is kept rather than dropped."""
    from ..age_gate import filter_items
    return filter_items(items, max_fsk)


def _feed_proxy_remote_posters(items):
    """Point a media-server card's artwork at our own proxy.

    web/mediaplayer.py hands back a server-RELATIVE ``poster_path`` on purpose
    (see image_bytes()); this turns it into the URL the browser may actually
    ask for. Anything without a path simply has no poster and falls back to
    the generated placeholder, same as a local card.

    Returns NEW dicts and never touches the originals. The first version
    pop()ed the path out of the items it was given -- and those items come
    straight out of mediaplayer.py's TTL cache, so the first page load got
    real artwork and every load for the next minute got the placeholder,
    because the path it needed had been taken out of the cached copy.
    """
    from urllib.parse import quote
    out = []
    for item in items or []:
        path = item.get("poster_path") or ""
        copy = {k: v for k, v in item.items() if k != "poster_path"}
        copy["poster_url"] = ("/api/mediaplayer/image?path=" + quote(path, safe="")
                              if path else "")
        out.append(copy)
    return out


def _feed_library_posters(entries):
    """Fill in ``poster_url`` for library-derived cards from the TMDB cache.

    The library scanner knows file names, not artwork, so these rows have
    always drawn the generated colour placeholder. TMDB has usually been
    asked about these titles already (the browse cards do it), so the poster
    is sitting in the local cache -- this reads it back in ONE bulk query for
    the whole row rather than a lookup per card.

    Cache only, never a network call: this runs while building the home page,
    and a row that waits for twenty TMDB requests is a row nobody sees. A
    title the cache does not know keeps the placeholder.
    """
    if not entries:
        return entries
    try:
        from flask import session
        from ..db import get_tmdb_cache_bulk
    except Exception:
        return entries

    country = get_setting("cineinfo_country", "DE") or "DE"
    try:
        ui_lang = session.get("ui_language", "de")
    except Exception:
        ui_lang = "de"

    keys = {}
    for entry in entries:
        name = str(entry.get("title") or "")
        if name and not entry.get("poster_url"):
            keys[name] = name + "|||" + country + "|||" + ui_lang
    if not keys:
        return entries
    try:
        cached = get_tmdb_cache_bulk(list(keys.values())) or {}
    except Exception:
        return entries

    for entry in entries:
        name = str(entry.get("title") or "")
        hit = cached.get(keys.get(name, "")) or {}
        path = ((hit.get("raw_details") or {}).get("poster_path")
                if isinstance(hit.get("raw_details"), dict) else None)
        if path:
            entry["poster_url"] = _poster_proxy("https://image.tmdb.org/t/p/w300" + path)
    return entries


def _feed_gap_rows(lib_titles, limit=12):
    """Turn library titles into "this season has holes" cards.

    Uses the statistics page's gap detection (_media_missing_episodes) and
    its ignore list, so a slot the user marked "never mind" on /stats does
    not reappear on the home page -- two places disagreeing about what
    "complete" means is worse than not having the row.

    Sorted by *fewest* missing episodes first: a series that is one episode
    short is one click from being finished, while a series missing two whole
    seasons is a project. The row is for the former.
    """
    from .stats import _media_missing_episodes
    try:
        from ..db import get_media_ignores
        ignores = get_media_ignores() or {}
    except Exception:
        ignores = {}

    out = []
    for title in lib_titles:
        seasons = title.get("seasons") or {}
        if not seasons:
            continue
        folder = title.get("folder", "")
        missing = _media_missing_episodes(seasons)
        # get_media_ignores() keys by the lower-cased folder name (see its
        # docstring); looking up the raw folder silently ignored every ignore.
        entry = ignores.get(folder.lower()) or ignores.get(folder)
        if entry:
            slots = entry.get("slots") if isinstance(entry, dict) else entry
            slots = set(slots or [])
            if "__all__" in slots:
                continue
            missing = [m for m in missing if m not in slots]
        if not missing:
            continue
        out.append({
            "title": folder,
            "folder": folder,
            "missing": missing[:12],
            "missing_count": len(missing),
            # Both are shown: "3 missing" is the headline, the slot list is
            # what makes it actionable.
            "poster_url": title.get("poster_url") or "",
        })
    out.sort(key=lambda item: (item["missing_count"], item["title"].lower()))
    return out[:limit]


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
    # The age ceiling in force, so the page can SAY it is limited. Reported
    # rather than accepted: this value is what the server already applied
    # while building the rows, so the toolbar can never claim a mode the feed
    # is not actually being filtered by.
    ceiling = _feed_max_fsk()
    cfg["max_fsk"] = "" if ceiling is None else str(ceiling)
    cfg["mode"] = (_feed_user_prefs().get("home_mode") or "")
    # Kids mode is off until an admin turns it on AND sets a PIN. Both, not
    # either: a kids mode you can leave by clicking "Standard" is a display
    # setting wearing a lock icon, and offering the button before the lock
    # exists is how it ends up being used that way.
    #
    # Two flags, not one: `kids_switched_on` is what the admin ticked and is
    # what the checkbox must show, while `kids_enabled` is whether the button
    # actually appears. Collapsing them would make the checkbox untick itself
    # whenever the PIN is missing, which reads as "the save failed".
    cfg["kids_switched_on"] = get_setting("home_kids_enabled", "0") == "1"
    cfg["kids_has_pin"] = bool((get_setting("home_kids_pin", "") or "").strip())
    # A kids ACCOUNT is never offered the switch: the role is the answer, and
    # there is nothing to leave. Showing it a mode toggle would be an invitation
    # to try a PIN it is not meant to have.
    from ..age_gate import is_kids_account
    cfg["kids_account"] = is_kids_account()
    cfg["kids_enabled"] = (cfg["kids_switched_on"] and cfg["kids_has_pin"]
                           and not cfg["kids_account"])
    cfg["kids_max_fsk"] = get_setting("home_kids_max_fsk", "6")
    return cfg


def _feed_known_source_ids():
    """Built-in plus module-registered source ids -- used to validate the
    stored "off by default" list."""
    # all_source_ids() already merges the search, provider and home-feed
    # registries, so this no longer has to walk them one by one -- and cannot
    # miss a registry that is added later.
    return {sid for sid, _label, _color in _FEED_BUILTIN_META} | all_source_ids()


def _feed_module_search_sources():
    """Module sources that exist but have no discovery fetchers.

    A module registers a provider plus a search source (register_provider /
    register_search_source) and is then fully usable from the search box --
    but the home feed only ever knew about register_home_feed_source, so such
    a source was missing from the "Sources" dropdown and from the Sources
    dashboard card. It could not be switched off there either, because the
    list it is switched off in is this one.

    source_policy.search_sources() is the catalogue of "which sources exist
    right now"; everything third-party in it that did not also register feed
    fetchers is returned here. No fetchers means no cards -- the core cannot
    invent a discovery list for a site it does not scrape -- so the source
    shows up as a filter/toggle, and contributes rows as soon as the module
    also calls register_home_feed_source().
    """
    have = {src["source_id"] for src in iter_home_feed_sources()}
    out = []
    for entry in search_sources():
        if not entry.get("thirdparty") or entry["id"] in have:
            continue
        out.append(entry)
    return out


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
        ("filmo",      "new",     "movies", "filmo_new_movies",        fetch_filmo_new_movies),
        ("filmo",      "popular", "movies", "filmo_popular_movies",    fetch_filmo_popular_movies),
        # 9anime/Aniwaves are opt-in sources (see source_policy.OPT_IN_SOURCE_IDS):
        # they are listed here unconditionally like every other built-in, and
        # _feed_source_enabled() -- which reads the same source_enabled_<id>
        # setting the settings page writes -- is what keeps them out of the feed
        # until the user turns them on. No extra gate here, so enabling the
        # source in Settings is all it takes for them to appear.
        ("nineanime",  "new",     "series", "nineanime_new",           fetch_nineanime_new),
        ("nineanime",  "popular", "series", "nineanime_popular",       fetch_nineanime_popular),
        ("aniwaves",   "new",     "series", "aniwaves_new",            fetch_aniwaves_new),
        ("aniwaves",   "popular", "series", "aniwaves_popular",        fetch_aniwaves_popular),
        ("hanime",     "new",     "adult",  "hanime_new" + suffix,
         lambda: fetch_hanime_new(show_censored=show_c, show_uncensored=show_u)),
        ("hanime",     "popular", "adult",  "hanime_trending" + suffix,
         lambda: fetch_hanime_trending(show_censored=show_c, show_uncensored=show_u)),
    ]


def _feed_source_enabled(source_id):
    """Same rule the settings page writes: every source is opt-out except the
    adult one, which is opt-in. Module sources default to on -- a module that
    registered a source was installed on purpose.

    The rule itself lives in source_policy so the UpTime page and this one
    cannot disagree about the same source (they did: UpTime had its own
    hardcoded copy naming "hanime")."""
    return _source_enabled(source_id)


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
    # The home page's button bar. Registered from here rather than from
    # create_app() because it belongs to the same page as the feed above and
    # ships with it -- one home page, one registration point.
    from .home_panels import register_home_panel_routes
    register_home_panel_routes(app)
    # Home page 2.1: media-server profile link, Wrapped, onboarding, suggest.
    # Registered here for the same reason the panels are -- everything the
    # home page needs has one entry point.
    from .home_extras import register_home_extras_routes
    register_home_extras_routes(app)
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
        return _browse_json("new_animes", fetch_new_animes, "Failed to fetch new animes")
    @app.route("/api/popular-animes")
    def api_popular_animes():
        """Return the cached "popular animes" browse list. GET /api/popular-animes.

        Called from static/app.js's `loadAniworldBrowse()`."""
        return _browse_json("popular_animes", fetch_popular_animes, "Failed to fetch popular animes")
    @app.route("/api/new-series")
    def api_new_series():
        """Return the cached "new series" browse list (S.TO). GET /api/new-series.

        Called from static/app.js's `loadStoBrowse()`."""
        return _browse_json("new_series", fetch_new_series, "Failed to fetch new series")
    @app.route("/api/popular-series")
    def api_popular_series():
        """Return the cached "popular series" browse list (S.TO). GET /api/popular-series.

        Called from static/app.js's `loadStoBrowse()`."""
        return _browse_json("popular_series", fetch_popular_series, "Failed to fetch popular series")
    @app.route("/api/new-movies")
    def api_new_movies():
        """Return the cached "new movies" browse list (FilmPalast). GET /api/new-movies.

        Called from static/app.js's `loadFilmPalastBrowse()`."""
        return _browse_json("new_movies", _fetch_new_movies, "Failed to fetch new movies")
    @app.route("/api/megakino/new-movies")
    def api_megakino_new_movies():
        """Return the cached Megakino "new movies" browse list. GET /api/megakino/new-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        return _browse_json("megakino_new_movies", fetch_megakino_new_movies, "Failed to fetch megakino movies")
    @app.route("/api/megakino/popular-movies")
    def api_megakino_popular_movies():
        """Return the cached Megakino "popular movies" browse list. GET /api/megakino/popular-movies.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        return _browse_json("megakino_popular_movies", fetch_megakino_popular_movies, "Failed to fetch megakino popular movies")
    @app.route("/api/megakino/new-series")
    def api_megakino_new_series():
        """Return the cached Megakino "new series" browse list. GET /api/megakino/new-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        return _browse_json("megakino_new_series", fetch_megakino_new_series, "Failed to fetch megakino series")
    @app.route("/api/megakino/popular-series")
    def api_megakino_popular_series():
        """Return the cached Megakino "popular series" browse list. GET /api/megakino/popular-series.

        Called from static/app.js's `loadMegakinoBrowse()`."""
        return _browse_json("megakino_popular_series", fetch_megakino_popular_series, "Failed to fetch megakino popular series")
    @app.route("/api/filmo/new-movies")
    def api_filmo_new_movies():
        """Return the cached filmo.to "new movies" browse list. GET /api/filmo/new-movies.

        Called from static/app.js's `loadFilmoBrowse()`."""
        return _browse_json("filmo_new_movies", fetch_filmo_new_movies, "Failed to fetch filmo new movies")
    @app.route("/api/filmo/popular-movies")
    def api_filmo_popular_movies():
        """Return the cached filmo.to "popular movies" browse list. GET /api/filmo/popular-movies.

        Called from static/app.js's `loadFilmoBrowse()`."""
        return _browse_json("filmo_popular_movies", fetch_filmo_popular_movies, "Failed to fetch filmo popular movies")
    @app.route("/api/nineanime/new")
    def api_nineanime_new():
        """Return the cached 9anime.or.at "newest" browse list. GET /api/nineanime/new.

        Returns an empty list unless the opt-in 9anime source is enabled.
        Called from static/app.js's `loadNineanimeBrowse()`."""
        if not _nineanime_enabled():
            return jsonify({"results": []})
        return _browse_json("nineanime_new", fetch_nineanime_new, "Failed to fetch 9anime new")
    @app.route("/api/nineanime/popular")
    def api_nineanime_popular():
        """Return the cached 9anime.or.at "trending" browse list. GET /api/nineanime/popular.

        Returns an empty list unless the opt-in 9anime source is enabled.
        Called from static/app.js's `loadNineanimeBrowse()`."""
        if not _nineanime_enabled():
            return jsonify({"results": []})
        return _browse_json("nineanime_popular", fetch_nineanime_popular, "Failed to fetch 9anime popular")
    @app.route("/api/aniwaves/new")
    def api_aniwaves_new():
        """Return the cached aniwaves.ru "newest" browse list. GET /api/aniwaves/new.

        Returns an empty list unless the opt-in Aniwaves source is enabled.
        Called from static/app.js's `loadAniwavesBrowse()`."""
        if not _aniwaves_enabled():
            return jsonify({"results": []})
        return _browse_json("aniwaves_new", fetch_aniwaves_new, "Failed to fetch aniwaves new")
    @app.route("/api/aniwaves/popular")
    def api_aniwaves_popular():
        """Return the cached aniwaves.ru "trending" browse list. GET /api/aniwaves/popular.

        Returns an empty list unless the opt-in Aniwaves source is enabled.
        Called from static/app.js's `loadAniwavesBrowse()`."""
        if not _aniwaves_enabled():
            return jsonify({"results": []})
        return _browse_json("aniwaves_popular", fetch_aniwaves_popular, "Failed to fetch aniwaves popular")
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
        return _browse_json(
            "hanime_new" + suffix,
            lambda: fetch_hanime_new(show_censored=show_censored, show_uncensored=show_uncensored),
            "Failed to fetch hanime new",
        )
    @app.route("/api/hanime/trending")
    def api_hanime_trending():
        """Return the cached "trending hanime" browse list, filtered by the
        censored/uncensored display prefs. GET /api/hanime/trending.

        Returns an empty list unless the adult hanime source is explicitly
        enabled. Called from static/app.js's `loadHanimeBrowse()`."""
        if not _hanime_enabled():
            return jsonify({"results": []})
        show_censored, show_uncensored, suffix = _hanime_censorship_prefs()
        return _browse_json(
            "hanime_trending" + suffix,
            lambda: fetch_hanime_trending(show_censored=show_censored, show_uncensored=show_uncensored),
            "Failed to fetch hanime trending",
        )
    @app.route("/api/home-feed")
    def api_home_feed():
        """Return the complete new-home-page feed in one answer.
        GET /api/home-feed?adult=0|1&limit=30.

        Kept for the Start Page settings preview, the module examples and the
        smoke tests. The home page itself asks per row through
        /api/home-feed/row/<row> so one slow source cannot hold up the page.
        """
        return jsonify(_feed_build())

    def _feed_build(only_row=None):
        """Build the feed payload. Returns a plain dict, not a response.

        *only_row* restricts the work to a single discovery row: sources that
        contribute nothing to it are never fetched, which is the entire point
        of the per-row endpoint.

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
        # The age ceiling is applied HERE, not in the browser. A kids mode a
        # client can undo by editing a query string (or by calling this
        # endpoint itself) is decoration; the chip row is a filter, this is a
        # restriction, and the two must not share an implementation.
        max_fsk = _feed_max_fsk()
        if max_fsk is not None and max_fsk < 18:
            want_adult = False
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
        # `pool=1` asks for a reserve on top of the visible count so the client
        # can hide a source without the row shrinking (see _FEED_POOL_FACTOR).
        # Opt-in rather than always-on: /api/home-feed also feeds the Start
        # Page settings preview, and a preview that shows four times the cards
        # the real page shows is a preview of the wrong thing.
        collect = limit
        if request.args.get("pool") == "1":
            collect = max(1, min(limit * _FEED_POOL_FACTOR, _FEED_POOL_MAX))
        hidden = set(config["hidden"])

        # 1. Everything that could contribute: built-ins + module sources.
        entries = list(_feed_builtin_entries())
        meta = {}
        for sid, label, color in _FEED_BUILTIN_META:
            meta[sid] = {"id": sid, "label": label, "color": color,
                         "types": set(), "builtin": True,
                         # Display-only marker; see source_policy.
                         "english_only": _is_english_only_source(sid)}
        for src in iter_home_feed_sources():
            sid = src["source_id"]
            meta.setdefault(sid, {"id": sid, "label": src["label"],
                                  "color": src["color"], "types": set(),
                                  "builtin": False,
                                  # A module knows its own catalogue; the core
                                  # cannot guess it.
                                  "english_only": False})
            for row, fn in src["fetchers"].items():
                entries.append((sid, row, src["media_type"],
                                "tp_%s_%s" % (sid, row), fn))
        # Module sources without feed fetchers: visible as a filter chip and a
        # Sources-card row, just with nothing to contribute yet. Their types
        # come from what the module declared to register_search_source(), so
        # the type filter does not drop them.
        extra_enabled = {}
        for entry in _feed_module_search_sources():
            sid = entry["id"]
            meta.setdefault(sid, {"id": sid, "label": entry["label"],
                                  "color": "", "builtin": False,
                                  "types": set(entry.get("media_types") or ()),
                                  "english_only": False})
            # A module source may own its enabled key -- ask the catalogue,
            # not the source_enabled_<id> convention.
            extra_enabled[sid] = bool(entry.get("enabled"))

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

        enabled = {sid: extra_enabled.get(sid, _feed_source_enabled(sid))
                   for sid in meta}

        def _wanted(sid, mtype, row):
            if not enabled.get(sid):
                return False
            if mtype == "adult" and not want_adult:
                return False
            if only_row == "movies":
                # The movie row is derived from media_type, not from a
                # fetcher name, so it draws on every row -- but only on the
                # sources that actually publish movies.
                return mtype == "movies"
            if only_row and row != only_row:
                return False
            return True

        # 3. Fetch in parallel. _cached_browse() needs no request context (it
        #    only touches the browse cache and the scrapers), so a worker pool
        #    is safe here and turns a cold start from "eleven timeouts in a
        #    row" into one.
        todo = [e for e in entries if _wanted(e[0], e[2], e[1])]
        fetched = {}
        failures = {}
        pending = {}
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
                        _report_source_outage(sid)
                        continue
                    if results is BROWSE_PENDING:
                        # Still scraping (cold cache). Not a failure -- saying
                        # so would put "this source is down" in the feed for a
                        # row that is simply not ready yet. Reported separately
                        # so the client knows to ask again in a moment instead
                        # of leaving the source's cards out until the next
                        # full reload an hour later.
                        pending.setdefault(sid, []).append(row)
                        continue
                    fetched.setdefault((row, sid), []).extend(
                        dict(r, source=sid, media_type=mtype) for r in results
                    )

        # 4. Proxy posters + inline cached TMDB, once per source list (both
        #    read settings, so this stays in the request thread).
        for key, items in fetched.items():
            fetched[key] = _feed_apply_age_limit(_proxy_result_list(items), max_fsk)

        # 5. Rows. `taken` spans all three, so nothing is shown twice.
        labels = {sid: meta[sid]["label"] for sid in meta}
        taken, index = set(), {}
        rows = {}
        # With per-row requests, `taken` can no longer span the three rows --
        # each request only knows its own. static/home_feed.js dedupes across
        # rows on the client instead (dedupeRows()), which it can do because
        # it is the side that eventually holds all of them.
        for row in ([only_row] if only_row else _FEED_ROW_ORDER):
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
            rows[row] = _feed_collect(bucket, order, collect, taken, index, labels)

        return {
            "rows": rows,
            # What the client should SHOW, independent of how many cards it was
            # given. Named separately from config.limit so a per-request
            # ?limit= (the settings preview) still wins on the client.
            "limit": limit,
            "sources": [
                {
                    "id": sid,
                    "label": meta[sid]["label"],
                    "color": meta[sid]["color"],
                    "english_only": bool(meta[sid].get("english_only")),
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
            # Sources whose first scrape is still running. Deliberately NOT in
            # "errors": the two need different UI (retry quietly vs. tell the
            # user the site is down) and merging them is what made a cold
            # start look like an outage.
            "pending": [
                {"source": sid, "label": labels.get(sid, sid), "rows": sorted(set(rws))}
                for sid, rws in pending.items()
            ],
            "adult": want_adult,
            "config": config,
            "row": only_row or "",
            "generated_at": _time.time(),
        }

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
                        "english_only": _is_english_only_source(sid),
                        "enabled": _feed_source_enabled(sid)})
        for src in iter_home_feed_sources():
            out.append({"id": src["source_id"], "label": src["label"],
                        "color": src["color"], "builtin": False,
                        "english_only": False,
                        "enabled": _feed_source_enabled(src["source_id"])})
        for entry in _feed_module_search_sources():
            out.append({"id": entry["id"], "label": entry["label"],
                        "color": "", "builtin": False,
                        "english_only": False,
                        "enabled": bool(entry.get("enabled"))})
        config = feed_effective_config()
        return jsonify({"sources": out, "rows": config["rows"], "config": config,
                        "defaults": feed_global_defaults()})

    @app.route("/api/home-feed/foryou")
    def api_home_feed_foryou():
        """Titles you do NOT have yet, derived from the ones you do.
        GET /api/home-feed/foryou[?refresh=1].

        Feeds the "Could be for you" row and its hero. The work is a tally
        over cached TMDB payloads (see recommend.for_you), which is cheap but
        not free, so the result is memoised for six hours per user -- long
        enough that a home page reload never recomputes it, short enough that
        a title imported this morning shows up this afternoon. ?refresh=1 is
        the "roll again" button and skips the memo.
        """
        from ..request_context import get_current_user_info
        from .. import recommend
        try:
            username, _is_admin = get_current_user_info()
        except Exception:
            username = None
        username = username or ""

        now = _time.time()
        refresh = request.args.get("refresh") in ("1", "true", "yes")
        if not refresh:
            entry = _FORYOU_MEMO.get(username)
            if entry and now - entry[0] < _FORYOU_TTL:
                return jsonify(entry[1])

        try:
            payload = recommend.for_you(username, shuffle=refresh)
        except Exception:
            logger.exception("[HomeFeed] for-you row failed")
            payload = {"configured": False, "items": [], "hero": [],
                       "generated_at": now}
        _FORYOU_MEMO[username] = (now, payload)
        # ponytail: one entry per account, dropped oldest-first. Ceiling is the
        # user count, which for this app is a household.
        while len(_FORYOU_MEMO) > 20:
            _FORYOU_MEMO.popitem(last=False)
        return jsonify(payload)

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

        out = {"continue": [], "watchlist": [], "library": [], "upcoming": [],
               "gaps": [], "because": [], "because_seed": "",
               "continue_source": "local"}
        hidden = set(feed_effective_config()["hidden"])
        # Reading the whole library to fill a row the user switched off is
        # exactly the kind of work a home page should not be doing.
        if all(row in hidden for row in _FEED_PERSONAL_ROWS):
            return jsonify(out)

        # --- Continue watching, from the media server instead of from here.
        #     When the account is linked to a Jellyfin/Plex user, that server
        #     is the truth about what this person was watching -- including
        #     everything they started in the Jellyfin app and MediaForge never
        #     saw. The local row is then not merged in but REPLACED: two rows
        #     of half-truths ordered differently is worse than one.
        remote_continue = None
        if "continue" not in hidden:
            try:
                from .. import mediaplayer
                linked = (_feed_user_prefs().get("mediaplayer_user") or "").strip()
                if linked and mediaplayer.is_configured():
                    remote_continue = mediaplayer.continue_watching(linked, limit=15)
                    if remote_continue is not None:
                        out["continue"] = _feed_proxy_remote_posters(remote_continue)
                        out["continue_source"] = mediaplayer.config().get("kind", "")
            except Exception:
                logger.debug("[HomeFeed] media-server continue-watching failed",
                             exc_info=True)
                remote_continue = None

        # --- the library, once: both "continue watching" (which needs to turn
        #     a file path back into a title) and "new in your library" read it.
        lib_titles = []
        by_path = {}
        try:
            if ("continue" in hidden or remote_continue is not None) \
                    and "library" in hidden and "gaps" in hidden:
                raise StopIteration
            from .library import lib_path_keys_for_kind, lib_iter_cached_titles
            from ..db import get_all_library_cache
            from ..media_kinds import KIND_VIDEO
            # Video only: "continue watching" and "new in your library"
            # are about episodes, and a path assigned to eBooks holds none.
            active = lib_path_keys_for_kind(KIND_VIDEO)
            for path_key, entry in (get_all_library_cache() or {}).items():
                if path_key not in active:
                    continue          # leftover of a removed scan target
                # A cache entry is a DICT ({"titles": [...], "lang_folders":
                # [...], "books": [...]}), not a list of titles. Iterating it
                # directly yielded the KEY STRINGS, so title.get() below raised
                # AttributeError into the except and BOTH personal rows were
                # silently empty on every install with a library. Use the same
                # reader the library page uses -- including lang_folders, which
                # this never handled at all.
                for title in lib_iter_cached_titles(entry.get("data")):
                    lib_titles.append(title)
                    for skey, eps in (title.get("seasons") or {}).items():
                        for ep in eps:
                            if ep.get("path"):
                                by_path[ep["path"]] = (title, skey, ep)
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] library lookup failed", exc_info=True)

        # --- Continue watching (local playback positions)
        try:
            if "continue" in hidden or remote_continue is not None:
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

        # --- Gaps: seasons with holes in them. Same source of truth as the
        #     statistics page (_media_missing_episodes), deliberately reusing
        #     lib_titles above instead of walking the library a second time.
        try:
            if "gaps" in hidden:
                raise StopIteration
            out["gaps"] = _feed_gap_rows(lib_titles)
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] gaps row failed", exc_info=True)

        # --- "Because you watched X". Last, because it is the only row here
        #     that guesses, and because it reads the same caches the rows
        #     above already warmed.
        try:
            if "because" in hidden:
                raise StopIteration
            from .. import recommend
            because = recommend.because_you_watched(username or "")
            if because:
                out["because"] = because["items"]
                out["because_seed"] = because["seed"]
        except StopIteration:
            pass
        except Exception:
            logger.debug("[HomeFeed] because-you-watched row failed", exc_info=True)

        # --- Artwork for the rows that come from the library. Done once here
        #     rather than per row, so every card that CAN have a poster gets
        #     one and the placeholder is genuinely "TMDB does not know this".
        try:
            _feed_library_posters(out["library"])
            _feed_library_posters(out["gaps"])
            _feed_library_posters(out["because"])
            if out["continue_source"] == "local":
                _feed_library_posters(out["continue"])
        except Exception:
            logger.debug("[HomeFeed] poster lookup failed", exc_info=True)

        return jsonify(out)

    @app.route("/api/home-feed/row/<row>")
    def api_home_feed_row(row):
        """One discovery row on its own. GET /api/home-feed/row/new?adult=0.

        /api/home-feed still exists and still answers everything at once (the
        settings preview and the tests use it), but the home page asks per
        row now: one slow source used to hold up the entire page, and rows
        below the fold were fetched whether or not anybody scrolled to them.

        Same payload shape as one entry of /api/home-feed's `rows`, plus the
        `sources` list -- the first row that answers is what renders the chip
        row, so it cannot be left out.
        """
        row = str(row or "").strip().lower()
        if row not in ("new", "popular", "movies"):
            return jsonify({"error": "unknown row"}), 404
        return jsonify(_feed_build(only_row=row))

    @app.route("/api/downloaded-folders")
    def api_downloaded_folders():
        """List folder names present under the download root(s) (and any
        custom paths), used to flag already-downloaded titles in browse
        views. GET /api/downloaded-folders.

        Also returns `aliases`: {loose_title_key: folder} for every alternative
        name a folder is known by (web/library_aliases.py). Folder names only
        ever answer to the spelling of whichever provider downloaded them, so
        without this a title reached from a source that calls it something else
        reports as missing -- see library_aliases' module docstring. Read from
        the table, never resolved here: resolving is a TMDB lookup per folder
        and has no business happening inside a page load.

        Called from static/app.js's `loadDownloadedFolders()`."""
        # If MediaScan is active and using a media-server source,
        # signal the frontend to skip the folder check entirely.
        ms_enabled = get_setting("mediascan_enabled", "0") == "1"
        ms_source  = get_setting("mediascan_source",  "") or ""
        if ms_enabled and ms_source and ms_source != "folders":
            return jsonify({"folders": [], "aliases": {}, "source": "mediascan",
                            "mediascan_source": ms_source})

        folders = downloaded_folder_names()
        aliases = {}
        try:
            from ..library_aliases import alias_index
            aliases = alias_index()
        except Exception:
            logger.debug("[Browse] Alias index unavailable", exc_info=True)
        return jsonify({"folders": folders, "aliases": aliases})
