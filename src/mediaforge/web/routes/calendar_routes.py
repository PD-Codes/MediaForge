"""Calendar page/API + background calendar watcher.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.calendar (usage counter, opened/used) --
# see telemetry/registry.py. Registry-only for now.
"""

from ..db import delete_calendar_episodes_except
from ..db import get_autosync_jobs
from ..db import get_cached_calendar_media
from ..db import get_calendar_episodes_from_db
from ..db import get_mediascan_series
from ..db import get_setting
from ..db import save_calendar_episode
from ..db import save_calendar_media
from datetime import datetime
from datetime import timedelta
from flask import jsonify
from flask import render_template
from flask import session
import json
import threading
import time
from ..request_context import get_current_user_info as _get_current_user_info
from ..tmdb_cache import _tmdb_calendar_episodes
from ..tmdb_cache import _tmdb_lookup_cached
from ..tmdb_cache import _tmdb_movie_release
from ...logger import get_logger


logger = get_logger(__name__)


# Background watcher state (moved verbatim from app.py; this module is
# now the sole owner so the `global` statements below bind here).
_calendar_watcher_active = False
_calendar_watcher_scanning = False
_calendar_watcher_last_sync = 0.0
_calendar_watcher_started = False
_cr_calendar_ids: list = []
_cr_calendar_meta: dict = {}
_cr_calendar_titles: dict = {}
_cr_targets_built_at: float = 0.0
_CR_TARGETS_TTL = 900
_CR_CAL_PAST_DAYS = 60
_CAL_A_BATCH = 25


def _seerr_requested_media():
    """Return [{tmdb_id, media_type}] of pending/approved Seerr requests that
    are not yet available. Empty list if Seerr is not configured/reachable."""
    import urllib.request as _ur
    import urllib.parse as _up
    seerr_url = (get_setting("seerr_url") or "").rstrip("/")
    seerr_key = get_setting("seerr_api_key") or ""
    if not seerr_url or not seerr_key:
        return []

    def seerr_get(path, params=None):
        url = seerr_url + path
        if params:
            url += "?" + _up.urlencode(params)
        req = _ur.Request(url, headers={"X-Api-Key": seerr_key})
        with _ur.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    out = []
    seen = set()
    try:
        for mt in ("tv", "movie"):
            for f in ("pending", "approved"):
                data = seerr_get("/api/v1/request", {
                    "filter": f, "mediaType": mt, "take": 100, "skip": 0,
                    "sort": "added", "sortDirection": "desc",
                })
                for r in data.get("results", []):
                    media = r.get("media") or {}
                    if r.get("status") not in (1, 2):
                        continue
                    if media.get("status") == 5:  # already fully available
                        continue
                    tid = media.get("tmdbId")
                    if not tid or (tid, mt) in seen:
                        continue
                    seen.add((tid, mt))
                    out.append({"tmdb_id": tid, "media_type": mt})
    except Exception as exc:
        logger.debug("[Calendar] Seerr request fetch failed: %s", exc)
    return out


