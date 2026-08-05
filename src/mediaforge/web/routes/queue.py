"""Download queue routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import add_to_queue
from ..db import cancel_queue_item
from ..db import clear_completed
from ..db import get_queue
from ..db import get_queue_item
from ..db import move_queue_item
from ..db import remove_from_queue
from ..db import restart_queue_item_inplace
from ..db import retry_single_episode
from ..db import update_queue_progress
from ..runtime_state import _active_cancel_events
from ..runtime_state import _active_cancel_events_lock
from ..runtime_state import is_queue_paused
from ..runtime_state import request_episode_skip
from ..runtime_state import set_queue_paused
from .. import runtime_state
from ..auth import get_current_user
from ..language_groups import is_group_ref
from ..language_groups import lang_separation_enabled
from ..language_groups import language_display
from ..language_groups import resolve_chain
from ..queue_worker import _dl_lock
from flask import jsonify
from flask import request
import atexit
import concurrent.futures
import json
import os
import threading


# ---------------------------------------------------------------------------
# Non-TMDB poster fallback (background-resolved, cache-only on the request path)
# ---------------------------------------------------------------------------
# Some sources have no TMDB entry at all (an adult-content module, for
# example) or the admin never configured a TMDB key, so the TMDB-cache lookup
# in _attach_cached_posters() below never has anything to attach for them.
# Their Provider's own series page usually already carries a poster -- the
# same poster_url the browse/search cards show -- but getting it means
# constructing ``series_cls(url=...)`` and reading ``.poster_url``, which for
# most providers triggers a real, uncached network fetch (see
# routes/browse.py's ``_prefetch_worker``, which does the exact same
# construction from a background thread for exactly this reason). /api/queue
# is polled every ~2s, so that fetch must never happen inline on this
# request's own thread -- the same rule _attach_cached_posters()'s docstring
# already states for TMDB.
#
# Cached through db.get_provider_cache()/set_provider_cache() -- the same
# namespaced, TTL'd table Crunchyroll's/Fernsehserien.de's availability pills
# already share -- rather than a plain process-memory dict, so a resolved
# poster survives a restart instead of every queue view re-fetching once per
# run. resolve_provider() checks third-party providers (register_provider)
# exactly like built-in ones, so this works the same for a module's own
# content source with no extra wiring -- as long as the module also called
# image_proxy.register_image_hosts() for its poster's CDN, or the proxy will
# still 403 it.
_QUEUE_POSTER_NAMESPACE = "queue_poster_fallback"
_QUEUE_POSTER_TTL = 86400.0  # 24h, matches provider_cache's own housekeeping window

_queue_poster_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="queue-poster")
atexit.register(_queue_poster_pool.shutdown, wait=False)

# series_url values a background resolve is already running for -- without
# this, every ~2s poll before the first fetch completes would submit another
# one for the same URL (the pool has only one worker, so they would queue up
# behind each other and each still hit the network in turn).
_queue_poster_inflight: set = set()
_queue_poster_inflight_lock = threading.Lock()


def _resolve_queue_poster_bg(series_url):
    """Background worker: resolve one series' poster via its Provider class
    and cache the result -- positive or negative, so a source with no poster
    (or a provider that's down) is not retried on every poll."""
    poster = ""
    try:
        from ...providers import resolve_provider
        from .image_proxy import _poster_proxy

        prov = resolve_provider(series_url)
        series_cls = getattr(prov, "series_cls", None)
        if series_cls:
            s_inst = series_cls(url=series_url)
            raw = getattr(s_inst, "poster_url", "") or getattr(s_inst, "poster", "")
            if raw:
                poster = _poster_proxy(raw)
    except Exception:
        pass
    try:
        from ..db import set_provider_cache
        set_provider_cache(_QUEUE_POSTER_NAMESPACE, series_url, {"poster": poster})
    except Exception:
        pass
    with _queue_poster_inflight_lock:
        _queue_poster_inflight.discard(series_url)


def _queue_poster_fallback(series_url):
    """Cached, non-blocking read: returns a cached poster URL, or None if
    nothing is cached yet (including "resolved to no poster") -- kicking off
    a background resolve in that case so the *next* poll finds it."""
    from ..db import get_provider_cache

    cached = get_provider_cache(_QUEUE_POSTER_NAMESPACE, series_url, ttl=_QUEUE_POSTER_TTL)
    if cached is not None:
        return cached.get("poster") or None

    with _queue_poster_inflight_lock:
        if series_url in _queue_poster_inflight:
            return None
        _queue_poster_inflight.add(series_url)
    _queue_poster_pool.submit(_resolve_queue_poster_bg, series_url)
    return None


def _attach_cached_posters(items):
    """Add a poster URL to queue rows — cache-only on this request's own
    thread, never a live fetch.

    download_queue has no poster column and /api/queue is polled every two
    seconds, so a lookup that could reach out to a source site or TMDB
    inline is out of the question. Two independent cache layers feed
    ``poster``, in order:

    1. The TMDB cache the browse/search pages already fill (below).
    2. For anything still unset afterwards -- no TMDB key configured, or a
       source with no TMDB entry at all -- the provider-resolved fallback
       above (:func:`_queue_poster_fallback`), itself only ever a cache read
       plus a fire-and-forget background resolve on a miss.

    Either way ``poster`` is left unset when there is no hit yet, and the
    queue hub falls back to its gradient placeholder for that poll. The URL
    handed to the client is always the /api/img proxy, never a source site
    or image.tmdb.org directly (same rule as everywhere else — see
    routes/image_proxy.py).
    """
    try:
        from ..db import get_setting
        from ..db import get_tmdb_cache_bulk
        from flask import session
        import urllib.parse as _up

        if get_setting("cineinfo_tmdb_api_key", ""):
            country = get_setting("cineinfo_country", "DE") or "DE"
            ui_lang = session.get("ui_language", "de")

            keys = {}
            for it in items:
                title = it.get("title")
                if title:
                    keys[title] = title + "|||" + country + "|||" + ui_lang
            if keys:
                hits = get_tmdb_cache_bulk(list(keys.values()))
                for it in items:
                    cached = hits.get(keys.get(it.get("title"), ""))
                    if not isinstance(cached, dict):
                        continue
                    details = cached.get("raw_details")
                    path = details.get("poster_path") if isinstance(details, dict) else None
                    if path:
                        raw = "https://image.tmdb.org/t/p/w154" + path
                        it["poster"] = "/api/img?url=" + _up.quote(raw, safe="")

        for it in items:
            if it.get("poster"):
                continue
            series_url = it.get("series_url") or ""
            if not series_url:
                continue
            poster = _queue_poster_fallback(series_url)
            if poster:
                it["poster"] = poster
    except Exception:
        # A missing poster must never cost the queue its response.
        pass


def register_queue_routes(app):
    """Register the download queue CRUD, pause/resume and per-item control endpoints."""
    @app.route("/api/download", methods=["POST"])
    def api_download():
        """Queue a new download for one or more episodes of a series.

        POST /api/download. Called from app.js's _submitDownloadGroups()
        and startDownloadAllLangs(), and from seerr.js, whenever the user
        submits a download from the search/series modal.
        """
        data = request.get_json(silent=True) or {}
        episodes = data.get("episodes", [])
        language = data.get("language", "German Dub")
        provider = data.get("provider", "VOE")
        title = data.get("title", "Unknown")
        series_url = str(data.get("series_url", "")).strip().rstrip("/")
        if not series_url:
            return jsonify({"error": "series_url is required"}), 400

        if not episodes:
            return jsonify({"error": "episodes list is required"}), 400

        # A kids ACCOUNT may not download at all. Not "may download things
        # under the limit": the limit is judged from TMDB metadata that may
        # simply be absent, so an allowed download would be one nobody could
        # vouch for -- and the file it produces then sits in the library
        # forever. The kids MODE (a shared account, PIN-protected) is left
        # alone: an adult is the one who set it and can step back out of it.
        from ..age_gate import is_kids_account
        if is_kids_account():
            return jsonify({"error": "not permitted", "code": "age_limited"}), 403

        if (
            language == "English Sub"
            and os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
        ):
            return jsonify({"error": "English Sub downloads are disabled"}), 403

        # A language fallback group is stored as-is ("group:<id>") and resolved
        # per episode by the queue worker; all that's checked here is that it
        # can still work, so a stale dropdown can't queue an item that is
        # guaranteed to fail later. (resolve_chain also drops English Sub when
        # that language is globally disabled, hence the empty check covering a
        # group that consisted only of it.)
        if is_group_ref(language):
            if not lang_separation_enabled():
                return jsonify({"error": "Sprachgruppen benötigen die Einstellung 'Sprachen in Ordner trennen'."}), 400
            if not resolve_chain(language):
                return jsonify({"error": "Diese Sprachgruppe existiert nicht mehr."}), 400

        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            if user:
                username = (
                    user.get("username")
                    if isinstance(user, dict)
                    else getattr(user, "username", None)
                )

        custom_path_id = data.get("custom_path_id")

        # Global lock to prevent race conditions during duplicate check + add
        with _dl_lock:
            # Check for duplicates before adding to queue
            from ..db import is_series_queued_or_running
            if is_series_queued_or_running(series_url, language, requested_episodes=episodes):
                return jsonify({"error": "Diese Episoden befinden sich bereits in der Warteschlange (gleiche Sprache)."}), 400

            upscale = bool(data.get("upscale", False))
            queue_id = add_to_queue(
                title,
                series_url,
                episodes,
                language,
                provider,
                username,
                custom_path_id=custom_path_id,
                upscale=upscale,
            )
        return jsonify({"queue_id": queue_id})
    @app.route("/api/queue")
    def api_queue():
        """Return all queue items plus ffmpeg encode progress and pause state.

        GET /api/queue. Polled by queue.js's loadQueue() to render the
        download queue modal.
        """
        from ...models.common.common import get_ffmpeg_progress

        items = get_queue()
        ffmpeg_pct = get_ffmpeg_progress()
        # Items using a fallback group store the internal "group:<id>"; the
        # queue rows show the group's name instead.
        from ..error_explain import summarize as _explain_errors

        for _it in items:
            _it["language_label"] = language_display(_it.get("language"))
            # Group this item's raw errors by cause. A twelve-episode job that
            # failed twelve times almost always failed for one reason, and the
            # queue is the place that has to say which -- a traceback there is
            # information nobody can act on.
            if _it.get("errors") and _it["errors"] != "[]":
                try:
                    _it["error_summary"] = _explain_errors(json.loads(_it["errors"]))
                except Exception:
                    _it["error_summary"] = None
        _attach_cached_posters(items)

        return jsonify({
            "items": items,
            "ffmpeg_progress": ffmpeg_pct,
            "paused": is_queue_paused()
        })
    @app.route("/api/queue/badge")
    def api_queue_badge():
        """Return just the active download count and its series URLs.

        GET /api/queue/badge. Polled by queue.js's badge timer on every page.
        Mirrors /api/encoding/queue/badge and /api/upscale/badge; the badge
        used to poll the full /api/queue payload instead.

        Also carries the running job, its ffmpeg progress and the pause flag:
        the home page's run strip (renderHomeRunStrip in app.js) has no poller
        of its own, and /api/queue only runs while the queue hub is open, so
        the strip froze on whatever snapshot it happened to get. This stays
        cheap -- five scalar columns plus an in-memory progress dict, no
        episodes JSON and no poster lookup.
        """
        from ...models.common.common import get_ffmpeg_progress
        from ..db import get_queue_badge_info
        info = get_queue_badge_info()
        return jsonify({
            "ok": True,
            "badge": info["active"],
            "urls": info["urls"],
            "running": info.get("running"),
            "queued": info.get("queued", 0),
            "ffmpeg_progress": get_ffmpeg_progress(),
            "paused": is_queue_paused(),
        })
    @app.route("/api/queue/pause", methods=["POST"])
    def api_queue_pause():
        """Pause the download queue worker.

        POST /api/queue/pause. Called from queue.js's toggleQueuePause().
        """
        set_queue_paused(True)
        return jsonify({"paused": True})
    @app.route("/api/queue/resume", methods=["POST"])
    def api_queue_resume():
        """Resume the download queue worker.

        POST /api/queue/resume. Called from queue.js's toggleQueuePause().
        """
        set_queue_paused(False)
        return jsonify({"paused": False})
    @app.route("/api/queue/<int:queue_id>", methods=["DELETE"])
    def api_queue_remove(queue_id):
        """Remove a single queue item.

        DELETE /api/queue/<queue_id>. Called from queue.js's
        removeQueueItem().
        """
        ok, err = remove_from_queue(queue_id)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})
    @app.route("/api/queue/<int:queue_id>/cancel", methods=["POST"])
    def api_queue_cancel(queue_id):
        """Cancel a running or queued download item.

        POST /api/queue/<queue_id>/cancel. Called from queue.js's
        cancelQueueItem().
        """
        item = get_queue_item(queue_id)
        ok, err = cancel_queue_item(queue_id)
        if not ok:
            return jsonify({"error": err}), 400
        # Signal the worker to kill the active subprocess immediately. The same
        # event is handed to the captcha solver (see queue_worker), so a cancel
        # during a captcha tears the patchright browser down too.
        with _active_cancel_events_lock:
            ev = _active_cancel_events.get(queue_id)
        if ev is not None:
            ev.set()
        # Clear the "currently downloading" URL right here rather than waiting
        # for the worker to reach its own clear. queue.js derives the
        # "Cancelling..." state from status=cancelled + a non-empty
        # current_url; when the worker was parked in a blocking scrape the row
        # stayed stuck on "finishing current episode..." long after yt-dlp had
        # already stopped. The abort is immediate now, so the row should say so.
        try:
            update_queue_progress(queue_id, (item or {}).get("current_episode") or 0, "")
        except Exception:
            pass
        return jsonify({"ok": True})
    @app.route("/api/queue/<int:queue_id>/restart", methods=["POST"])
    def api_queue_restart(queue_id):
        """Restart a failed, cancelled or completed queue item.

        POST /api/queue/<queue_id>/restart. Called from queue.js's
        restartQueueItem(). Re-queues only the previously failed episode
        URLs when available, otherwise the full episode list.
        """
        import json as _json
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] not in ("failed", "cancelled", "completed"):
            return jsonify({"error": "Only failed, cancelled or completed items can be restarted"}), 400

        # Prefer re-queuing only the failed episode URLs; fall back to full list
        try:
            errors = _json.loads(item.get("errors") or "[]")
            failed_urls = [e["url"] for e in errors if e.get("url")]
        except Exception:
            failed_urls = []

        if failed_urls:
            episodes = failed_urls
        else:
            try:
                episodes = _json.loads(item.get("episodes") or "[]")
            except Exception:
                return jsonify({"error": "Could not parse episode list"}), 500

        if not episodes:
            return jsonify({"error": "No episodes to restart"}), 400

        # Reset the existing row in-place (no new row created)
        ok, err = restart_queue_item_inplace(queue_id, episodes)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True, "queue_id": queue_id, "episodes": len(episodes)})
    @app.route("/api/queue/<int:queue_id>/skip-episode", methods=["POST"])
    def api_queue_skip_episode(queue_id):
        """Signal the worker to skip the current episode after its active attempt finishes.

        POST /api/queue/<queue_id>/skip-episode. No confirmed frontend
        caller found in static/ or templates/ at time of writing.
        """
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] != "running":
            return jsonify({"error": "Job is not running"}), 400
        request_episode_skip(queue_id)
        return jsonify({"ok": True})
    @app.route("/api/queue/<int:queue_id>/retry-episode", methods=["POST"])
    def api_queue_retry_episode(queue_id):
        """Retry a single failed episode URL, preserving all other episode errors.

        POST /api/queue/<queue_id>/retry-episode. No confirmed frontend
        caller found in static/ or templates/ at time of writing.
        """
        data = request.get_json(silent=True) or {}
        ep_url = data.get("url", "").strip()
        if not ep_url:
            return jsonify({"error": "Missing episode URL"}), 400
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] not in ("failed", "cancelled", "completed"):
            return jsonify({"error": "Only failed, cancelled or completed items support per-episode retry"}), 400
        ok, err = retry_single_episode(queue_id, ep_url)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})
    @app.route("/api/queue/<int:queue_id>/move", methods=["POST"])
    def api_queue_move(queue_id):
        """Move a queue item up or down in the queue order.

        POST /api/queue/<queue_id>/move. Called from queue.js's
        moveQueueItem(id, direction).
        """
        data = request.get_json(silent=True) or {}
        direction = data.get("direction", "").strip()
        if direction not in ("up", "down"):
            return jsonify({"error": "direction must be 'up' or 'down'"}), 400
        ok, err = move_queue_item(queue_id, direction)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})
    @app.route("/api/queue/bulk", methods=["POST"])
    def api_queue_bulk():
        """Apply one action to several queue items at once.

        POST /api/queue/bulk with {"ids": [...], "action": "cancel"|"remove"|
        "retry"|"top"|"bottom"}. Called from queue.js's bulk toolbar.

        Every id is processed independently and its result reported: a
        selection of twenty where one item was already gone must not fail the
        other nineteen, and "it didn't work" without saying which one is the
        reason bulk actions get distrusted.
        """
        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).strip()
        raw_ids = data.get("ids") or []
        if action not in ("cancel", "remove", "retry", "top", "bottom"):
            return jsonify({"error": "unknown action"}), 400

        # "retry" re-queues downloads, which is the one thing a kids account
        # must not do -- /api/download refuses it for the same reason (see
        # api_download above). The other actions stay open, matching the
        # standing of the singular endpoints they batch.
        if action == "retry":
            from ..age_gate import is_kids_account
            if is_kids_account():
                return jsonify({"error": "not permitted", "code": "age_limited"}), 403
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "no items selected"}), 400
        # Cap the batch. The ids come from a browser and each one is a
        # database round trip; an unbounded list is a free way to tie up a
        # worker thread for as long as the caller likes.
        if len(raw_ids) > 500:
            return jsonify({"error": "too many items"}), 400

        ids = []
        for value in raw_ids:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue

        done, failed = [], {}
        for queue_id in ids:
            try:
                if action == "cancel":
                    ok, err = cancel_queue_item(queue_id)
                    if ok:
                        with _active_cancel_events_lock:
                            ev = _active_cancel_events.get(queue_id)
                        if ev is not None:
                            ev.set()
                        try:
                            update_queue_progress(queue_id, 0, "")
                        except Exception:
                            pass
                elif action == "remove":
                    ok, err = remove_from_queue(queue_id)
                elif action == "retry":
                    item = get_queue_item(queue_id)
                    if not item:
                        ok, err = False, "not found"
                    else:
                        ok, err = restart_queue_item_inplace(
                            queue_id, json.loads(item.get("episodes") or "[]"))
                else:
                    # "top" / "bottom": move_queue_item only steps one place,
                    # so repeat it. Bounded by the queue length, which is what
                    # the loop counts down -- not by a while-True that a
                    # move which silently stops working would turn into a hang.
                    direction = "up" if action == "top" else "down"
                    ok, err = True, None
                    for _ in range(len(get_queue()) or 1):
                        moved, _merr = move_queue_item(queue_id, direction)
                        if not moved:
                            break
                if ok:
                    done.append(queue_id)
                else:
                    failed[str(queue_id)] = err or "failed"
            except Exception as exc:
                failed[str(queue_id)] = str(exc)

        from .. import audit as _audit
        _audit.audit("queue", "bulk_%s" % action,
                     target="%d item(s)" % len(done),
                     detail={"ok": done, "failed": failed},
                     outcome="success" if not failed else "partial")

        return jsonify({"ok": True, "action": action,
                        "succeeded": done, "failed": failed})

    @app.route("/api/queue/completed", methods=["DELETE"])
    def api_queue_clear():
        """Remove all completed items from the queue.

        DELETE /api/queue/completed. Called from queue.js's
        clearOldQueueItems().
        """
        clear_completed()
        return jsonify({"ok": True})
