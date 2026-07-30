"""External REST API (v1).

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

Telemetry: flag.v1_api (stage-2 usage counter) and detail.v1_api (stage-3,
which endpoint was called and whether it worked) are reported from the
after-request hook registered at the bottom of register_v1_api_routes().

Two deliberate restrictions there:
  * Only calls that PASSED the API-key check count. This API is reachable by
    anything that can open a socket to it; an unauthenticated probe from a
    port scanner is not "the user used the external API".
  * Both keys are throttled per process (see _V1_FLAG_INTERVAL /
    _V1_DETAIL_INTERVAL). /api/v1/status is explicitly a poll-me endpoint
    (Home Assistant and friends hit it every few seconds), so one event per
    request would be pure noise.

detail.v1_api carries the Flask ENDPOINT NAME only ("api_v1_queue_item") --
never the concrete path, query string or any id from it.
"""

from .. import selfupdate
from ..db import get_all_library_cache
from ..db import get_autosync_jobs
from ..db import get_download_history
from ..db import get_general_stats
from ..db import get_mediascan_count
from ..db import get_mediascan_last_updated
from ..db import get_queue
from ..db import get_queue_item
from ..db import get_queue_stats
from ..db import get_setting
from ..db import get_upscale_badge_count
from ..db import get_upscale_queue
from ..db import get_uptime_range
from ..mediascan import _mediascan_status
from ..mediascan import _mediascan_status_lock
from ..queue_worker import _is_job_adaptive_paused
from ..runtime_state import _syncing_jobs
from ..runtime_state import _syncing_jobs_lock
from ..runtime_state import is_queue_paused
from ..uptime_monitor import _MONITOR_SITES
from ..uptime_monitor import _uptime_config
from ..version_info import _get_display_version
from flask import Response as _FlaskResponse
from flask import g
from flask import jsonify
from flask import request
import json
import secrets
import threading
import time
from ...logger import get_logger
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events


logger = get_logger(__name__)


# --- Telemetry throttling ---------------------------------------------------
# The external API exists to be polled. flag.v1_api answers "does this install
# use the REST API", so once an hour is all the resolution it needs;
# detail.v1_api answers "which endpoints are used", so it is throttled per
# endpoint+status on a shorter interval -- enough to collapse a poller's burst
# without losing a genuinely different endpoint being called in between.
_V1_FLAG_INTERVAL = 3600.0
_V1_DETAIL_INTERVAL = 300.0
_v1_throttle_lock = threading.Lock()
_v1_last_sent = {}


def _v1_throttle_reserve(key, interval):
    """Reserve the throttle slot for *key* when it has not been reported within
    the last *interval* seconds in this process.

    Checking and arming happen in the same locked step, so two threads can
    never both pass for the same key. The event itself is built afterwards,
    outside the lock (building reads the telemetry settings, so it must not
    run under this lock). When that build produces nothing -- consent for the
    data key is missing -- the caller hands the slot back via
    _v1_throttle_release(), so the throttle only stays armed for calls that
    really did send something.

    Returns an opaque token to pass to _v1_throttle_release(), or None when
    the key is still throttled.
    """
    now = time.monotonic()
    with _v1_throttle_lock:
        last = _v1_last_sent.get(key)
        if last is not None and now - last < interval:
            return None
        _v1_last_sent[key] = now
        return (now, last)


def _v1_throttle_release(key, token):
    """Undo a reservation that did not produce an event, so the next API call
    reports immediately once the user enables telemetry."""
    stamp, previous = token
    with _v1_throttle_lock:
        if _v1_last_sent.get(key) != stamp:
            return  # a later reservation owns the slot now -- leave it alone
        if previous is None:
            _v1_last_sent.pop(key, None)
        else:
            _v1_last_sent[key] = previous