def _resolve_cr_titles(api_key, country, ui_lang):
    """Resolve Crunchyroll simulcast/watchlist/list titles to TMDB tv ids.

    Returns ``(ids, meta)`` where ``meta[tid] = {title, in_wl, in_list,
    lists}``. Pure title->id resolution (TMDB-cached); no episode/DB work.
    Runs in the background watcher so the request path stays instant.
    """
    from .. import crunchyroll_service as _crs

    def _norm(x):
        return "".join(c for c in (x or "").lower() if c.isalnum())

    want_sim = get_setting("crunchyroll_calendar_simulcast", "0") == "1"
    want_wl = get_setting("crunchyroll_calendar_watchlist", "0") == "1"
    want_lists = get_setting("crunchyroll_calendar_lists", "0") == "1"
    if not (want_sim or want_wl or want_lists):
        return [], {}, True, {}

    wl_titles = _crs.get_watchlist_titles() if want_wl else []
    wl_norm = {_norm(t) for t in wl_titles}
    list_entries = _crs.get_custom_list_entries() if want_lists else []
    list_titles = [e["title"] for e in list_entries]
    list_names_by_norm = {}
    for _e in list_entries:
        list_names_by_norm.setdefault(_norm(_e["title"]), set()).add(
            _e.get("list_name") or "Crunchylist")
    list_norm = set(list_names_by_norm.keys())

    sim_titles = list(_crs.get_simulcast_titles()) if want_sim else []
    # A category whose toggle is on but which returned nothing is a
    # transient failure (e.g. a re-login hiccup). Signal "incomplete" so
    # the caller keeps the previous good set instead of a partial one.
    complete = not ((want_sim and not sim_titles) or
                    (want_wl and not wl_titles) or
                    (want_lists and not list_entries))
    titles = sim_titles + list(wl_titles) + list(list_titles)
    if not titles:
        return [], {}, False, {}

    # Prefer a TMDB id already in the calendar cache for this title (synced by
    # ANY source, e.g. Seerr's authoritative id) over the CR title search,
    # which sometimes picks a wrong/duplicate TMDB entry. Per title we take the
    # cached id with the most recent episode (the active entry).
    db_id_by_title, db_best = {}, {}
    try:
        from ..db import get_calendar_media_titles
        for _tid, _ttl, _ttl_en, _max_air in get_calendar_media_titles():
            for _nm in (_norm(_ttl), _norm(_ttl_en)):
                if not _nm:
                    continue
                if _nm not in db_best or (_max_air or "") > db_best[_nm]:
                    db_id_by_title[_nm] = int(_tid)
                    db_best[_nm] = _max_air or ""
    except Exception as _exc:
        logger.debug("[Calendar] CR id reconcile map failed: %s", _exc)
        db_id_by_title = {}

    ids, meta = [], {}
    for t in titles:
        nt = _norm(t)
        disp = t
        tid = db_id_by_title.get(nt)
        if tid is None:
            try:
                info = _tmdb_lookup_cached(t, None, api_key, country, ui_lang)
            except Exception:
                continue
            if not (info and info.get("found") and info.get("media_type") == "tv"):
                continue
            _t = info.get("tmdb_id")
            if not _t:
                continue
            tid = int(_t)
            disp = info.get("title") or t
        in_wl = nt in wl_norm
        in_list = nt in list_norm
        names = list_names_by_norm.get(nt, set())
        if tid not in meta:
            ids.append(tid)
            meta[tid] = {"title": disp,
                         "in_wl": in_wl, "in_list": in_list,
                         "lists": set(names)}
        else:
            if in_wl:
                meta[tid]["in_wl"] = True
            if in_list:
                meta[tid]["in_list"] = True
            meta[tid]["lists"].update(names)
    # Title-keyed membership (for matching events whose source gave a
    # different TMDB id than the CR title resolves to, e.g. Seerr).
    tmeta = {}
    for _t in wl_titles:
        tmeta.setdefault(_norm(_t), {"in_wl": False, "in_list": False,
                                     "lists": set()})["in_wl"] = True
    for _k, _names in list_names_by_norm.items():
        _d = tmeta.setdefault(_k, {"in_wl": False, "in_list": False, "lists": set()})
        _d["in_list"] = True
        _d["lists"].update(_names)
    for _t in sim_titles:
        tmeta.setdefault(_norm(_t), {"in_wl": False, "in_list": False, "lists": set()})
    return ids, meta, complete, tmeta


def _cr_calendar_targets(api_key, country, ui_lang, now):
    """Throttled CR target resolver. Caches ids+meta in module globals so the
    request path can read them without any TMDB/Crunchyroll calls. Returns the
    list of CR tmdb ids to feed into the watcher's sync pool."""
    global _cr_calendar_ids, _cr_calendar_meta, _cr_targets_built_at, _cr_calendar_titles
    from .. import crunchyroll_service as _crs
    want_any = any(get_setting(k, "0") == "1" for k in (
        "crunchyroll_calendar_simulcast",
        "crunchyroll_calendar_watchlist",
        "crunchyroll_calendar_lists"))
    if not (want_any and (_crs.is_enabled() or _crs.has_account())):
        _cr_calendar_ids, _cr_calendar_meta, _cr_targets_built_at = [], {}, now
        return []
    if _cr_calendar_ids and (now - _cr_targets_built_at) < _CR_TARGETS_TTL:
        return list(_cr_calendar_ids)
    try:
        ids, meta, complete, tmeta = _resolve_cr_titles(api_key, country, ui_lang)
    except Exception as exc:
        logger.debug("[Calendar Watcher] CR resolve failed: %s", exc)
        return list(_cr_calendar_ids)
    # Replace the cached set only on a non-empty result, and never let a
    # partial (incomplete) resolve overwrite a previously good full set.
    if ids and (complete or not _cr_calendar_ids):
        _cr_calendar_ids, _cr_calendar_meta = ids, meta
    if tmeta and (complete or not _cr_calendar_titles):
        _cr_calendar_titles = tmeta
    # On an incomplete resolve, retry soon instead of waiting the full TTL.
    _cr_targets_built_at = now if complete else (now - _CR_TARGETS_TTL + 90)
    return list(_cr_calendar_ids)


