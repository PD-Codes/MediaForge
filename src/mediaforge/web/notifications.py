"""Notification helpers — Web Push (VAPID), Telegram, Pushover, Discord Webhook, WhatsApp.

Event keys (used across all services):
  - "on_completed"   — queue item finished without errors
  - "on_errors"      — queue item finished with errors
  - "on_cancelled"   — queue item was cancelled
  - "on_autosync"    — auto-sync found new episodes for a series
  - "on_sync_hold"   — auto-sync paused because custom path is unavailable
  - "on_sync_resume" — auto-sync resumed because custom path is accessible again

Storage:
  - Global / admin settings  -> app_settings table (prefixed with "notif_")
  - Per-user settings/prefs  -> user_notification_prefs table
  - Push subscriptions       -> push_subscriptions table
  - VAPID keys               -> ~/.mediaforge/vapid_keys.json  (auto-generated)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
import urllib.parse

logger = logging.getLogger("mediaforge")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pref_enabled(prefs: dict, key: str, default: bool = True) -> bool:
    val = prefs.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() not in ("0", "false", "no", "")


def _get_setting(key: str, default: str = "") -> str:
    from .db import get_setting
    return get_setting(key) or default


def _get_user_prefs(username: str | None) -> dict:
    from .db import get_user_id_by_username, get_user_notif_prefs_all
    if not username:
        # No-auth mode: uid=0 is the synthetic admin pseudo-user whose
        # prefs are saved from the notification settings UI.
        try:
            return get_user_notif_prefs_all(0)
        except Exception:
            return {}
    uid = get_user_id_by_username(username)
    if uid is None:
        try:
            return get_user_notif_prefs_all(0)
        except Exception:
            return {}
    return get_user_notif_prefs_all(uid)



def _format_errors_text(errors: list, max_items: int = 5) -> str:
    """Format a list of {url, error} dicts into a compact human-readable string."""
    if not errors:
        return ""
    lines = []
    for e in errors[:max_items]:
        ep = (e.get("url") or "?").rstrip("/").split("/")[-1]
        err = (e.get("error") or "?")[:100]
        lines.append(f"  • {ep}: {err}")
    text = "\n".join(lines)
    if len(errors) > max_items:
        text += f"\n  ... und {len(errors) - max_items} weitere"
    return text

def _post_json(url: str, payload: dict, headers: dict | None = None) -> int:
    data = json.dumps(payload).encode("utf-8")
    h = {
        "Content-Type": "application/json",
        "User-Agent": "MediaForge/1.0 (https://github.com/PD-Codes/MediaForge)",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        logger.warning("[Notif] HTTP POST to %s failed: %s", url, exc)
        return 0


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# VAPID key management (file-based — auto-generated infrastructure)
# ---------------------------------------------------------------------------

def _vapid_keys_file():
    import pathlib
    return pathlib.Path.home() / ".mediaforge" / "vapid_keys.json"


def _ensure_vapid_keys() -> tuple:
    import base64
    priv_env = os.environ.get("MEDIAFORGE_VAPID_PRIVATE_KEY", "").strip()
    pub_env  = os.environ.get("MEDIAFORGE_VAPID_PUBLIC_KEY", "").strip()
    email    = os.environ.get("MEDIAFORGE_VAPID_CLAIMS_EMAIL", "").strip()
    if priv_env and pub_env:
        return priv_env, pub_env, email or "push@mediaforge.local"

    keys_file = _vapid_keys_file()
    if keys_file.exists():
        try:
            data = json.loads(keys_file.read_text())
            priv = data.get("private_key", "")
            if priv and not priv.startswith("-----"):
                return priv, data["public_key"], data.get("email", "push@mediaforge.local")
            keys_file.unlink(missing_ok=True)
        except Exception:
            keys_file.unlink(missing_ok=True)

    try:
        from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        priv_key  = generate_private_key(SECP256R1())
        priv_raw  = priv_key.private_numbers().private_value.to_bytes(32, "big")
        priv_b64  = base64.urlsafe_b64encode(priv_raw).decode().rstrip("=")
        pub_bytes = priv_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        pub_b64   = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")

        auto_email = "push@mediaforge.local"
        keys_file.parent.mkdir(parents=True, exist_ok=True)
        keys_file.write_text(json.dumps(
            {"private_key": priv_b64, "public_key": pub_b64, "email": auto_email}, indent=2
        ))
        logger.info("[WebPush] Auto-generated VAPID keys -> %s", keys_file)
        return priv_b64, pub_b64, auto_email
    except Exception as exc:
        logger.debug("[WebPush] Could not generate VAPID keys: %s", exc)
        return None, None, ""


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------

def add_push_subscription(sub: dict, user_id: int | None = None) -> None:
    from .db import db_add_push_subscription
    ep     = sub.get("endpoint", "")
    auth   = (sub.get("keys") or {}).get("auth", "")
    p256dh = (sub.get("keys") or {}).get("p256dh", "")
    if not (ep and auth and p256dh):
        return
    db_add_push_subscription(ep, auth, p256dh, user_id)
    logger.debug("[WebPush] Subscription stored (user_id=%s)", user_id)


def remove_push_subscription(endpoint: str) -> None:
    from .db import db_remove_push_subscription
    db_remove_push_subscription(endpoint)
    logger.debug("[WebPush] Subscription removed: %s", endpoint[:60])


def get_push_subscriptions(user_id: int | None = None) -> list:
    from .db import db_get_push_subscriptions
    return db_get_push_subscriptions(user_id)


def notify_webpush(
    title: str,
    body: str,
    event: str | None = None,
    username: str | None = None,
) -> None:
    if _get_setting("notif_webpush_enabled", "1") == "0":
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    priv_key, pub_key, email = _ensure_vapid_keys()
    if not priv_key:
        return

    from .db import get_user_id_by_username, get_user_notif_prefs_all
    if username:
        uid = get_user_id_by_username(username)
    else:
        uid = 0  # no-auth mode: uid=0 is the synthetic admin pseudo-user

    if uid is None:
        # Unknown username — broadcast to all, no pref check
        subs = get_push_subscriptions()
    else:
        if event:
            prefs = get_user_notif_prefs_all(uid)
            _pref_event = "on_sync_hold" if event == "on_sync_resume" else event
            if not _pref_enabled(prefs, "webpush_" + _pref_event):
                return
        subs = get_push_subscriptions(user_id=uid)
        # If user has no personal subscriptions, broadcast to all
        if not subs:
            subs = get_push_subscriptions()

    if not subs:
        return

    data = json.dumps({"title": title, "body": body})

    def _send_all():
        dead = []
        for sub in subs:
            sub_info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
            try:
                resp = webpush(
                    subscription_info=sub_info,
                    data=data,
                    vapid_private_key=priv_key,
                    vapid_claims={"sub": "mailto:" + email},
                )
                if resp and resp.status_code == 410:
                    dead.append(sub["endpoint"])
            except WebPushException as exc:
                if "410" in str(exc):
                    dead.append(sub["endpoint"])
                else:
                    logger.warning("[WebPush] Send failed: %s", exc)
            except Exception as exc:
                logger.warning("[WebPush] Send failed: %s", exc)
        for ep in dead:
            remove_push_subscription(ep)

    threading.Thread(target=_send_all, daemon=True).start()


# ---------------------------------------------------------------------------
# Discord Webhook
# ---------------------------------------------------------------------------

def notify_discord(title: str, status: str, episode_count: int, errors: list, is_movie: bool = False) -> None:
    if _get_setting("notif_discord_enabled", "1") == "0":
        return
    webhook_url = (
        _get_setting("notif_discord_webhook_url")
        or os.environ.get("MEDIAFORGE_DISCORD_WEBHOOK", "")
    ).strip()
    if not webhook_url:
        return
    if status == "completed" and not errors:
        event_key   = "on_completed"
        color       = 0x57F287
        status_text = "Erfolgreich abgeschlossen"
    elif status == "completed" and errors:
        event_key   = "on_errors"
        color       = 0xFEE75C
        status_text = "Abgeschlossen mit " + str(len(errors)) + " Fehler(n)"
    elif status == "partial":
        event_key   = "on_partial"
        color       = 0xE67E22
        status_text = "Teilweise erfolgreich"
    elif status == "failed":
        event_key   = "on_errors"
        color       = 0xED4245
        status_text = "Download fehlgeschlagen"
    elif status == "cancelled":
        event_key   = "on_cancelled"
        color       = 0x95A5A6
        status_text = "Download abgebrochen"
    else:
        return

    if _get_setting("notif_discord_" + event_key, "1") == "0":
        return

    count_label = "Film" if is_movie else "Episoden"
    fields = [
        {"name": count_label, "value": str(episode_count), "inline": True},
        {"name": "Status",    "value": status_text,        "inline": True},
    ]
    if errors:
        lines = []
        for e in errors[:5]:
            url_part = e.get("url", "?").split("/")[-1]
            err_part = e.get("error", "?")[:80]
            lines.append("- " + url_part + ": " + err_part)
        error_text = "\n".join(lines)
        if len(errors) > 5:
            error_text += "\n... und " + str(len(errors) - 5) + " weitere"
        fields.append({"name": "Fehler", "value": error_text, "inline": False})

    payload = {
        "embeds": [{
            "title":     title,
            "color":     color,
            "fields":    fields,
            "footer":    {"text": "MediaForge"},
            "timestamp": _utc_iso(),
        }]
    }

    def _send():
        code = _post_json(webhook_url, payload)
        if code and code not in (200, 204):
            logger.warning("[Discord] Webhook returned HTTP %s", code)

    threading.Thread(target=_send, daemon=True).start()


def notify_discord_autosync(title: str, new_count: int) -> None:
    if _get_setting("notif_discord_enabled", "1") == "0":
        return
    webhook_url = (
        _get_setting("notif_discord_webhook_url")
        or os.environ.get("MEDIAFORGE_DISCORD_WEBHOOK", "")
    ).strip()
    if not webhook_url:
        return
    if _get_setting("notif_discord_on_autosync", "1") == "0":
        return

    payload = {
        "embeds": [{
            "title":     title,
            "color":     0x5865F2,
            "fields":    [
                {"name": "Neue Folgen", "value": str(new_count), "inline": True},
                {"name": "Status",      "value": "Online verfuegbar", "inline": True},
            ],
            "footer":    {"text": "MediaForge - Auto-Sync"},
            "timestamp": _utc_iso(),
        }]
    }

    threading.Thread(target=lambda: _post_json(webhook_url, payload), daemon=True).start()


def notify_discord_system(title: str, body: str, event: str) -> None:
    """Discord embed for system events: on_sync_error, on_disk_space_low, on_sync_hold, on_sync_resume."""
    if _get_setting("notif_discord_enabled", "1") == "0":
        return
    webhook_url = (
        _get_setting("notif_discord_webhook_url")
        or os.environ.get("MEDIAFORGE_DISCORD_WEBHOOK", "")
    ).strip()
    if not webhook_url:
        return
    _setting_event = "on_sync_hold" if event == "on_sync_resume" else event
    if _get_setting("notif_discord_" + _setting_event, "1") == "0":
        return
    color_map = {
        "on_sync_error":   0xED4245,  # Red
        "on_sync_hold":    0xFEE75C,  # Yellow
        "on_sync_resume":  0x57F287,  # Green
        "on_disk_space_low": 0xFEE75C,
    }
    color = color_map.get(event, 0xFEE75C)
    payload = {
        "embeds": [{
            "title":       title,
            "description": body,
            "color":       color,
            "footer":      {"text": "MediaForge"},
            "timestamp":   _utc_iso(),
        }]
    }
    threading.Thread(target=lambda: _post_json(webhook_url, payload), daemon=True).start()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg_escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(("\\" + c) if c in special else c for c in str(text))


def notify_telegram(
    title: str,
    body: str,
    event: str | None = None,
    username: str | None = None,
    errors: list | None = None,
) -> None:
    # These are all routine, expected no-ops (not configured / intentionally
    # disabled), not error conditions — every notify_all() call goes through
    # here for every user regardless of whether they use Telegram at all, so
    # this fires constantly for anyone who hasn't set it up. Logging them at
    # WARNING made an unconfigured Telegram integration look like it was
    # repeatedly "trying to activate itself" on every download event/restart,
    # when in fact nothing was ever sent. Keep these at DEBUG, matching the
    # global-disabled check below.
    if _get_setting("notif_telegram_enabled", "1") == "0":
        logger.debug("[Telegram] Skipping — globally disabled (notif_telegram_enabled=0)")
        return
    bot_token = _get_setting("notif_telegram_bot_token").strip()
    if not bot_token:
        logger.debug("[Telegram] Skipping — no bot token configured")
        return
    prefs   = _get_user_prefs(username)
    if not _pref_enabled(prefs, "telegram_enabled"):
        logger.debug("[Telegram] Skipping — telegram_enabled=0 in user prefs (username=%s)", username)
        return
    chat_id = prefs.get("telegram_chat_id", "").strip()
    if not chat_id:
        logger.debug("[Telegram] Skipping — no telegram_chat_id in user prefs (username=%s)", username)
        return
    _pref_event = "on_sync_hold" if event == "on_sync_resume" else event
    if event and not _pref_enabled(prefs, "telegram_" + _pref_event):
        logger.debug("[Telegram] Skipping — event %s disabled in user prefs (username=%s)", event, username)
        return

    err_text = _format_errors_text(errors or [])
    full_body = _tg_escape(body) + ("\n\n*Fehler:*\n" + _tg_escape(err_text) if err_text else "")
    text    = "*" + _tg_escape(title) + "*\n" + full_body
    url     = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}

    def _send():
        code = _post_json(url, payload)
        if code in (200, 201):
            logger.info("[Telegram] Message sent OK (event=%s, username=%s)", event, username)
        elif code:
            logger.warning("[Telegram] sendMessage returned HTTP %s (event=%s, username=%s)", code, event, username)
        else:
            logger.warning("[Telegram] sendMessage failed — no HTTP response (event=%s, username=%s)", event, username)

    threading.Thread(target=_send, daemon=True).start()


def telegram_detect_chat_id(bot_token: str) -> str | None:
    """Poll getUpdates for the most recent chat that has messaged the bot,
    used by the Settings UI to auto-fill a user's chat_id after they send
    the bot a message (avoids having the user look up the ID manually).

    Used by: routes/push_notifications.py's Telegram chat-id auto-detect API.
    """
    url = "https://api.telegram.org/bot" + bot_token + "/getUpdates?limit=10&offset=-10"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("[Telegram] getUpdates failed: %s", exc)
        return None

    for update in reversed(data.get("result", [])):
        msg = update.get("message") or update.get("channel_post")
        if msg and "chat" in msg:
            return str(msg["chat"]["id"])
    return None


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

def notify_pushover(
    title: str,
    body: str,
    event: str | None = None,
    username: str | None = None,
    errors: list | None = None,
) -> None:
    if _get_setting("notif_pushover_enabled", "1") == "0":
        return
    app_token = _get_setting("notif_pushover_app_token").strip()
    if not app_token:
        return
    prefs    = _get_user_prefs(username)
    if not _pref_enabled(prefs, "pushover_enabled"):
        return
    user_key = prefs.get("pushover_user_key", "").strip()
    if not user_key:
        return
    _pref_event = "on_sync_hold" if event == "on_sync_resume" else event
    if event and not _pref_enabled(prefs, "pushover_" + _pref_event):
        return

    err_text = _format_errors_text(errors or [])
    full_body = body + ("\n\nFehler:\n" + err_text if err_text else "")
    payload = {"token": app_token, "user": user_key, "title": title, "message": full_body}

    def _send():
        code = _post_json("https://api.pushover.net/1/messages.json", payload)
        if code and code not in (200, 201):
            logger.warning("[Pushover] API returned HTTP %s", code)

    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# WhatsApp (via Twilio)
# ---------------------------------------------------------------------------

def notify_whatsapp(
    title: str,
    body: str,
    event: str | None = None,
    username: str | None = None,
    errors: list | None = None,
) -> None:
    if _get_setting("notif_whatsapp_enabled", "1") == "0":
        return
    sid        = _get_setting("notif_whatsapp_sid").strip()
    auth_token = _get_setting("notif_whatsapp_auth_token").strip()
    from_num   = _get_setting("notif_whatsapp_from").strip()
    if not (sid and auth_token and from_num):
        return
    prefs = _get_user_prefs(username)
    if not _pref_enabled(prefs, "whatsapp_enabled"):
        return
    phone = prefs.get("whatsapp_phone", "").strip()
    if not phone:
        return
    _pref_event = "on_sync_hold" if event == "on_sync_resume" else event
    if event and not _pref_enabled(prefs, "whatsapp_" + _pref_event):
        return

    if not from_num.startswith("whatsapp:"):
        from_num = "whatsapp:" + from_num
    if not phone.startswith("whatsapp:"):
        phone = "whatsapp:" + phone

    err_text = _format_errors_text(errors or [])
    full_body = body + ("\n\nFehler:\n" + err_text if err_text else "")
    text      = title + "\n" + full_body
    api_url   = "https://api.twilio.com/2010-04-01/Accounts/" + sid + "/Messages.json"
    post_data = urllib.parse.urlencode({"From": from_num, "To": phone, "Body": text}).encode()

    import base64
    creds   = base64.b64encode((sid + ":" + auth_token).encode()).decode()
    headers = {
        "Authorization": "Basic " + creds,
        "Content-Type":  "application/x-www-form-urlencoded",
    }

    def _send():
        req = urllib.request.Request(api_url, data=post_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201):
                    logger.warning("[WhatsApp] Twilio returned HTTP %s", resp.status)
        except urllib.error.HTTPError as exc:
            logger.warning("[WhatsApp] Twilio error HTTP %s", exc.code)
        except Exception as exc:
            logger.warning("[WhatsApp] Send failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()



# ---------------------------------------------------------------------------
# Synchronous Discord send — used by the test endpoint to return real HTTP status
# ---------------------------------------------------------------------------

def send_discord_sync(webhook_url, payload):
    """Send a Discord webhook synchronously. Returns (http_status, error_msg).

    Used by: routes/push_notifications.py's Discord "send test message" API,
    which needs the real HTTP status/error to show the admin immediately
    (unlike notify_discord*, which fire-and-forget on a background thread).
    """
    import json as _json2
    url = webhook_url.strip()
    if not url:
        return 0, "Webhook-URL nicht konfiguriert"
    data = _json2.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MediaForge/1.0 (https://github.com/PD-Codes/MediaForge)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return exc.code, body
    except Exception as exc:
        return 0, str(exc)


# ---------------------------------------------------------------------------
# NTFY
# ---------------------------------------------------------------------------

def _post_text(url: str, text: str, headers: dict | None = None) -> int:
    """POST plain text to a URL and return HTTP status (0 on error)."""
    data = (text or "").encode("utf-8")
    h = {
        "Content-Type": "text/plain",
        "User-Agent": "MediaForge/1.0 (https://github.com/PD-Codes/MediaForge)",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        logger.warning("[NTFY] HTTP POST to %s failed: %s", url, exc)
        return 0


def notify_ntfy(
    title: str,
    body: str,
    event: str | None = None,
    username: str | None = None,
    errors: list | None = None,
) -> None:
    """Send a notification to a ntfy topic.

    Settings (app_settings keys):
      - notif_ntfy_enabled (default "1")
      - notif_ntfy_server  (base URL, required)
      - notif_ntfy_topic   (topic name, required)
      - notif_ntfy_auth_token (optional bearer token)
      - notif_ntfy_user / notif_ntfy_password (optional basic auth)
