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
import os


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
        from ..db import get_general_stats

        items = get_queue()
        ffmpeg_pct = get_ffmpeg_progress()
        # Items using a fallback group store the internal "group:<id>"; the
        # queue rows show the group's name instead.
        for _it in items:
            _it["language_label"] = language_display(_it.get("language"))

        return jsonify({
            "items": items,
            "ffmpeg_progress": ffmpeg_pct,
            "paused": is_queue_paused()
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
        ok, err = cancel_queue_item(queue_id)
        if not ok:
            return jsonify({"error": err}), 400
        # Signal the worker to kill the active subprocess immediately.
        with _active_cancel_events_lock:
            ev = _active_cancel_events.get(queue_id)
        if ev is not None:
            ev.set()
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
    @app.route("/api/queue/completed", methods=["DELETE"])
    def api_queue_clear():
        """Remove all completed items from the queue.

        DELETE /api/queue/completed. Called from queue.js's
        clearOldQueueItems().
        """
        clear_completed()
        return jsonify({"ok": True})