def _sync_calendar_item(tmdb_id, media_type, api_key):
    """Fetch one TV show's or movie's schedule from TMDB and persist it to
    the calendar cache tables (media row + per-episode/release rows). Called
    by the watcher loop and by the on-demand sync paths in api_calendar()."""
    # The cache is language-agnostic: we fetch both German and English from
    # TMDB and store both, so the calendar can be displayed in either UI
    # language without re-fetching. (TMDB lookups are cached per language.)
    try:
        if media_type == "tv":
            cal = _tmdb_calendar_episodes(tmdb_id, api_key, "de")
            if not cal or not cal.get("title"):
                # Save a dummy media to db so we don't query it infinitely
                save_calendar_media(tmdb_id, f"TMDB TV #{tmdb_id}", f"TMDB TV #{tmdb_id}", "")
                return
            cal_en = {}
            try:
                cal_en = _tmdb_calendar_episodes(tmdb_id, api_key, "en") or {}
            except Exception:
                pass
            title_en = cal_en.get("title") or cal["title"]
            en_names = {
                (e.get("season"), e.get("episode")): (e.get("name") or "")
                for e in cal_en.get("episodes", [])
            }

            media_id = save_calendar_media(tmdb_id, cal["title"], title_en, cal.get("poster") or "")
            keep_episodes = []
            for ep in cal.get("episodes", []):
                season = ep.get("season")
                episode = ep.get("episode")
                name = ep.get("name") or ""
                name_en = en_names.get((season, episode)) or name
                air_date = ep.get("air_date")
                still_path = ep.get("still") or ""
                if season is not None and episode is not None and air_date:
                    save_calendar_episode(media_id, season, episode, name, name_en, air_date, still_path)
                    keep_episodes.append((season, episode))

            # Delete any other episodes no longer in the TMDB schedule
            delete_calendar_episodes_except(media_id, keep_episodes)
            logger.debug("[Calendar Watcher] Synced TV show tmdb_id=%d: %s (%d episodes)", tmdb_id, cal["title"], len(keep_episodes))

        elif media_type == "movie":
            mov = _tmdb_movie_release(tmdb_id, api_key, "de")
            if not mov or not mov.get("title") or not mov.get("release_date"):
                save_calendar_media(tmdb_id, f"TMDB Movie #{tmdb_id}", f"TMDB Movie #{tmdb_id}", "")
                return
            title_en = mov["title"]
            try:
                mov_en = _tmdb_movie_release(tmdb_id, api_key, "en") or {}
                title_en = mov_en.get("title") or mov["title"]
            except Exception:
                pass

            media_id = save_calendar_media(tmdb_id, mov["title"], title_en, mov.get("poster") or "")
            save_calendar_episode(media_id, None, None, "", "", mov["release_date"], "")
            delete_calendar_episodes_except(media_id, [(None, None)])
            logger.debug("[Calendar Watcher] Synced Movie tmdb_id=%d: %s (release: %s)", tmdb_id, mov["title"], mov["release_date"])
    except Exception as exc:
        logger.error("[Calendar Watcher] Failed to sync tmdb_id=%d type=%s: %s", tmdb_id, media_type, exc, exc_info=True)