"""
    if _get_setting("notif_ntfy_enabled", "1") == "0":
        return

    server = _get_setting("notif_ntfy_server", "").strip()
    topic = _get_setting("notif_ntfy_topic", "").strip()
    if not (server and topic):
        return

    prefs = _get_user_prefs(username)
    if not _pref_enabled(prefs, "ntfy_enabled"):
        return
    _pref_event = "on_sync_hold" if event == "on_sync_resume" else event
    if event and not _pref_enabled(prefs, "ntfy_" + _pref_event):
        return

    err_text = _format_errors_text(errors or [])
    full_body = body + ("\n\nFehler:\n" + err_text if err_text else "")

    # build URL and headers
    topic_quoted = urllib.parse.quote(topic, safe="")
    url = server.rstrip("/") + "/" + topic_quoted
    headers: dict = {"Title": title}

    token = _get_setting("notif_ntfy_auth_token", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    else:
        user = _get_setting("notif_ntfy_user", "").strip()
        pwd = _get_setting("notif_ntfy_password", "").strip()
        if user and pwd:
            import base64

            creds = base64.b64encode((user + ":" + pwd).encode()).decode()
            headers["Authorization"] = "Basic " + creds

    def _send():
        code = _post_text(url, full_body, headers=headers)
        if code and code not in (200, 201, 204):
            logger.warning("[NTFY] send returned HTTP %s", code)

    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Convenience: notify all services for a single event
# ---------------------------------------------------------------------------

def notify_all(
    title: str,
    body: str,
    event: str,
    username: str | None = None,
    status: str | None = None,
    episode_count: int = 0,
    errors: list | None = None,
    is_movie: bool = False,
) -> None:
    """Fan out one notification to every configured channel (WebPush,
    Telegram, Pushover, NTFY, WhatsApp, Discord), then to every third-party
    channel/event hook a module has registered (see
    ``web/thirdparties/registry.py``'s ``register_notification_channel`` /
    ``register_event_hook``). Each channel/hook is called in its own
    try/except so one failing/misconfigured service never blocks the
    others; individual notify_* functions handle their own enabled/pref
    checks and send asynchronously in a background thread.

    Used by: queue_worker.py (download completion/error/cancel events) and
    autosync_worker.py (auto-sync found episodes / sync hold / resume).
    """
    try:
        notify_webpush(title=title, body=body, event=event, username=username)
    except Exception as exc:
        logger.error("[Notif] Webpush notification failed: %s", exc, exc_info=True)
    try:
        notify_telegram(title=title, body=body, event=event, username=username, errors=errors)
    except Exception as exc:
        logger.error("[Notif] Telegram notification failed: %s", exc, exc_info=True)
    try:
        notify_pushover(title=title, body=body, event=event, username=username, errors=errors)
    except Exception as exc:
        logger.error("[Notif] Pushover notification failed: %s", exc, exc_info=True)
    try:
        notify_ntfy(title=title, body=body, event=event, username=username, errors=errors)
    except Exception as exc:
        logger.error("[Notif] NTFY notification failed: %s", exc, exc_info=True)
    try:
        notify_whatsapp(title=title, body=body, event=event, username=username, errors=errors)
    except Exception as exc:
        logger.error("[Notif] WhatsApp notification failed: %s", exc, exc_info=True)
    
    try:
        if event == "on_autosync":
            notify_discord_autosync(title=title, new_count=episode_count)
        elif event in ("on_sync_error", "on_disk_space_low", "on_sync_hold", "on_sync_resume"):
            notify_discord_system(title=title, body=body, event=event)
        elif status is not None:
            notify_discord(
                title=title,
                status=status,
                episode_count=episode_count,
                errors=errors or [],
                is_movie=is_movie,
            )
    except Exception as exc:
        logger.error("[Notif] Discord notification failed: %s", exc, exc_info=True)

    # Third-party notification channels (see registry.register_notification_channel).
    try:
        from .thirdparties.registry import notification_channels
        for _channel_id, _send_fn in notification_channels().items():
            try:
                _send_fn(
                    title=title,
                    body=body,
                    event=event,
                    username=username,
                    status=status,
                    episode_count=episode_count,
                    errors=errors,
                    is_movie=is_movie,
                )
            except Exception as exc:
                logger.error("[Notif] Module channel '%s' failed: %s", _channel_id, exc, exc_info=True)
    except Exception as exc:
        logger.error("[Notif] Failed to fan out to module notification channels: %s", exc, exc_info=True)

    # Generic lifecycle event hooks (see registry.register_event_hook) --
    # for modules reacting to the event itself rather than sending a message.
    try:
        from .thirdparties.registry import fire_event_hooks
        fire_event_hooks(
            event,
            title=title,
            body=body,
            username=username,
            status=status,
            episode_count=episode_count,
            errors=errors,
            is_movie=is_movie,
        )
    except Exception as exc:
        logger.error("[Notif] Failed to fire module event hooks: %s", exc, exc_info=True)