def _report_v1_call(endpoint, status):
    """Submit flag.v1_api + detail.v1_api for one authenticated API call.

    ``endpoint`` is the Flask endpoint name (a fixed route identifier such as
    "api_v1_queue_item"), never the request path -- so no queue id, no
    ?status= filter and no API key can ride along. ``status`` is "ok" or
    "error"; nothing about the response body is looked at.

    Wrapped in its own try/except so a telemetry bug can never affect the API
    response itself.
    """
    try:
        token = _v1_throttle_reserve("flag", _V1_FLAG_INTERVAL)
        if token is not None:
            event = telemetry_events.build_feature_flag_event("flag.v1_api")
            if event is not None:
                telemetry_client.submit(event)
            else:
                _v1_throttle_release("flag", token)
        detail_key = "detail:%s:%s" % (endpoint, status)
        token = _v1_throttle_reserve(detail_key, _V1_DETAIL_INTERVAL)
        if token is not None:
            event = telemetry_events.build_feature_detail_event(
                "detail.v1_api", action="call", status=status,
                metadata={"endpoint": endpoint},
            )
            if event is not None:
                telemetry_client.submit(event)
            else:
                _v1_throttle_release(detail_key, token)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit v1 API events", exc_info=True)


def _v1_json(data, status=200):
    """Pretty-printed JSON response for all /api/v1/ endpoints."""
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    return _FlaskResponse(body, status=status, mimetype="application/json")


def _check_api_key():
    """Return a 401 JSON response if the API key is invalid, else None.

    Accepts the key either via the X-Api-Key header (preferred) or an
    ?apikey= query param — the latter matches the example URL shown on the
    Settings page's API docs table, which used to be undocumented dead
    weight since only the header was actually checked.
    """
    stored = get_setting("external_api_key", "")
    if not stored:
        return jsonify({"error": "API key not configured"}), 500
    provided = request.headers.get("X-Api-Key", "") or request.args.get("apikey", "")
    if not provided or not secrets.compare_digest(provided, stored):
        return _v1_json({
            "error": "Unauthorized",
            "message": "Provide your API key via the X-Api-Key header or an ?apikey= query param.",
        }, status=401)
    # Marks this request as a genuine API call for the telemetry hook below --
    # unauthenticated probes must not count as "the REST API was used".
    g._v1_authenticated = True
    return None


def _v1_library_data(only_movies: bool | None = None):
    """Return library cache as a clean list of location objects."""
    cache = get_all_library_cache()
    locations = []
    for path_key, entry in cache.items():
        loc_data = entry.get("data") or {}
        label        = loc_data.get("label", path_key)
        cp_id        = loc_data.get("custom_path_id")
        is_scanning  = entry.get("is_scanning", False)
        scanned_at   = entry.get("scanned_at")

        all_titles = []
        lang_folders = loc_data.get("lang_folders") or []
        if lang_folders:
            for lf in lang_folders:
                for t in (lf.get("titles") or []):
                    all_titles.append({**t, "_lang_folder": lf.get("name")})
        else:
            for t in (loc_data.get("titles") or []):
                all_titles.append(t)

        if only_movies is True:
            all_titles = [t for t in all_titles if t.get("is_movie")]
        elif only_movies is False:
            all_titles = [t for t in all_titles if not t.get("is_movie")]

        clean_titles = []
        for t in all_titles:
            seasons_clean = {}
            for skey, eps in (t.get("seasons") or {}).items():
                seasons_clean[skey] = [
                    {
                        "episode":       e.get("episode"),
                        "file":          e.get("file"),
                        "size":          e.get("size", 0),
                        "is_movie_file": e.get("is_movie_file", False),
                    }
                    for e in eps
                ]
            clean_titles.append({
                "folder":         t.get("folder"),
                "is_movie":       t.get("is_movie", False),
                "total_episodes": t.get("total_episodes", 0),
                "total_size":     t.get("total_size", 0),
                "lang_folder":    t.get("_lang_folder"),
                "seasons":        seasons_clean,
            })

        locations.append({
            "location":       label,
            "custom_path_id": cp_id,
            "is_scanning":    is_scanning,
            "scanned_at":     scanned_at,
            "title_count":    len(clean_titles),
            "titles":         clean_titles,
        })
    return _v1_json(locations)


