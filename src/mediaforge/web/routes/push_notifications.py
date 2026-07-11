"""Push / notification routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.push_notifications (usage counter) -- see
# telemetry/registry.py. Registry-only for now.
"""

from ..db import delete_setting
from ..db import get_setting
from ..db import get_user_notif_prefs_all
from ..db import set_setting
from ..db import set_user_notif_prefs_bulk
from flask import jsonify
from flask import render_template
from flask import request


def register_push_notifications_routes(app):
    """Register Web Push subscription routes and the notification-settings
    (per-user + admin) routes on the given Flask app."""
    @app.route("/api/push/vapid-public-key")
    def api_vapid_public_key():
        """Return the server's VAPID public key for Web Push subscription.

        Route: GET /api/push/vapid-public-key. Called from static/pwa.js's
        service-worker subscription flow.
        """
        from ..notifications import _ensure_vapid_keys
        _, key, _ = _ensure_vapid_keys()
        return jsonify({"vapid_public_key": key})
    @app.route("/api/push/subscribe", methods=["POST"])
    def api_push_subscribe():
        """Store a browser's Web Push subscription for the current user.

        Route: POST /api/push/subscribe. Called from static/pwa.js's push
        subscription setup.
        """
        from flask import session as _sess
        from ..notifications import add_push_subscription
        sub = request.get_json(silent=True)
        if not sub or "endpoint" not in sub:
            return jsonify({"error": "invalid subscription"}), 400
        add_push_subscription(sub, user_id=_sess.get("user_id"))
        return jsonify({"ok": True})
    @app.route("/api/push/unsubscribe", methods=["POST"])
    def api_push_unsubscribe():
        """Remove a browser's Web Push subscription by endpoint.

        Route: POST /api/push/unsubscribe. Called from static/pwa.js's push
        unsubscribe flow.
        """
        from ..notifications import remove_push_subscription
        data = request.get_json(silent=True)
        if not data or "endpoint" not in data:
            return jsonify({"error": "missing endpoint"}), 400
        remove_push_subscription(data["endpoint"])
        return jsonify({"ok": True})
    @app.route("/api/notif/settings")
    def api_notif_settings_get():
        """Return notification settings for the current user.

        Route: GET /api/notif/settings. Called from the inline script in
        templates/notifications.html (page load).

        Admins receive the real global admin settings values (the UI masks
        secrets client-side via password-type inputs with a reveal button).
        Non-admins only get booleans for the admin settings (configured or
        not) plus their own per-user settings and event prefs.
        """
        from flask import session as _sess
        uid  = _sess.get("user_id")
        role = _sess.get("user_role", "user")

        # Per-user prefs: uid=0 is the no-auth admin pseudo-user
        user_prefs = get_user_notif_prefs_all(uid) if uid is not None else {}

        result = {"user_prefs": user_prefs, "is_admin": role == "admin"}

        # Global admin settings — admins get the real values, users see booleans only
        admin_keys = [
            "notif_telegram_bot_token",
            "notif_telegram_enabled",
            "notif_pushover_app_token",
            "notif_pushover_enabled",
            "notif_discord_webhook_url",
            "notif_discord_enabled",
            "notif_discord_on_completed",
            "notif_discord_on_errors",
            "notif_discord_on_partial",
            "notif_discord_on_cancelled",
            "notif_discord_on_autosync",
            "notif_discord_on_sync_error",
            "notif_discord_on_sync_hold",
            "notif_discord_on_disk_space_low",
            "notif_disk_space_min_gb",
            "notif_sync_error_only_failed_all",
            "notif_whatsapp_sid",
            "notif_whatsapp_auth_token",
            "notif_whatsapp_from",
            "notif_whatsapp_enabled",
            "notif_webpush_enabled",
            "notif_ntfy_server",
            "notif_ntfy_topic",
            "notif_ntfy_auth_token",
            "notif_ntfy_user",
            "notif_ntfy_password",
            "notif_ntfy_enabled",
        ]
        # Keys that store a "0"/"1" boolean flag rather than a secret/value.
        # These need explicit "0" => False handling for non-admins below —
        # bool(raw) would be wrong here because bool("0") is True in Python
        # (any non-empty string is truthy), which made a disabled toggle
        # still report as "on" to non-admin viewers.
        boolean_keys = {
            "notif_telegram_enabled", "notif_pushover_enabled",
            "notif_discord_enabled", "notif_discord_on_completed",
            "notif_discord_on_errors", "notif_discord_on_partial",
            "notif_discord_on_cancelled", "notif_discord_on_autosync",
            "notif_discord_on_sync_error", "notif_discord_on_sync_hold",
            "notif_discord_on_disk_space_low", "notif_sync_error_only_failed_all",
            "notif_whatsapp_enabled", "notif_webpush_enabled", "notif_ntfy_enabled",
        }
        admin_data = {}
        for k in admin_keys:
            raw = get_setting(k) or ""
            if role == "admin":
                # Return the real value to admins — they're already authenticated.
                # Sensitive inputs are shown as type=password in the UI (dots),
                # with an explicit eye-button to reveal. No need to mask here.
                admin_data[k] = raw
            elif k in boolean_keys:
                # Non-admins get the real on/off state for toggles.
                admin_data[k] = raw not in ("", "0", "false")
            else:
                # Non-admins only need to know if the service is configured
                # (secret/value keys — empty means not set up).
                admin_data[k] = bool(raw)
        result["admin"] = admin_data
        return jsonify(result)
    @app.route("/api/notif/admin-settings", methods=["POST"])
    def api_notif_admin_settings_set():
        """Update global admin notification settings. Admin only.

        Route: POST /api/notif/admin-settings. Called from the inline script
        in templates/notifications.html (admin settings form handlers).
        """
        from flask import session as _sess
        if _sess.get("user_role") != "admin":
            return jsonify({"error": "admin access required"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid payload"}), 400

        allowed = {
            "notif_telegram_bot_token",
            "notif_telegram_enabled",
            "notif_pushover_app_token",
            "notif_pushover_enabled",
            "notif_discord_webhook_url",
            "notif_discord_enabled",
            "notif_discord_on_completed",
            "notif_discord_on_errors",
            "notif_discord_on_partial",
            "notif_discord_on_cancelled",
            "notif_discord_on_autosync",
            "notif_discord_on_sync_error",
            "notif_discord_on_sync_hold",
            "notif_discord_on_disk_space_low",
            "notif_disk_space_min_gb",
            "notif_sync_error_only_failed_all",
            "notif_whatsapp_sid",
            "notif_whatsapp_auth_token",
            "notif_whatsapp_from",
            "notif_whatsapp_enabled",
            "notif_webpush_enabled",
            "notif_ntfy_server",
            "notif_ntfy_topic",
            "notif_ntfy_auth_token",
            "notif_ntfy_user",
            "notif_ntfy_password",
            "notif_ntfy_enabled",
        }
        for k, v in data.items():
            if k not in allowed:
                continue
            val = str(v).strip()
            if val == "":
                delete_setting(k)
            else:
                set_setting(k, val)

        return jsonify({"ok": True})
    @app.route("/api/notif/user-settings", methods=["POST"])
    def api_notif_user_settings_set():
        """Update per-user notification settings (chat_id, user_key, phone, event prefs).

        Route: POST /api/notif/user-settings. Called from the inline script
        in templates/notifications.html (per-user settings form handlers).
        """
        from flask import session as _sess
        # uid=0 is the synthetic no-auth admin user
        uid = _sess.get("user_id")
        if uid is None:
            return jsonify({"error": "not authenticated"}), 401

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid payload"}), 400

        allowed_keys = {
            # Web Push prefs
            "webpush_on_completed", "webpush_on_errors",
            "webpush_on_partial", "webpush_on_cancelled", "webpush_on_autosync",
            "webpush_on_sync_error", "webpush_on_sync_hold", "webpush_on_disk_space_low",
            # Telegram
            "telegram_enabled",
            "telegram_chat_id",
            "telegram_on_completed", "telegram_on_errors",
            "telegram_on_partial", "telegram_on_cancelled", "telegram_on_autosync",
            "telegram_on_sync_error", "telegram_on_sync_hold", "telegram_on_disk_space_low",
            # Pushover
            "pushover_enabled",
            "pushover_user_key",
            "pushover_on_completed", "pushover_on_errors",
            "pushover_on_partial", "pushover_on_cancelled", "pushover_on_autosync",
            "pushover_on_sync_error", "pushover_on_sync_hold", "pushover_on_disk_space_low",
            # WhatsApp
            "whatsapp_enabled",
            "whatsapp_phone",
            "whatsapp_on_completed", "whatsapp_on_errors",
            "whatsapp_on_partial", "whatsapp_on_cancelled", "whatsapp_on_autosync",
            "whatsapp_on_sync_error", "whatsapp_on_sync_hold", "whatsapp_on_disk_space_low",
            # NTFY
            "ntfy_enabled",
            "ntfy_on_completed", "ntfy_on_errors",
            "ntfy_on_partial", "ntfy_on_cancelled", "ntfy_on_autosync",
            "ntfy_on_sync_error", "ntfy_on_sync_hold", "ntfy_on_disk_space_low",
        }
        filtered = {k: str(v) for k, v in data.items() if k in allowed_keys}
        set_user_notif_prefs_bulk(uid, filtered)
        return jsonify({"ok": True})
    @app.route("/api/notif/telegram/detect-chat-id")
    def api_telegram_detect_chat_id():
        """Call Telegram getUpdates and return the most recent chat_id.

        Route: GET /api/notif/telegram/detect-chat-id. Called from
        templates/notifications.html's inline script (auto-detect chat ID button).
        """
        from flask import session as _sess
        # Both admins and users can trigger this (bot token must be set by admin)
        from ..notifications import telegram_detect_chat_id
        token = get_setting("notif_telegram_bot_token") or ""
        if not token:
            return jsonify({"error": "Bot-Token wurde noch nicht konfiguriert"}), 400
        chat_id = telegram_detect_chat_id(token)
        if chat_id is None:
            return jsonify({"error": "Keine Nachricht gefunden. Schreib dem Bot zuerst eine Nachricht."}), 404
        return jsonify({"chat_id": chat_id})
    @app.route("/api/notif/test", methods=["POST"])
    def api_notif_test():
        """Send a test notification via the requested service.

        Route: POST /api/notif/test. Called from templates/notifications.html's
        inline script (per-service "Send test" buttons).
        """
        from flask import session as _sess
        uid      = _sess.get("user_id")
        # In no-auth mode username="admin" is the pseudo-user for per-user pref lookups
        username = _sess.get("user_name")
        data     = request.get_json(silent=True) or {}
        service  = data.get("service", "")

        if service == "webpush":
            from ..notifications import notify_webpush
            notify_webpush("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "telegram":
            from ..notifications import notify_telegram
            notify_telegram("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "pushover":
            from ..notifications import notify_pushover
            notify_pushover("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "whatsapp":
            from ..notifications import notify_whatsapp
            notify_whatsapp("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "ntfy":
            from ..notifications import notify_ntfy
            notify_ntfy("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "discord":
            if _sess.get("user_role") != "admin":
                return jsonify({"error": "admin access required"}), 403
            from ..notifications import send_discord_sync
            from ..db import get_setting as _gs
            import os as _os
            _wh_url = (_gs("notif_discord_webhook_url") or _os.environ.get("MEDIAFORGE_DISCORD_WEBHOOK", "")).strip()
            if not _wh_url:
                return jsonify({"error": "Kein Webhook konfiguriert"}), 400
            import json as _json
            _payload = {
                "embeds": [{
                    "title": "MediaForge — Test",
                    "color": 0x57F287,
                    "fields": [
                        {"name": "Status", "value": "Test erfolgreich ✅", "inline": True},
                    ],
                    "footer": {"text": "MediaForge"},
                }]
            }
            _code, _err = send_discord_sync(_wh_url, _payload)
            if _code in (200, 204):
                return jsonify({"ok": True, "http": _code})
            else:
                return jsonify({"error": f"Discord antwortete HTTP {_code}: {_err or ''}"}), 502
        else:
            return jsonify({"error": "unknown service"}), 400

        return jsonify({"ok": True})
    @app.route("/notifications")
    def notifications_page():
        """Render the notification settings page. Route: GET /notifications."""
        from flask import session as _sess
        # In no-auth mode user_role is set to "admin" by before_request,
        # so this works correctly for both auth and no-auth.
        return render_template(
            "notifications.html",
            is_admin=(_sess.get("user_role") == "admin"),
        )
