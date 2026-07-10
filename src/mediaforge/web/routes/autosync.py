"""AutoSync routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...search import hanime_search
from ...search import megakino_search
from ...search import query as aniworld_query
from ...search import query_s_to
from ..autosync_worker import _normalize_episode_filter
from ..autosync_worker import _run_autosync_for_job
from ..db import add_autosync_job
from ..db import find_autosync_by_url
from ..db import get_autosync_job
from ..db import get_autosync_jobs
from ..db import get_setting
from ..db import remove_autosync_job
from ..db import update_autosync_job
from ..queue_worker import _hanime_enabled
from ..queue_worker import _is_job_adaptive_paused
from ..runtime_state import _SERIES_LINK_PATTERN
from ..runtime_state import _STO_SERIES_LINK_PATTERN
from ..runtime_state import _syncing_jobs
from ..runtime_state import _syncing_jobs_lock
from flask import jsonify
from flask import render_template
from flask import request
from html import unescape as _html_unescape
import json
import os
import re
import threading
from ..request_context import get_current_user_info as _get_current_user_info
from ...logger import get_logger


logger = get_logger(__name__)


def find_site_candidates(title: str) -> list:
    """Resolve a free-text title to candidate series/movie pages on
    AniWorld / S.TO / MegaKino (+ hanime if enabled), each scored against
    `title` by fuzzy string similarity, best match first (top 12).

    Extracted out of api_autosync_site_search's body (see that route,
    still the only in-app caller reachable over HTTP, via
    static/library.js's libAddToAutosync()) so other code that needs the
    same "is this actually findable on a site" check can call it directly
    in-process instead of round-tripping through HTTP -- see
    web/thirdparties/mediacalendar/service.py's planned-download worker,
    which polls this once an hour per pending release to auto-create an
    AutoSync job the moment a release becomes available on one of these
    sites.
    """
    import difflib

    def _norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    target = _norm(title)

    candidates = []
    seen = set()

    def _collect(items, site, site_label, base, pattern):
        if isinstance(items, dict):
            items = [items]
        for item in (items or []):
            link = item.get("link") or item.get("url", "")
            if not pattern.match(link):
                continue
            name = _html_unescape(
                item.get("title") or item.get("name", "Unknown")
            ).replace("<em>", "").replace("</em>", "")
            url = base + link
            if url in seen:
                continue
            seen.add(url)
            score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
            candidates.append({
                "site": site, "site_label": site_label,
                "title": name, "url": url, "score": round(score, 3),
            })

    try:
        _collect(aniworld_query(title), "aniworld", "AniWorld",
                 "https://aniworld.to", _SERIES_LINK_PATTERN)
    except Exception as e:
        logger.debug("[AutosyncSearch] AniWorld search failed: %s", e)
    try:
        _collect(query_s_to(title), "sto", "S.TO",
                 "https://serienstream.to", _STO_SERIES_LINK_PATTERN)
    except Exception as e:
        logger.debug("[AutosyncSearch] S.TO search failed: %s", e)
    try:
        for item in (megakino_search(title) or []):
            url = item.get("url", "")
            if not item.get("is_series"):  # Auto-Sync tracks series only
                continue
            if url in seen:
                continue
            seen.add(url)
            name = _html_unescape(item.get("title") or "Unknown")
            score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
            candidates.append({
                "site": "megakino", "site_label": "MegaKino",
                "title": name, "url": url, "score": round(score, 3),
            })
    except Exception as e:
        logger.debug("[AutosyncSearch] MegaKino search failed: %s", e)
    if _hanime_enabled():
        try:
            for item in (hanime_search(title) or []):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                name = _html_unescape(item.get("title") or "Unknown")
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": "hanime", "site_label": "hanime 18+",
                    "title": name, "url": url, "score": round(score, 3),
                })
        except Exception as e:
            logger.debug("[AutosyncSearch] hanime search failed: %s", e)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:12]


def register_autosync_routes(app):
    """Register all AutoSync job management routes (CRUD, triggering, batch
    operations, import/export) on the given Flask app."""
    @app.route("/autosync")
    def autosync_page():
        """Render the AutoSync jobs page. Route: GET /autosync."""
        return render_template("autosync.html")
    @app.route("/api/autosync")
    def api_autosync_list():
        """List AutoSync jobs (all for admins, own jobs only for regular users).

        Route: GET /api/autosync. Called from static/autosync.js's
        `loadAutosyncJobs()` and `openEditModal()`.
        """
        username, is_admin = _get_current_user_info()
        # Admins see all jobs; regular users see only their own
        jobs = get_autosync_jobs(username=None if is_admin else username)
        for job in jobs:
            job["adaptive_paused"] = _is_job_adaptive_paused(job)
        return jsonify({"jobs": jobs})
    @app.route("/api/autosync", methods=["POST"])
    def api_autosync_create():
        """Create a new AutoSync job for a series URL.

        Route: POST /api/autosync. Called from static/autosync_filter.js's
        `openCreate()` save handler.
        """
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        series_url = (data.get("series_url") or "").strip()
        language = data.get("language", "German Dub")
        provider = data.get("provider", "VOE")
        custom_path_id = data.get("custom_path_id")
        movie_custom_path_id = data.get("movie_custom_path_id")
        episode_filter = _normalize_episode_filter(data.get("episode_filter"))

        if not title or not series_url:
            return jsonify({"error": "title and series_url are required"}), 400

        existing = find_autosync_by_url(series_url)
        if existing:
            return jsonify(
                {"error": "A sync job for this series already exists", "job": existing}
            ), 409

        username, _ = _get_current_user_info()
        # Resolve path_unavailable_action: request body > global setting > "skip"
        path_action = (
            data.get("path_unavailable_action")
            or get_setting("sync_path_unavailable_action")
            or os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip")
        ).strip().lower()
        if path_action not in ("skip", "hold"):
            path_action = "skip"
        job_id = add_autosync_job(
            title=title,
            series_url=series_url,
            language=language,
            provider=provider,
            custom_path_id=custom_path_id,
            added_by=username,
            path_unavailable_action=path_action,
            episode_filter=episode_filter,
            movie_custom_path_id=movie_custom_path_id,
        )
        return jsonify({"ok": True, "id": job_id})
    @app.route("/api/autosync/site-search", methods=["POST"])
    def api_autosync_site_search():
        """Resolve a (library) title to candidate series on AniWorld / S.TO so
        it can be added to Auto-Sync. Performs the "is it actually findable on
        a site" check, and returns every match (with its source site) so the
        caller can let the user choose when more than one is found.

        Route: POST /api/autosync/site-search. Called from static/library.js's
        `libAddToAutosync()`.
        """
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400
        return jsonify({"results": find_site_candidates(title)})
    @app.route("/api/autosync/<int:job_id>", methods=["PUT"])
    def api_autosync_update(job_id):
        """Update an AutoSync job's settings (owner or admin only).

        Route: PUT /api/autosync/<job_id>. Called from static/autosync.js's
        `saveEdit()` and from static/autosync_filter.js's `openCreate()` save
        handler (edit path).
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to edit this job"}), 403
        data = request.get_json(silent=True) or {}
        allowed = {"language", "provider", "enabled", "custom_path_id",
                   "path_unavailable_action", "episode_filter", "movie_custom_path_id",
                   "group_name"}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if "group_name" in filtered:
            gn = filtered["group_name"]
            gn = (str(gn).strip() if gn is not None else "")
            filtered["group_name"] = gn or None
        filter_changed = "episode_filter" in filtered
        if filter_changed:
            filtered["episode_filter"] = _normalize_episode_filter(filtered["episode_filter"])
            # Mark for a silent baseline recompute on the next sync so the
            # "new episodes" badge is not skewed by the changed filter scope.
            filtered["filter_dirty"] = 1
        update_autosync_job(job_id, **filtered)
        # When the filter changed, kick off a background sync immediately so the
        # card counts reflect the new scope right away (and in-scope missing
        # episodes are queued).
        if filter_changed:
            fresh = get_autosync_job(job_id)
            if fresh and fresh.get("enabled"):
                with _syncing_jobs_lock:
                    _busy = job_id in _syncing_jobs
                if not _busy:
                    threading.Thread(
                        target=_run_autosync_for_job, args=(fresh,), daemon=True
                    ).start()
        return jsonify({"ok": True})
    @app.route("/api/autosync/<int:job_id>", methods=["DELETE"])
    def api_autosync_delete(job_id):
        """Delete an AutoSync job (owner or admin only).

        Route: DELETE /api/autosync/<job_id>. Called from static/autosync.js's
        `removeJob()`.
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to delete this job"}), 403
        ok, err = remove_autosync_job(job_id)
        if not ok:
            return jsonify({"error": err}), 404
        return jsonify({"ok": True})
    @app.route("/api/autosync/<int:job_id>/sync", methods=["POST"])
    def api_autosync_trigger(job_id):
        """Manually trigger a background sync run for a single AutoSync job.

        Route: POST /api/autosync/<job_id>/sync. Called from static/autosync.js's
        `syncNow()`.
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized"}), 403
        with _syncing_jobs_lock:
            if job_id in _syncing_jobs:
                return jsonify({"error": "Sync already running for this job"}), 409
        threading.Thread(target=_run_autosync_for_job, args=(job, True), daemon=True).start()
        return jsonify({"ok": True, "message": "Sync started"})
    @app.route("/api/autosync/running")
    def api_autosync_running():
        """Return the set of currently running sync job IDs.

        Route: GET /api/autosync/running. Called from static/autosync.js's
        `pollRunningJobs()` and `loadAutosyncJobs()`.
        """
        with _syncing_jobs_lock:
            return jsonify({"running": list(_syncing_jobs)})
    @app.route("/api/autosync/sync-all", methods=["POST"])
    def api_autosync_sync_all():
        """Trigger sync for all enabled jobs the current user owns (or all if admin).

        Route: POST /api/autosync/sync-all. Called from static/autosync.js's
        `syncAll()`.
        """
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs()
        started = 0
        skipped = 0
        for job in jobs:
            if not job.get("enabled"):
                continue
            if not is_admin and job.get("added_by") != username:
                continue
            job_id = job["id"]
            with _syncing_jobs_lock:
                if job_id in _syncing_jobs:
                    skipped += 1
                    continue
            threading.Thread(target=_run_autosync_for_job, args=(job,), daemon=True).start()
            started += 1
        return jsonify({"ok": True, "started": started, "skipped": skipped})
    @app.route("/api/autosync/check", methods=["GET"])
    def api_autosync_check():
        """Check if a sync job exists for a given series URL.

        Route: GET /api/autosync/check. Called from static/app.js to reflect
        AutoSync state on the series detail modal's sync button.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"exists": False})
        job = find_autosync_by_url(url)
        if not job:
            return jsonify({"exists": False})
        # Only expose job details to the owner or admins
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "job": job})
    @app.route("/api/autosync/export", methods=["GET"])
    def api_autosync_export():
        """Export all autosync jobs the current user can see as JSON.

        Route: GET /api/autosync/export. Called from static/autosync.js's
        `exportAutosync()`.
        """
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs(username=None if is_admin else username)
        # Strip runtime-only fields that make no sense on import
        export_fields = {"title", "series_url", "language", "provider", "enabled", "episode_filter"}
        clean = [{k: j[k] for k in export_fields if k in j} for j in jobs]
        payload = json.dumps({"version": 1, "jobs": clean}, ensure_ascii=False, indent=2)
        from flask import Response
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="autosync_backup.json"'},
        )
    @app.route("/api/autosync/import", methods=["POST"])
    def api_autosync_import():
        """Import autosync jobs from a JSON backup. Skips duplicates.

        Route: POST /api/autosync/import. Called from static/autosync.js's
        `importAutosync()`.
        """
        username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "Nur Admins können Jobs importieren"}), 403
        try:
            data = request.get_json(silent=True)
            if data is None:
                # try raw text body
                data = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Ungültiges JSON"}), 400

        jobs_in = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs_in, list):
            return jsonify({"error": "Erwartet: {jobs: [...]}"}), 400

        imported = 0
        skipped  = 0
        errors   = []
        for entry in jobs_in:
            title      = (entry.get("title") or "").strip()
            series_url = (entry.get("series_url") or "").strip()
            language   = entry.get("language", "German Dub")
            provider   = entry.get("provider", "VOE")
            enabled    = int(entry.get("enabled", 1))
            episode_filter = _normalize_episode_filter(entry.get("episode_filter"))
            if not title or not series_url:
                errors.append(f"Übersprungen (kein title/series_url): {entry}")
                continue
            if find_autosync_by_url(series_url):
                skipped += 1
                continue
            try:
                job_id = add_autosync_job(
                    title=title,
                    series_url=series_url,
                    language=language,
                    provider=provider,
                    added_by=username,
                    episode_filter=episode_filter,
                )
                if not enabled:
                    update_autosync_job(job_id, enabled=0)
                imported += 1
            except Exception as exc:
                errors.append(f"{title}: {exc}")
        return jsonify({"ok": True, "imported": imported, "skipped": skipped, "errors": errors})
    @app.route("/api/autosync/batch", methods=["POST"])
    def api_autosync_batch():
        """Batch-update multiple autosync jobs at once.

        Route: POST /api/autosync/batch. Called from static/autosync.js's
        `batchAction()`.

        Body: { ids: [int, ...], action: "enable"|"disable"|"set_path", custom_path_id: int|null }
        """
        username, is_admin = _get_current_user_info()
        data   = request.get_json(silent=True) or {}
        ids    = data.get("ids", [])
        action = data.get("action", "")
        if not ids or action not in ("enable", "disable", "set_path", "delete",
                                     "set_group", "remove_group"):
            return jsonify({"error": "ids und action (enable|disable|set_path|delete|set_group|remove_group) erforderlich"}), 400

   