"""Per-user notification preferences.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import get_db
from .push import db_add_push_subscription

logger = get_logger(__name__)


_CREATE_USER_NOTIF_PREFS_TABLE = """\
CREATE TABLE IF NOT EXISTS user_notification_prefs (
    user_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""

_CREATE_PUSH_SUBSCRIPTIONS_TABLE = """\
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT    NOT NULL UNIQUE,
    user_id    INTEGER,
    auth       TEXT    NOT NULL,
    p256dh     TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def init_notification_db() -> None:
    """Create notification tables and migrate legacy JSON subscription file."""
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_USER_NOTIF_PREFS_TABLE)
        conn.execute(_CREATE_PUSH_SUBSCRIPTIONS_TABLE)
        conn.commit()
    finally:
        conn.close()

    # One-time migration: import legacy push_subscriptions.json into DB
    import json as _json
    legacy = MEDIAFORGE_CONFIG_DIR / "push_subscriptions.json"
    if legacy.exists():
        try:
            data = _json.loads(legacy.read_text())
            if isinstance(data, list):
                for sub in data:
                    ep     = sub.get("endpoint", "")
                    keys   = sub.get("keys", {})
                    auth   = keys.get("auth", "")
                    p256dh = keys.get("p256dh", "")
                    if ep and auth and p256dh:
                        db_add_push_subscription(ep, auth, p256dh)
            legacy.rename(legacy.with_suffix(".json.migrated"))
            logger.info("[DB] Migrated %d push subscription(s) from legacy JSON", len(data))
        except Exception as exc:
            logger.warning("[DB] Push subscription migration failed: %s", exc)

    # One-time migration: retire legacy push_prefs.json (prefs now live in DB)
    legacy_prefs = MEDIAFORGE_CONFIG_DIR / "push_prefs.json"
    if legacy_prefs.exists():
        try:
            legacy_prefs.rename(legacy_prefs.with_suffix(".json.migrated"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# User notification preferences
# ---------------------------------------------------------------------------

def get_user_id_by_username(username: str) -> "int | None":
    """Resolve a username to its numeric user id, for use as the key in
    the per-user notification-prefs / watch-progress tables.

    In no-auth mode (see app.py: init_db()/the users table is only created
    when auth is enabled) the session always uses the pseudo-username
    "admin" with no backing row, so that case short-circuits to id 0
    instead of hitting a nonexistent table.
    """
    if not username:
        return None
    # In no-auth mode there is no users table — return 0 (pseudo-user)
    if username == "admin":
        conn = get_db()
        try:
            conn.execute("SELECT 1 FROM users LIMIT 1")
        except Exception:
            conn.close()
            return 0  # no-auth pseudo-user
        conn.close()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_user_notif_pref(user_id: int, key: str, default: str = "") -> str:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM user_notification_prefs WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def get_user_notif_prefs_all(user_id: int) -> dict:
    """Return all notification prefs for *user_id* as a plain dict."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM user_notification_prefs WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def set_user_notif_pref(user_id: int, key: str, value: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user_notification_prefs (user_id, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_notif_prefs_bulk(user_id: int, prefs: dict) -> None:
    """Upsert multiple preference keys at once for *user_id*."""
    if not prefs:
        return
    conn = get_db()
    try:
        for key, value in prefs.items():
            conn.execute(
                "INSERT INTO user_notification_prefs (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (user_id, key, str(value)),
            )
        conn.commit()
    finally:
        conn.close()
