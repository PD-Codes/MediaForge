"""Full & Selective Backup routes (admin only).

Exposes the export/preview/import endpoints backing the "Backup" settings tab.
Every endpoint is gated on the current request being an admin (auth disabled =>
treated as admin, matching the rest of the settings API).

Uploads are sent as a JSON body ({"file": "<backup json text>", ...}) rather
than multipart/form-data: every /api/ POST in this app must be
application/json (see app.py's _enforce_json_content_type, which stands in for
the CSRF token on /api/ routes), so a JSON envelope is the only upload shape
that passes -- the same approach routes/autosync.py's import uses.

Extracted as a plain route-registration function (no Flask blueprint: endpoint
names stay bare so url_for() keeps working).
"""

import io
import time

from flask import jsonify
from flask import request
from flask import send_file

from .. import backup as _backup
from ..backup import BackupError
from ..request_context import get_current_user_info as _get_current_user_info
from ...logger import get_logger

logger = get_logger(__name__)

# Safety cap on an uploaded backup so a malicious/oversized file cannot exhaust
# memory when parsed. Backups are settings + user data (no media), so this is
# generous.
_MAX_BACKUP_BYTES = 64 * 1024 * 1024


def _require_admin():
    """Return an error response tuple if the caller is not an admin, else None."""
    _user, is_admin = _get_current_user_info()
    if not is_admin:
        return jsonify({"error": "admin access required"}), 403
    return None


def _read_upload(payload):
    """Validate and return the uploaded backup text, or an (error, status) tuple.

    Returns a ``str`` on success or a Flask response tuple on failure.
    """
    file_text = payload.get("file") or ""
    if not file_text:
        return jsonify({"error": "no backup file provided"}), 400
    if len(file_text.encode("utf-8")) > _MAX_BACKUP_BYTES:
        return jsonify({"error": "backup file too large"}), 413
    return file_text


def register_backup_routes(app):
    """Register the /api/backup/* endpoints on *app*."""

    @app.route("/api/backup/categories", methods=["GET"])
    def api_backup_categories():
        """List available backup categories with current row counts."""
        denied = _require_admin()
        if denied:
            return denied
        return jsonify({"categories": _backup.list_categories()})

    @app.route("/api/backup/export", methods=["POST"])
    def api_backup_export():
        """Create a backup and return it as a downloadable .mfbackup file."""
        denied = _require_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        categories = payload.get("categories") or []
        password = payload.get("password") or ""
        no_password = bool(payload.get("no_password"))
        if not password and not no_password:
            return jsonify({"error": "password required"}), 400
        try:
            blob = _backup.export_backup(categories, password, allow_no_password=no_password)
        except BackupError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("Backup export failed")
            return jsonify({"error": "export failed"}), 500

        filename = f"mediaforge-backup-{time.strftime('%Y%m%d-%H%M%S')}.mfbackup"
        return send_file(
            io.BytesIO(blob),
            mimetype="application/json",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/backup/preview", methods=["POST"])
    def api_backup_preview():
        """Inspect an uploaded backup (JSON body) without importing anything."""
        denied = _require_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        file_text = _read_upload(payload)
        if not isinstance(file_text, str):
            return file_text  # error response tuple
        try:
            info = _backup.preview_backup(file_text.encode("utf-8"), payload.get("password") or "")
        except BackupError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("Backup preview failed")
            return jsonify({"error": "could not read backup"}), 500
        return jsonify(info)

    @app.route("/api/backup/import", methods=["POST"])
    def api_backup_import():
        """Restore selected categories from an uploaded backup (JSON body)."""
        denied = _require_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        file_text = _read_upload(payload)
        if not isinstance(file_text, str):
            return file_text  # error response tuple
        # Password may be empty for an unencrypted backup; import_backup()
        # raises BackupError if the backup actually needs one.
        password = payload.get("password") or ""
        mode = payload.get("mode", "merge")
        categories = payload.get("categories") or []
        try:
            report = _backup.import_backup(file_text.encode("utf-8"), password, categories, mode)
        except BackupError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("Backup import failed")
            return jsonify({"error": "import failed"}), 500
        return jsonify({"ok": True, "imported": report})