def _calendar_watcher_loop():
    """Background thread loop (started once via ensure_calendar_watcher_started):
    every 0.5-10s, figure out which TMDB tv/movie ids the calendar needs
    (AutoSync jobs, optional Seerr requests, optional Media Library series,
    optional Crunchyroll simulcast/watchlist/lists), sync any that are
    missing from the cache in priority order, and refresh stale cached
    entries once the missing-item backlog has been empty for 15 minutes."""
    import time as _t
    global _calendar_watcher_active
    last_list_a_empty_time = None
    _busy = False  # fast cadence while actively populating List A
    while True:
        _t.sleep(0.5 if _busy else 10)
        try:
            # 1. Check if calendar and TMDB API key are configured/enabled.
            #    Reflect the real enabled state in the status flag.
            if get_setting("cineinfo_calendar", "0") != "1":
                _calendar_watcher_active = False
                continue
            api_key = (get_setting("cineinfo_tmdb_api_key") or "").strip()
            if not api_key:
                _calendar_watcher_active = False
                continue
            _calendar_watcher_active = True
            country = get_setting("cineinfo_country", "DE")
            # Language used only to resolve title -> TMDB id (the id is the same
            # regardless); the cached display data itself is stored bilingually.
            ui_lang = "de"

            # 2. Gather active targets
            # Priority 2.1: AutoSync jobs
            autosync_jobs = get_autosync_jobs()
            priority_titles = []
            for job in autosync_jobs:
                if job.get("enabled") == 1:
                    t = (job.get("title") or "").strip()
                    if t:
                        priority_titles.append(t)

            priority_tv_ids = set()
            for title in priority_titles:
                try:
                    info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                    if info and info.get("found") and info.get("media_type") == "tv":
                        tid = info.get("tmdb_id")
                        if tid:
                            priority_tv_ids.add(int(tid))
                except Exception as exc:
                    logger.debug("[Calendar Watcher] TMDB lookup failed for title %s: %s", title, exc)

            # Priority 2.2: Seerr requests
            seerr_items = []
            if get_setting("cineinfo_calendar_seerr", "0") == "1":
                seerr_items = _seerr_requested_media()

            priority_media_targets = []
            for tid in priority_tv_ids:
                priority_media_targets.append((tid, "tv"))
            for item in seerr_items:
                tid = item.get("tmdb_id")
                mt = item.get("media_type")
                if tid and mt in ("tv", "movie"):
                    priority_media_targets.append((int(tid), mt))

            # Non-priority 2.3: Media Library series
            mediathek_media_targets = []
            if get_setting("cineinfo_calendar_mediathek", "0") == "1":
                mediathek_series = get_mediascan_series()
                for item in mediathek_series:
                    tid = item.get("tmdb_id")
                    if tid:
                        try:
                            mediathek_media_targets.append((int(tid), "tv"))
                        except ValueError:
                            pass
                    elif item.get("title"):
                        title = item.get("title").strip()
                        try:
                            info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                            if info and info.get("found") and info.get("media_type") == "tv":
                                tid = info.get("tmdb_id")
                                if tid:
                                    mediathek_media_targets.append((int(tid), "tv"))
                        except Exception as exc:
                            logger.debug("[Calendar Watcher] TMDB lookup failed for library series %s: %s", title, exc)

            # Deduplicate Mediathek targets against priority targets
            priority_set = {tid for tid, mt in priority_media_targets}
            mediathek_media_targets = [item for item in mediathek_media_targets if item[0] not in priority_set]

            # Non-priority 2.4: Crunchyroll simulcast / watchlist / lists.
            #    Resolve CR titles -> TMDB ids (throttled; cached in globals
            #    for the request path) and sync their episodes like Mediathek
            #    so the calendar fills in progressively in the background.
            try:
                cr_ids = _cr_calendar_targets(api_key, country, ui_lang, _t.time())
            except Exception as exc:
                logger.debug("[Calendar Watcher] CR targets failed: %s", exc)
                cr_ids = []
            _seen_np = {tid for tid, _mt in mediathek_media_targets}
            for _cid in cr_ids:
                if _cid not in priority_set and _cid not in _seen_np:
                    mediathek_media_targets.append((_cid, "tv"))
                    _seen_np.add(_cid)

            # 3. Retrieve currently cached media status from DB
            all_target_ids = list({tid for tid, mt in (priority_media_targets + mediathek_media_targets)})
            cached_times = get_cached_calendar_media(all_target_ids)

            # Build List A (missing from DB)
            list_a_priority = []
            list_a_mediathek = []
            for tid, mt in priority_media_targets:
                if tid not in cached_times:
                    list_a_priority.append((tid, mt))
            for tid, mt in mediathek_media_targets:
                if tid not in cached_times:
                    list_a_mediathek.append((tid, mt))

            # Build List B (existing in DB, needs refresh)
            refresh_hours = int(get_setting("cineinfo_calendar_refresh_interval", "24"))
            refresh_seconds = refresh_hours * 3600

            list_b = []
            now = _t.time()
            for tid, mt in (priority_media_targets + mediathek_media_targets):
                if tid in cached_times:
                    last_updated = cached_times[tid]
                    if now - last_updated >= refresh_seconds:
                        list_b.append((tid, mt))

            # 4. Processing logic
            global _calendar_watcher_scanning
            global _calendar_watcher_last_sync
            if list_a_priority or list_a_mediathek:
                _busy = True  # keep the fast cadence
                # List A has items: drain a batch this cycle (priority first).
                # The shared TMDB rate limiter (40 req/s) throttles the calls,
                # so the calendar fills in fast instead of one item per cycle.
                last_list_a_empty_time = None
                batch = (list_a_priority + list_a_mediathek)[:_CAL_A_BATCH]
                _calendar_watcher_scanning = True
                try:
                    for target_id, target_type in batch:
                        _sync_calendar_item(target_id, target_type, api_key)
                finally:
                    _calendar_watcher_scanning = False
                    _calendar_watcher_last_sync = _t.time()
            else:
                _busy = False  # back to the calm 10s cadence
                # List A is empty
                if last_list_a_empty_time is None:
                    last_list_a_empty_time = _t.time()
                
                # If List A has been empty for at least 15 minutes (900 seconds), process List B
                if _t.time() - last_list_a_empty_time >= 900:
                    if list_b:
                        target_id, target_type = list_b[0]
                        _calendar_watcher_scanning = True
                        try:
                            _sync_calendar_item(target_id, target_type, api_key)
                        finally:
                            _calendar_watcher_scanning = False
                            _calendar_watcher_last_sync = _t.time()
                        _t.sleep(1.5)
        except Exception as e:
            logger.error("[Calendar Watcher] Error in watcher loop: %s", e, exc_info=True)