def register_v1_api_routes(app):
    """Register the /api/v1/* external REST API (auth'd via X-Api-Key header).

    This is a separate, stable, machine-readable API intended for external
    tools/scripts, distinct from the internal /api/* endpoints the web UI
    itself uses (those are not versioned and can change shape freely).
    """
    @app.route("/api/v1/status")
    def api_v1_status():
        """Overall downloader status — safe to poll frequently."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        from ...models.common.common import get_ffmpeg_progress
        stats  = get_queue_stats()
        ffmpeg = get_ffmpeg_progress()
        r      = stats["currently_running"]

        if r:
            cur = r.get("current_episode") or 0
            tot = r.get("total_episodes") or 0
            # current_episode = i (0-based loop index) → i episodes fully done,
            # episode i+1 is in progress.  Mirror queue.js logic exactly:
            #   epPct  = cur / tot * 100
            #   inEpPct = 100 if ffmpeg phase (download done), else dl percent
            #   overall = epPct + inEpPct / tot
            ep_pct   = round(ffmpeg.get("percent") or 0) if ffmpeg.get("active") else 0
            in_ep    = 100 if (ffmpeg.get("active") and ffmpeg.get("phase") == "ffmpeg") else ep_pct
            overall_pct = round(((cur + in_ep / 100) / tot * 100) if tot > 0 else 0)
            r["episode_progress"] = {
                "percent":       ep_pct,
                "phase":         ffmpeg.get("phase", ""),
                "speed":         ffmpeg.get("speed", ""),
                "bandwidth":     ffmpeg.get("bandwidth", ""),
                "downloaded_mb": round(ffmpeg.get("downloaded_mb", 0.0), 1),
                "active":        ffmpeg.get("active", False),
            }
            r["overall_progress_percent"] = overall_pct

        return _v1_json({
            "version": _get_display_version(),
            "paused": is_queue_paused(),
            "queue": {
                "total":     stats["total"],
                "queued":    stats["by_status"].get("queued", 0),
                "running":   stats["by_status"].get("running", 0),
                "completed": stats["by_status"].get("completed", 0),
                "failed":    stats["by_status"].get("failed", 0),
                "cancelled": stats["by_status"].get("cancelled", 0),
            },
            "currently_running": r,
        })
    @app.route("/api/v1/queue")
    def api_v1_queue():
        """All queue items, optionally filtered by ?status=<status>."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        items = get_queue()
        status_filter = request.args.get("status", "").strip().lower()
        if status_filter:
            items = [i for i in items if i.get("status") == status_filter]
        for item in items:
            if isinstance(item.get("episodes"), str):
                try:
                    item["episodes"] = json.loads(item["episodes"])
                except Exception:
                    pass
        return _v1_json(items)
    @app.route("/api/v1/queue/<int:queue_id>")
    def api_v1_queue_item(queue_id):
        """Single queue item detail."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        item = get_queue_item(queue_id)
        if not item:
            return _v1_json({"error": "Not found"}, status=404)
        if isinstance(item.get("episodes"), str):
            try:
                item["episodes"] = json.loads(item["episodes"])
            except Exception:
                pass
        return _v1_json(item)
    @app.route("/api/v1/library")
    def api_v1_library():
        """Full library — all titles (series + movies)."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=None)
    @app.route("/api/v1/library/series")
    def api_v1_library_series():
        """Library — series only (no movies)."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=False)
    @app.route("/api/v1/library/movies")
    def api_v1_library_movies():
        """Library — movies only."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=True)
    @app.route("/api/v1/stats")
    def api_v1_stats():
        """Download statistics."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_json(get_general_stats())
    @app.route("/api/v1/autosync")
    def api_v1_autosync():
        """AutoSync jobs — status overview (all jobs, all users).

        Unlike the internal GET /api/autosync (session-authed, filtered to the
        current user's own jobs), this always returns every job — the external
        API has no notion of a logged-in user, only the shared API key.
        """
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        jobs = get_autosync_jobs()
        with _syncing_jobs_lock:
            running_ids = set(_syncing_jobs)
        for job in jobs:
            job["adaptive_paused"] = _is_job_adaptive_paused(job)
            job["running"] = job.get("id") in running_ids
        return _v1_json(jobs)
    @app.route("/api/v1/uptime")
    def api_v1_uptime():
        """UpTime monitor — current status per tracked source.

        Lightweight variant of the internal /api/uptime/status: current
        status/uptime%/avg response time only, no bucketed history (that's
        a UI-chart concern, not something worth shipping to external pollers).
        """
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        import time as _t
        cfg = _uptime_config()
        now = int(_t.time())
        window = min(6 * 3600, cfg["retention_days"] * 86400)
        sources = []
        for _sid, (_label, _url, _domain, _markers, _headers) in _MONITOR_SITES.items():
            rr = get_uptime_range(_sid, now - window, now, n_buckets=1)
            latest = rr["latest"] or {}
            sources.append({
                "id":               _sid,
                "label":            _label,
                "tracked":          cfg["tracked"].get(_sid, False),
                "current_status":   latest.get("status"),
                "last_response_ms": latest.get("response_ms"),
                "uptime_pct":       rr["stats"]["uptime_pct"],
                "avg_ms":           rr["stats"]["avg_ms"],
            })
        return _v1_json({
            "enabled": cfg["enabled"],
            "interval": cfg["interval"],
            "sources": sources,
        })
    @app.route("/api/v1/update-status")
    def api_v1_update_status():
        """Self-update progress/state (download/apply progress of an in-flight
        update, or the idle state if none is running)."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_json(selfupdate.read_status())
    @app.route("/api/v1/mediascan")
    def api_v1_mediascan():
        """MediaScan (Plex/Jellyfin library import) run status + cached count."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        with _mediascan_status_lock:
            snap = dict(_mediascan_status)
        return _v1_json({
            "running":      snap["running"],
            "started_at":   snap["started_at"],
            "finished_at":  snap["finished_at"],
            "count":        snap["count"],
            "total":        snap["total"],
            "error":        snap["error"],
            "source":       snap["source"],
            "last_updated": get_mediascan_last_updated(),
            "cached_count": get_mediascan_count(),
        })
    @app.route("/api/v1/upscale")
    def api_v1_upscale():
        """Upscale queue — all items, badge count, and current job progress."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        try:
            from ...anime4k.anime4k import get_upscale_progress
            progress = get_upscale_progress()
        except Exception:
            progress = {"active": False, "percent": 0}
        return _v1_json({
            "items":    get_upscale_queue(),
            "badge":    get_upscale_badge_count(),
            "progress": progress,
        })
    @app.route("/api/v1/history")
    def api_v1_history():
        """Download history — all users, optionally filtered/paginated.

        Unlike the internal GET /api/history (session-authed, filtered to the
        current user's own entries unless admin), this always returns every
        user's entries — same "no session, just the API key" reasoning as
        /api/v1/autosync above.

        Query params: ?limit=&offset=&status=&source=
        """
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        status = (request.args.get("status") or "").strip() or None
        source = (request.args.get("source") or "").strip() or None
        entries, total = get_download_history(
            username=None, status=status, source=source,
            limit=limit, offset=offset,
        )
        return _v1_json({"entries": entries, "total": total, "limit": limit, "offset": offset})

    @app.after_request
    def _v1_api_telemetry(response):
        """Report an authenticated /api/v1/* call (see the module docstring).

        Registered as a normal after_request hook: it runs for every request,
        but returns immediately unless _check_api_key() marked this one as an
        authenticated API call. A client that hangs up mid-response never gets
        here at all, so a cancelled request cannot produce an "error" event.
        """
        try:
            if getattr(g, "_v1_authenticated", False):
                _report_v1_call(request.endpoint or "unknown",
                                "ok" if response.status_code < 400 else "error")
        except Exception:
            logger.debug("[Telemetry] v1 API after_request hook failed", exc_info=True)
        return response
