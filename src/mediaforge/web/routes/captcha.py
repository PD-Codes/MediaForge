"""Captcha solve routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

detail.captcha (solve timeout/error) is already wired at the actual solve
call sites in playwright/captcha.py (_solve_captcha_cli/_interactive,
solve_sto_modal) -- this module only forwards screenshot/click/status
polling for an already-open interactive session, so there is no separate
success/failure point to add here. flag.captcha (usage counter) is
intentionally NOT wired -- out of scope for now, see telemetry/registry.py.
"""

from ..db import get_queue_item
from flask import jsonify
from flask import request


def _captcha_access_allowed(queue_id):
    """Return True if the current session may interact with this captcha."""
    from flask import session as _sess
    if _sess.get("user_role") == "admin":
        return True
    item = get_queue_item(queue_id)
    if not item:
        return False
    return item.get("username") == _sess.get("user_name")


def register_captcha_routes(app):
    """Register the captcha-solving endpoints used by the queue's captcha modal."""
    @app.route("/api/captcha/<int:queue_id>/screenshot")
    def api_captcha_screenshot(queue_id):
        """Stream the latest captcha screenshot for a running queue item.

        GET /api/captcha/<queue_id>/screenshot. Polled every 800ms by
        queue.js's openCaptchaModal() (captchaRefreshTimer) to refresh the
        screenshot shown in the captcha modal.
        """
        if not _captcha_access_allowed(queue_id):
            return "", 403
        from ...playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            captcha_sess = _captcha_mod._active_sessions.get(queue_id)
        if captcha_sess is None:
            return "", 204
        data = captcha_sess.get_screenshot()
        if not data:
            return "", 204
        from flask import Response
        return Response(data, mimetype="image/jpeg")
    @app.route("/api/captcha/<int:queue_id>/click", methods=["POST"])
    def api_captcha_click(queue_id):
        """Forward a click coordinate to the captcha browser.

        POST /api/captcha/<queue_id>/click. Called from queue.js's
        attachCaptchaClickHandler() when the user clicks on the captcha
        screenshot image.
        """
        if not _captcha_access_allowed(queue_id):
            return jsonify({"error": "Forbidden"}), 403
        from ...playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            captcha_sess = _captcha_mod._active_sessions.get(queue_id)
        if captcha_sess is None:
            return jsonify({"error": "No active captcha session"}), 404
        data = request.get_json(silent=True) or {}
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        captcha_sess.enqueue_click(x, y)
        return jsonify({"ok": True})
    @app.route("/api/captcha/<int:queue_id>/status")
    def api_captcha_status(queue_id):
        """Return whether a captcha session is active for the given queue item.

        GET /api/captcha/<queue_id>/status. Polled every 1500ms by queue.js's
        openCaptchaModal() (captchaStatusTimer); once the session is no
        longer active it closes the modal and refreshes the queue.
        """
        if not _captcha_access_allowed(queue_id):
            return jsonify({"error": "Forbidden"}), 403
        from ...playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            active = queue_id in _captcha_mod._active_sessions
        return jsonify({"active": active})
