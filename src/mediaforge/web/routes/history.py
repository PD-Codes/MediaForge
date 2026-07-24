"""Download history routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import add_to_queue
from ..db import clear_download_history
from ..db import delete_download_history_entries
from ..db import delete_download_history_entry
from ..db import get_download_history
from ..db import get_download_history_entry
from ..language_groups import language_display
from ..queue_worker import _ensure_queue_worker
from flask import jsonify
from flask import render_template
from flask import request
import json
from ..request_context import get_current_user_info as _get_current_user_info


def _history_since_from_range(rng):
    """Map a date-range key (1d/7d/30d/all) to a UTC cutoff string, or None."""
    from datetime import datetime, timedelta
    days = {"1d": 1, "7d": 7, "30d": 30}.get((rng or "all").strip())
    if not days:
        return None
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _history_filters():
    """Read the shared search/status/source/range query params used by the history list and export/clear endpoints."""
    search = (request.args.get("search") or "").strip() or None
    status = (request.args.get("status") or "all").strip()
    source = (request.args.get("source") or "all").strip()
    since = _history_since_from_range(request.args.get("range"))
    return search, status, source, since


def register_history_routes(app):
    """Register the history page and its list/retry/delete/export/clear API endpoints."""
    @app.route("/history")
    def history_page():
        """Render the download history page shell (data is loaded client-side).

        GET /history.
        """
        return render_template("history.html")
    @app.route("/api/history")
    def api_history_list():
        """Return a paginated, filtered page of download history entries.

        GET /api/history. Called from history.js's fetchPage().
        """
        username, is_admin = _get_current_user_info()
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        search, status, source, since = _history_filters()
        entries, total = get_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
            limit=limit, offset=offset,
        )
        # Entries whose episode never got a resolved language (a cancelled
        # remainder, say) still hold the "group:<id>" reference the retry needs.
        for entry in entries:
            entry["language_label"] = language_display(entry.get("language"))
        return jsonify({"entries": entries, "total": total, "limit": limit, "offset": offset})
    @app.route("/api/history/<int:entry_id>/retry", methods=["POST"])
    def api_history_retry(entry_id):
        """Re-queue a single failed or cancelled history entry's episode.

        POST /api/history/<entry_id>/retry. Called from history.js's
        retryEntry().
        """
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        if entry.get("status") not in ("failed", "cancelled"):
            return jsonify({"error": "Only failed or cancelled downloads can be retried"}), 400
        ep_url = entry.get("episode_url")
        if not ep_url:
            return jsonify({"error": "No episode URL stored for this entry"}), 400
        add_to_queue(
            title=entry.get("title") or "",
            series_url=entry.get("series_url") or ep_url,
            episodes=[ep_url],
            language=entry.get("language") or "German Dub",
            provider=entry.get("provider") or "VOE",
            username=entry.get("username"),
            source=entry.get("source") or "manual",
        )
        _ensure_queue_worker()
        return jsonify({"ok": True})
    @app.route("/api/history/delete", methods=["POST"])
    def api_history_bulk_delete():
        """Delete multiple history entries by id in one request.

        POST /api/history/delete. Called from history.js's bulkDelete().
        """
        username, is_admin = _get_current_user_info()
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids required"}), 400
        deleted = delete_download_history_entries(
            ids, username=None if is_admin else username
        )
        return jsonify({"ok": True, "deleted": deleted})
    @app.route("/api/history/export")
    def api_history_export():
        """Export the (filtered) download history as a CSV or JSON file download.

        GET /api/history/export. Called from history.js's exportHistory()
        via a direct navigation (window.location.href), not fetch, so the
        browser handles the file download.
        """
        import csv as _csv, io as _io
        username, is_admin = _get_current_user_info()
        search, status, source, since = _history_filters()
        fmt = (request.args.get("format") or "csv").strip().lower()
        entries, _ = get_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
            limit=1000000, offset=0,
        )
        from flask import Response
        if fmt == "json":
            payload = json.dumps({"entries": entries}, ensure_ascii=False, indent=2)
            return Response(payload, mimetype="application/json",
                            headers={"Content-Disposition": 'attachment; filename="download_history.json"'})
        cols = ["title", "season", "episode", "status", "error", "language",
                "provider", "source", "size_mb", "avg_speed_mbps", "duration_sec",
                "started_at", "finished_at", "target_path", "episode_url"]
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(cols)
        for e in entries:
            w.writerow([e.get(c, "") for c in cols])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="download_history.csv"'})
    @app.route("/api/history/<int:entry_id>")
    def api_history_get(entry_id):
        """Return a single history entry's full detail.

        GET /api/history/<entry_id>. Called from history.js's
        openDetail().
        """
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        return jsonify({"entry": entry})
    @app.route("/api/history/<int:entry_id>", methods=["DELETE"])
    def api_history_delete(entry_id):
        """Delete a single history entry.

        DELETE /api/history/<entry_id>. Called from history.js's
        deleteEntry().
        """
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        delete_download_history_entry(entry_id)
        return jsonify({"ok": True})
    @app.route("/api/history/clear", methods=["POST"])
    def api_history_clear():
        """Clear history entries matching the given filters (or all, with no filters).

        POST /api/history/clear. Called from history.js's clearAll().
        """
        username, is_admin = _get_current_user_info()
        # Honour active filters so the user can clear just the current view
        # (e.g. only failed, or only the last 7 days). No filters = clear all.
        data = request.get_json(silent=True) or {}
        search = (data.get("search") or "").strip() or None
        status = (data.get("status") or "all").strip()
        source = (data.get("source") or "all").strip()
        since = _history_since_from_range(data.get("range"))
        deleted = clear_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
        )
        return jsonify({"ok": True, "deleted": deleted})
