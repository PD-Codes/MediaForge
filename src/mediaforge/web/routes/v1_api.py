"""External REST API (v1).

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.v1_api (usage counter) and detail.v1_api
# (per-endpoint usage frequency) -- see telemetry/registry.py.
# Registry-only for now.
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
from flask import jsonify
from flask import request
import json
import secrets


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
