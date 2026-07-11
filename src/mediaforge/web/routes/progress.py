"""Watch-progress routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import get_download_history_meta_for_path
from ..db import get_watch_progress
from ..db import get_watch_progress_bulk
from ..db import save_watch_progress
from flask import jsonify
from flask import request
from ..request_context import get_current_user_info as _get_current_user_info
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events

# Consider a title "completed" (watch.completion, stage 6) once playback
# reaches this fraction -- matches the common "credits/outro" convention
# other media players use, since very few plays reach a literal 100%.
_COMPLETION_THRESHOLD = 0.9


def register_progress_routes(app):
    """Register the watch-progress save/get/bulk-get endpoints used by the player."""
    @app.route("/api/progress/save", methods=["POST"])
    def api_progress_save():
        """Save the current playback position for an episode.

        POST /api/progress/save. Called from player.js's _saveProgress().
        """
        data     = request.get_json(force=True, silent=True) or {}
        path     = data.get("path", "")
        position = float(data.get("position", 0) or 0)
        duration = float(data.get("duration", 0) or 0)
        if not path:
            return jsonify({"error": "path required"}), 400
        _user, _ = _get_current_user_info()
        save_watch_progress(path, position, duration, username=_user)

        # Telemetry (stage 6, off by default) -- best-effort provider/title
        # lookup via download_history since the player only ever sends a
        # local file path, never provider metadata. A lookup miss (file not
        # in download history, e.g. manually placed in the library) means
        # provider is None, which is NOT the same as "known safe" -- but
        # is_adult_provider() only ever matches the literal "hanime_tv", so
        # an unknown provider here is simply never sent as hanime_tv and the
        # event proceeds normally. "watch_seconds" is the raw playback
        # position, used as a best-effort proxy for watch time in this pass
        # -- true cumulative watched-time tracking (accounting for skips/
        # rewatches) is not implemented.
        try:
            _meta = get_download_history_meta_for_path(path) or {}
            _progress_percent = int(round((position / duration) * 100)) if duration > 0 else None
            _completed = (duration > 0 and position / duration >= _COMPLETION_THRESHOLD) if duration > 0 else None
            telemetry_client.submit_all(telemetry_events.build_watch_event(
                provider=_meta.get("provider"),
                media_type="movie" if _meta.get("season") is None else "series",
                title=_meta.get("title"),
                season=_meta.get("season"), episode=_meta.get("episode"),
                watch_seconds=int(position) if position else None,
                progress_percent=_progress_percent,
                completed=_completed,
            ))
        except Exception:
            pass  # telemetry must never break saving progress
        return jsonify({"ok": True})
    @app.route("/api/progress/get")
    def api_progress_get():
        """Return the saved playback position for a single episode path.

        GET /api/progress/get. Called from app.js's streamEpisode() to
        resume playback from where the user left off.
        """
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        _user, _ = _get_current_user_info()
        return jsonify(get_watch_progress(path, username=_user))
    @app.route("/api/progress/bulk", methods=["POST"])
    def api_progress_bulk():
        """Return saved playback positions for multiple episode paths at once.

        POST /api/progress/bulk. Called from library.js's
        _libFlushProgress() to annotate the library listing with
        "continue watching" progress in one request.
        """
        data  = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return jsonify({"error": "paths must be list"}), 400
        _user, _ = _get_current_user_info()
        return jsonify(get_watch_progress_bulk(paths, username=_user))