def reset_cr_targets():
    """Drop cached CR calendar targets (called when CR settings change)."""
    global _cr_calendar_ids, _cr_calendar_meta, _cr_calendar_titles, _cr_targets_built_at
    _cr_calendar_ids, _cr_calendar_meta, _cr_calendar_titles = [], {}, {}
    _cr_targets_built_at = 0.0


def ensure_calendar_watcher_started():
    """Start the calendar watcher thread exactly once per process."""
    global _calendar_watcher_started
    if not _calendar_watcher_started:
        _calendar_watcher_started = True
        threading.Thread(target=_calendar_watcher_loop, daemon=True, name="calendar-watcher").start()


def register_calendar_routes(app):
    """Register the calendar page route and the calendar-events API route
    on the Flask app."""
    @app.route("/calendar")
    def calendar_page():
        """Serve GET /calendar: render the calendar page, or redirect home
        if the CineInfo calendar feature is disabled in Settings."""
        from ..db import get_setting
        if get_setting("cineinfo_calendar", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        return render_template("calendar.html")
    @app.route("/api/calendar")
    def api_calendar():
        """Serve GET /api/calendar: aggregate upcoming episode air dates for
        the current user's AutoSync jobs (and optionally Seerr requests,
        Media Library series, and Crunchyroll simulcast/watchlist/lists)
        using cached database tables. Called from static/calendar.js's
        `load()`."""
        from ..db import get_setting
        if get_setting("cineinfo_calendar", "0") != "1":
            return jsonify({"error": "Calendar disabled", "events": []}), 403

        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "no_key", "events": []})
        country = get_setting("cineinfo_country", "DE")
        ui_lang = session.get("ui_language", "en")
        _is_en = ui_lang == "en"

        # The calendar cache stores titles/episode names bilingually; pick the
        # column that matches the current UI language (fall back to the primary).
        def _disp_title(ep):
            return (ep["title_en"] or ep["title"]) if _is_en else ep["title"]

        def _disp_name(ep):
            return (ep["name_en"] or ep["name"]) if _is_en else ep["name"]

        def _cr_norm(t):
            return "".join(c for c in (t or "").lower() if c.isalnum())

        def _crunchyroll_calendar_events():
            """Crunchyroll calendar events. The background watcher resolves CR
            titles -> TMDB and syncs episodes; this normally just reads the cached
            ids/meta + DB. If the watcher cache is still cold, it lazily resolves
            the ids here and on-demand syncs a *bounded* batch so the calendar is
            never empty and fills in progressively across the frontend's 10s polls."""
            try:
                from .. import crunchyroll_service as _crs
                if not (_crs.is_enabled() or _crs.has_account()):
                    return []
                ids = list(_cr_calendar_ids)
                if not ids:
                    # Watcher cache cold -> resolve once here (throttled; the call
                    # caches into the module globals for subsequent fast reads).
                    try:
                        ids = _cr_calendar_targets(api_key, country, ui_lang, time.time())
                    except Exception as _e:
                        logger.debug("[Calendar] CR lazy resolve failed: %s", _e)
                        ids = []
                if not ids:
                    return []
                meta = _cr_calendar_meta
                # On-demand sync a bounded number of not-yet-cached ids so the
                # request stays responsive; the watcher fills in the rest.
                cached = get_cached_calendar_media(ids)
                synced = 0
                for tid in ids:
                    if tid in cached:
                        continue
                    try:
                        _sync_calendar_item(tid, "tv", api_key)
                    except Exception as _e:
                        logger.debug("[Calendar] CR on-demand sync %s failed: %s", tid, _e)
                    synced += 1
                    if synced >= _CAL_A_BATCH:
                        break
                out = []
                cutoff = (datetime.now() - timedelta(days=_CR_CAL_PAST_DAYS)).strftime("%Y-%m-%d")
                for ep in get_calendar_episodes_from_db(ids):
                    # Trim the long tail of past episodes (a large watchlist
                    # has thousands); keep a rolling 60-day window + future.
                    if ep["air_date"] and ep["air_date"] < cutoff:
                        continue
                    tid = ep["tmdb_id"]
                    m = meta.get(tid, {})
                    out.append({
                        "job_id": None,
                        "title": m.get("title") or _disp_title(ep),
                        "tmdb_id": tid,
                        "season": ep["season"],
                        "episode": ep["episode"],
                        "name": _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster": ep["poster_path"],
                        "still": ep["still_path"],
                        "source": "crunchyroll",
                        "cr_in_watchlist": m.get("in_wl", False),
                        "cr_in_list": m.get("in_list", False),
                        "cr_lists": sorted(m.get("lists", set())),
                        "cr_kind": ("watchlist" if m.get("in_wl")
                                    else "list" if m.get("in_list")
                                    else "simulcast"),
                    })
                return out
            except Exception as _exc:
                logger.debug("[Calendar] Crunchyroll read failed: %s", _exc)
                return []

        events = []
        seen = set()

        # 1. AutoSync jobs for this user
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs(username=None if is_admin else username)

        autosync_tmdb_ids = []
        job_id_by_tmdb_id = {}
        title_by_tmdb_id = {}
        for job in jobs:
            if job.get("enabled") != 1:
                continue
            title = (job.get("title") or "").strip()
            if not title:
                continue
            try:
                info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                if info and info.get("found") and info.get("media_type") == "tv":
                    tid = info.get("tmdb_id")
                    if tid:
                        tid_int = int(tid)
                        autosync_tmdb_ids.append(tid_int)
                        job_id_by_tmdb_id[tid_int] = job.get("id")
                        title_by_tmdb_id[tid_int] = info.get("title") or title
            except Exception:
                continue

        if autosync_tmdb_ids:
            db_eps = get_calendar_episodes_from_db(autosync_tmdb_ids)
            for ep in db_eps:
                tid = ep["tmdb_id"]
                key = (tid, ep["season"], ep["episode"])
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "job_id":   job_id_by_tmdb_id.get(tid),
                    "title":    title_by_tmdb_id.get(tid) or _disp_title(ep),
                    "tmdb_id":  tid,
                    "season":   ep["season"],
                    "episode":  ep["episode"],
                    "name":     _disp_name(ep),
                    "air_date": ep["air_date"],
                    "poster":   ep["poster_path"],
                    "still":    ep["still_path"],
                    "source":   "autosync",
                })

        # 2. Seerr requests (optional overlay).
        #    Independent of the Media Library option AND of the watcher: Seerr
        #    requests are dynamic and few, so any item that isn't cached yet is
        #    synced on demand here. This guarantees Seerr works immediately even
        #    if the watcher hasn't reached it (or isn't running at all).
        seerr_active = get_setting("cineinfo_calendar_seerr", "0") == "1"
        seerr_count = 0
        if seerr_active:
            seerr_media = _seerr_requested_media()
            seerr_tv_ids = []
            seerr_movie_ids = []
            for m in seerr_media:
                tid = m.get("tmdb_id")
                if not tid:
                    continue
                if m["media_type"] == "tv":
                    seerr_tv_ids.append(int(tid))
                else:
                    seerr_movie_ids.append(int(tid))
            seerr_count = len(seerr_tv_ids) + len(seerr_movie_ids)

            # On-demand sync for any Seerr items missing from the cache.
            seerr_targets = ([(tid, "tv") for tid in seerr_tv_ids]
                             + [(tid, "movie") for tid in seerr_movie_ids])
            if seerr_targets:
                seerr_cached = get_cached_calendar_media([tid for tid, _ in seerr_targets])
                for tid, mt in seerr_targets:
                    if tid not in seerr_cached:
                        try:
                            _sync_calendar_item(tid, mt, api_key)
                        except Exception as _exc:
                            logger.debug("[Calendar] On-demand Seerr sync failed for %s: %s", tid, _exc)

            if seerr_tv_ids:
                db_eps = get_calendar_episodes_from_db(seerr_tv_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = (tid, ep["season"], ep["episode"])
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   ep["season"],
                        "episode":  ep["episode"],
                        "name":     _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    ep["still_path"],
                        "source":   "seerr",
                    })
            if seerr_movie_ids:
                db_eps = get_calendar_episodes_from_db(seerr_movie_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = ("movie", tid)
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   None,
                        "episode":  None,
                        "name":     "",
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    None,
                        "source":   "seerr",
                        "is_movie": True,
                    })

        # 3. Media Library series (optional overlay)
        if get_setting("cineinfo_calendar_mediathek", "0") == "1":
            mediathek_series = get_mediascan_series()
            mediathek_tv_ids = []
            for item in mediathek_series:
                tid = item.get("tmdb_id")
                if tid:
                    try:
                        mediathek_tv_ids.append(int(tid))
                    except ValueError:
                        pass
                elif item.get("title"):
                    title = item.get("title").strip()
                    try:
                        info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                        if info and info.get("found") and info.get("media_type") == "tv":
                            tid = info.get("tmdb_id")
                            if tid:
                                mediathek_tv_ids.append(int(tid))
                    except Exception:
                        continue

            if mediathek_tv_ids:
                db_eps = get_calendar_episodes_from_db(mediathek_tv_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = (tid, ep["season"], ep["episode"])
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   ep["season"],
                        "episode":  ep["episode"],
                        "name":     _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    ep["still_path"],
                        "source":   "mediathek",
                    })

        # 4. Crunchyroll simulcast / watchlist / lists. If a CR episode is already
        #    shown via another source (e.g. a Seerr request), keep that event but
        #    attach the CR membership so the Crunchyroll/watchlist badge also shows.
        ev_by_key = {(e.get("tmdb_id"), e.get("season"), e.get("episode")): e
                     for e in events}
        for ev in _crunchyroll_calendar_events():
            ev["cr_member"] = True
            key = (ev.get("tmdb_id"), ev.get("season"), ev.get("episode"))
            existing = ev_by_key.get(key)
            if existing is not None:
                existing["cr_member"] = True
                existing["cr_in_watchlist"] = ev.get("cr_in_watchlist", False)
                existing["cr_in_list"] = ev.get("cr_in_list", False)
                existing["cr_lists"] = ev.get("cr_lists", [])
                existing["cr_kind"] = ev.get("cr_kind")
                continue
            seen.add(key)
            events.append(ev)
            ev_by_key[key] = ev

        # Title fallback: tag events from other sources (e.g. Seerr) whose
        # title matches a CR title but whose TMDB id differs from the
        # CR-resolved one, so they still get the Crunchyroll/watchlist badge.
        if _cr_calendar_titles:
            for ev in events:
                if ev.get("cr_member"):
                    continue
                tm = _cr_calendar_titles.get(_cr_norm(ev.get("title") or ""))
                if tm:
                    ev["cr_member"] = True
                    ev["cr_in_watchlist"] = tm["in_wl"]
                    ev["cr_in_list"] = tm["in_list"]
                    ev["cr_lists"] = sorted(tm["lists"])
                    ev["cr_kind"] = ("watchlist" if tm["in_wl"]
                                     else "list" if tm["in_list"] else "simulcast")

        events.sort(key=lambda e: (e.get("air_date") or ""))
        return jsonify({
            "events": events,
            "watcher": {
                "active": _calendar_watcher_active,
                "is_scanning": _calendar_watcher_scanning,
                "last_sync": _calendar_watcher_last_sync
            },
            "meta": {
                "seerr_active": seerr_active,
                "seerr_count": seerr_count,
            }
        })
