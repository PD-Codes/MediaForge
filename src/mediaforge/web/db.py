"""SQLite persistence layer for the MediaForge web app.

This module owns the single on-disk SQLite database (``mediaforge.db``) and
every table used by the web UI and its background workers: user accounts,
the download queue, auto-sync jobs, download history, statistics, custom
paths, favourites, app settings (including encrypted secrets), notification
prefs/push subscriptions, various result caches (TMDB/provider/browse/
mediascan), the calendar watcher, the upscale queue, watch progress, and
uptime monitoring heartbeats.

Each public function opens its own connection via ``get_db()``, does its
work, and closes it again — see ``get_db()`` for how connection reuse and
WAL mode are handled. Tables are created and migrated lazily by the
``init_*_db()`` functions, which are called once at app startup (see
``mediaforge/web/app.py``) and are safe to call repeatedly.
"""

import os
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from ..config import MEDIAFORGE_CONFIG_DIR
from ..logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Single-instance file lock — warns when two processes share the same DB
# ---------------------------------------------------------------------------

_LOCK_PATH = MEDIAFORGE_CONFIG_DIR / "mediaforge.pid"
_instance_lock_fh = None  # keep file handle open to hold the lock


def acquire_instance_lock() -> bool:
    """Write a PID lock file so a second instance can detect the conflict.

    Uses fcntl.flock on POSIX and a best-effort PID check on Windows.
    Returns True if this process holds the lock, False if another instance
    is already running (a warning is logged but startup is not blocked).
    """
    global _instance_lock_fh
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = str(_LOCK_PATH)

    try:
        import platform
        if platform.system() == "Windows":
            # Windows: check PID file for a running process
            if _LOCK_PATH.exists():
                try:
                    pid = int(_LOCK_PATH.read_text().strip())
                    import ctypes
                    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        logger.warning(
                            "[DB] Another instance (PID %d) appears to be running against "
                            "the same database. Concurrent writes may corrupt data.", pid
                        )
                        return False
                except Exception:
                    pass  # stale lock — overwrite below
            _LOCK_PATH.write_text(str(os.getpid()))
            return True
        else:
            import fcntl
            fh = open(lock_path, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    pid = int(open(lock_path).read().strip())
                except Exception:
                    pid = "?"
                logger.warning(
                    "[DB] Another instance (PID %s) is already running against the same "
                    "database. Concurrent writes may corrupt data.", pid
                )
                fh.close()
                return False
            fh.write(str(os.getpid()))
            fh.flush()
            _instance_lock_fh = fh  # keep open to hold flock
            return True
    except Exception as e:
        logger.warning("[DB] Could not acquire instance lock: %s", e)
        return True  # non-fatal — proceed anyway



# ---------------------------------------------------------------------------
# Sensitive setting keys — values are stored encrypted in the database
# ---------------------------------------------------------------------------

SENSITIVE_KEYS: frozenset = frozenset({
    "external_api_key",
    "seerr_api_key",
    "oidc_client_secret",
    "cineinfo_tmdb_api_key",
    "mediaplayer_apikey",
    "mediascan_jf_apikey",
    "notif_telegram_bot_token",
    "notif_pushover_app_token",
    "notif_discord_webhook_url",
    "notif_ntfy_auth_token",
    "notif_ntfy_password",
    "pushover_user_key",
    "crunchyroll_email",
    "crunchyroll_password",
    "crunchyroll_session_key",
})

# Sensitive keys registered at runtime on top of the frozen core set above --
# populated by register_sensitive_keys(), which is how a third-party module
# marks a setting of its own (e.g. "module:discord_request_bot:token") as
# secret without needing a core release to add it to SENSITIVE_KEYS. Kept as a
# separate mutable set so the core list stays a frozenset (i.e. still can't be
# mutated by accident from anywhere else).
_RUNTIME_SENSITIVE_KEYS: set = set()

_ENC_PREFIX = "enc:"
_fernet_instance = None


def is_sensitive_key(key: str) -> bool:
    """True if `key`'s value is stored encrypted (core set or runtime-registered).
    """
    return key in SENSITIVE_KEYS or key in _RUNTIME_SENSITIVE_KEYS


def register_sensitive_keys(keys) -> int:
    """Mark app_settings `keys` as sensitive from here on: set_setting() will
    encrypt their values, get_setting() decrypts them, and any value already
    stored in plaintext is encrypted right now (same one-shot migration
    _migrate_sensitive_settings() does for the core keys at startup).

    This is the registry mechanism modules use -- see
    thirdparties/registry.py: every extra_settings field declared with
    type="secret" is registered here automatically, and a module can name
    further keys (ones with no settings-card field of their own) via the
    MODULE_SENSITIVE_SETTINGS constant.

    Registering is deliberately one-way and cumulative: a key never becomes
    "not sensitive" again, because a disabled/uninstalled module leaving an
    already-encrypted value behind must still be readable. get_setting()
    decrypts anything carrying the _ENC_PREFIX regardless of registration for
    the same reason.

    Returns how many previously-plaintext values were encrypted by this call.
    """
    new_keys = {k for k in (keys or ()) if k and not is_sensitive_key(k)}
    if not new_keys:
        return 0
    _RUNTIME_SENSITIVE_KEYS.update(new_keys)
    return _encrypt_existing_plaintext(new_keys)


def _encrypt_existing_plaintext(keys) -> int:
    """Encrypt any of `keys` still stored as plaintext. Best-effort: a missing
    app_settings table (registration before the DB is initialized) or any DB
    error is logged, never raised -- a module must not fail to load because a
    value couldn't be re-encrypted, and the next set_setting() writes it
    encrypted anyway."""
    conn = get_db()
    migrated = 0
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
        ).fetchone()
        if not tbl:
            return 0
        keys = tuple(keys)
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ({})".format(
                ",".join("?" * len(keys))
            ),
            keys,
        ).fetchall()
        for row in rows:
            key, val = row["key"], row["value"]
            if val and not val.startswith(_ENC_PREFIX):
                encrypted = _encrypt_value(val)
                if encrypted != val:  # encryption succeeded
                    conn.execute(
                        "UPDATE app_settings SET value = ? WHERE key = ?",
                        (encrypted, key),
                    )
                    migrated += 1
        if migrated:
            conn.commit()
            logger.info("Encrypted %d previously plaintext sensitive setting(s)", migrated)
    except Exception:
        logger.warning("Error encrypting sensitive settings", exc_info=True)
    finally:
        conn.close()
    return migrated


def _get_fernet():
    """Return a Fernet instance keyed from the Flask secret, or None on error."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    try:
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes

        secret_path = MEDIAFORGE_CONFIG_DIR / ".flask_secret"
        if not secret_path.exists():
            return None
        raw_secret = secret_path.read_bytes()

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"aniworld-settings-v1",
            info=b"aniworld-db-encryption",
        )
        fernet_key = base64.urlsafe_b64encode(hkdf.derive(raw_secret))
        _fernet_instance = Fernet(fernet_key)
        return _fernet_instance
    except Exception:
        logger.warning("Could not initialize settings encryption", exc_info=True)
        return None


def _encrypt_value(plaintext: str) -> str:
    """Encrypt a sensitive value. Falls back to plaintext if encryption unavailable."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    try:
        return _ENC_PREFIX + f.encrypt(plaintext.encode()).decode()
    except Exception:
        logger.warning("Failed to encrypt setting value")
        return plaintext


def _decrypt_value(stored: str) -> str:
    """Decrypt a value. Transparently handles legacy plaintext values."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    f = _get_fernet()
    if f is None:
        return stored
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt setting value — returning raw stored value")
        return stored

DB_PATH = MEDIAFORGE_CONFIG_DIR / "mediaforge.db"

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    auth_method TEXT NOT NULL DEFAULT 'local',
    sso_subject TEXT,
    sso_issuer TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SSO_INDEX = """\
CREATE UNIQUE INDEX IF NOT EXISTS idx_sso_identity
ON users (sso_issuer, sso_subject)
WHERE sso_issuer IS NOT NULL AND sso_subject IS NOT NULL;
"""


class ContextConnection(sqlite3.Connection):
    """sqlite3.Connection subclass whose close() is a no-op while it is the
    connection cached on the current Flask request (``g.db_conn``).

    This lets every function in this module call ``conn.close()`` in a
    ``finally`` block unconditionally (simple, uniform code) while still
    reusing a single connection per request when one is available: the
    real close happens once, via Flask app-context teardown, not on every
    call. Outside of a request (e.g. background worker threads), close()
    behaves normally.
    """

    def close(self):
        try:
            from flask import g, has_app_context
            if has_app_context() and g.get("db_conn") is self:
                return  # Do not close if cached in request context
        except Exception:
            pass
        super().close()


def get_db():
    """Return a SQLite connection for the current context.

    Reuses the connection cached on the active Flask request (``g.db_conn``)
    when a request context exists; otherwise opens a fresh connection (used
    by background worker threads, which have no request context). WAL
    journal mode + a 30s busy_timeout are set on every connection so
    concurrent readers/writers (web requests, queue worker, autosync
    worker, upscale worker, ...) do not immediately hit "database is
    locked" errors.
    """
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from flask import g, has_app_context
        if has_app_context():
            if "db_conn" not in g:
                conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False, factory=ContextConnection)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                g.db_conn = conn
            return g.db_conn
    except Exception:
        pass

    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False, factory=ContextConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate_db(conn):
    """Add columns to the users table that were introduced after the
    initial CREATE TABLE, so existing databases stay compatible.

    Each column is added only if missing (checked via PRAGMA table_info),
    so this is safe to call on every startup.
    """
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    columns = {r["name"] for r in rows}

    if "auth_method" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'local'"
        )
    if "sso_subject" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN sso_subject TEXT")
    if "sso_issuer" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN sso_issuer TEXT")

    if "language" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"
        )

    conn.execute(_CREATE_SSO_INDEX)
    conn.commit()


def init_db():
    """Create the users table (and migrate it) and auto-create an admin
    account from MEDIAFORGE_WEB_ADMIN_USER/PASS env vars if none exists yet.

    Used by: mediaforge/web/app.py (create_app, only when auth is enabled).
    """
    acquire_instance_lock()
    conn = get_db()
    try:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_SSO_INDEX)
        conn.commit()
        _migrate_db(conn)
    finally:
        conn.close()

    if not has_any_admin():
        env_user = os.environ.get("MEDIAFORGE_WEB_ADMIN_USER", "").strip()
        env_pass = os.environ.get("MEDIAFORGE_WEB_ADMIN_PASS", "").strip()
        if env_user and env_pass:
            create_user(env_user, env_pass, role="admin")
            logger.info("Auto-created admin user '%s' from environment", env_user)


def has_any_admin():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
        ).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def create_user(username, password, role="user", language="en"):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, language) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, language),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_language(user_id: int) -> str:
    """Return the UI language code for a user ('en' or 'de'). Defaults to 'en'."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT language FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row["language"] in ("en", "de"):
            return row["language"]
        return "en"
    finally:
        conn.close()


def set_user_language(user_id: int, language: str) -> None:
    """Persist the UI language preference for a user."""
    if language not in ("en", "de"):
        language = "en"
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET language = ? WHERE id = ?", (language, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None, "Invalid username or password."
        if row["auth_method"] != "local":
            return None, "This account uses SSO. Please use the SSO login button."
        if check_password_hash(row["password_hash"], password):
            return {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
            }, None
        return None, "Invalid username or password."
    finally:
        conn.close()


def find_or_create_sso_user(
    issuer, subject, username, admin_username=None, admin_subject=None
):
    def _should_be_admin():
        # Subject-based promotion takes full priority — it is tied to the IdP
        # identity and cannot be spoofed by changing a display name.
        if admin_subject:
            return subject == admin_subject
        # Fall back to username only when no subject is configured.
        # This is weaker: warn once so admins know to upgrade.
        if admin_username and username == admin_username:
            logger.warning(
                "OIDC admin promotion matched by username '%s'. "
                "Configure OIDC_ADMIN_SUBJECT for stronger identity binding.",
                username,
            )
            return True
        return False

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE sso_issuer = ? AND sso_subject = ?",
            (issuer, subject),
        ).fetchone()

        if row:
            user = {"id": row["id"], "username": row["username"], "role": row["role"]}
            if _should_be_admin() and row["role"] != "admin":
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],)
                )
                conn.commit()
                user["role"] = "admin"
            return user

        # Check for username conflict with local users
        existing = conn.execute(
            "SELECT id, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"Username '{username}' is already taken by a local account."
            )

        role = "admin" if _should_be_admin() else "user"
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, auth_method, sso_subject, sso_issuer) "
            "VALUES (?, ?, ?, 'oidc', ?, ?)",
            (username, "", role, subject, issuer),
        )
        conn.commit()
        return {"id": cur.lastrowid, "username": username, "role": role}
    finally:
        conn.close()


def list_users():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, username, role, auth_method, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_user(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot delete the last admin"
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def update_user_role(user_id, new_role):
    if new_role not in ("admin", "user"):
        return False, "Invalid role"
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin" and new_role != "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot demote the last admin"
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ===== Download Queue =====

_CREATE_QUEUE_TABLE = """\
CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    series_url TEXT NOT NULL,
    episodes TEXT NOT NULL,
    total_episodes INTEGER NOT NULL,
    language TEXT NOT NULL,
    provider TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','completed','partial','failed','cancelled')),
    current_episode INTEGER NOT NULL DEFAULT 0,
    current_url TEXT,
    errors TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    upscale INTEGER NOT NULL DEFAULT 0
);
"""


def init_queue_db():
    """Create the download_queue table and apply schema migrations.

    There is no formal migration/version table: each column added after the
    initial release is applied via an ALTER TABLE wrapped in try/except,
    where "duplicate column" errors are swallowed because they just mean
    the column was already added on a previous run. The CHECK constraint
    migration (adding the 'partial' status) instead recreates the whole
    table, since SQLite cannot ALTER a CHECK constraint in place.
    """
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_QUEUE_TABLE)
        # Add upscale column (migration for existing DBs)
        try:
            conn.execute("ALTER TABLE download_queue ADD COLUMN upscale INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # column already exists
        # Add position column for queue reordering (migration for existing DBs)
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
            # Backfill: set position = id for existing rows
            conn.execute("UPDATE download_queue SET position = id WHERE position = 0")
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add custom_path_id column (migration for existing DBs)
        try:
            conn.execute("ALTER TABLE download_queue ADD COLUMN custom_path_id INTEGER")
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add source column (migration for existing DBs) - marks origin: 'manual' or 'sync'
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add captcha_url column (migration for existing DBs)
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN captcha_url TEXT"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add hidden column — rows with hidden=1 are excluded from the queue UI
        # but retained for statistics (migration for existing DBs)
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add speed/size columns (migration for existing DBs)
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN average_speed_mbps REAL"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN total_size_mb REAL"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add format_id column (migration for existing DBs) — the yt-dlp
        # format selector picked in the Direct Link format-picker modal
        # (see models/direct_link/probe.py). NULL for all non-direct-link
        # jobs; those are identified by provider = 'Direct'.
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN format_id TEXT"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add source_provider column (migration for existing DBs) — the
        # embed host (e.g. "VOE") a Direct Link job's URL was recognized as
        # at probe time, if any (see models/direct_link/probe.py). NULL for
        # generic direct links and all non-direct-link jobs.
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN source_provider TEXT"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Migrate CHECK constraint to include 'partial' status (existing DBs)
        # SQLite cannot ALTER constraints — must recreate the table
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='download_queue'"
            ).fetchone()
            if row and "'partial'" not in row["sql"]:
                conn.execute("ALTER TABLE download_queue RENAME TO _download_queue_old")
                conn.execute(_CREATE_QUEUE_TABLE)
                conn.execute(
                    """INSERT INTO download_queue
                        SELECT id, title, series_url, episodes, total_episodes, language,
                               provider, username,
                               CASE WHEN status = 'partial' THEN 'partial' ELSE status END,
                               current_episode, current_url, errors, created_at, completed_at
                        FROM _download_queue_old"""
                )
                # Re-add columns added via ALTER TABLE (may not exist in old table)
                for col, sql in [
                    ("position",    "ALTER TABLE download_queue ADD COLUMN position INTEGER NOT NULL DEFAULT 0"),
                    ("custom_path_id", "ALTER TABLE download_queue ADD COLUMN custom_path_id INTEGER"),
                    ("source",      "ALTER TABLE download_queue ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
                    ("captcha_url", "ALTER TABLE download_queue ADD COLUMN captcha_url TEXT"),
                    ("hidden",      "ALTER TABLE download_queue ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"),
                    ("format_id",   "ALTER TABLE download_queue ADD COLUMN format_id TEXT"),
                    ("source_provider", "ALTER TABLE download_queue ADD COLUMN source_provider TEXT"),
                ]:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError as _mig_err:
                        if "duplicate column" not in str(_mig_err).lower():
                            logger.warning("[Migration] Column add error: %s", _mig_err)
                # Copy extra columns from old table if they exist
                try:
                    conn.execute(
                        """UPDATE download_queue AS new SET
                            position = (SELECT position FROM _download_queue_old WHERE id = new.id),
                            custom_path_id = (SELECT custom_path_id FROM _download_queue_old WHERE id = new.id),
                            source = (SELECT source FROM _download_queue_old WHERE id = new.id),
                            captcha_url = (SELECT captcha_url FROM _download_queue_old WHERE id = new.id),
                            hidden = (SELECT hidden FROM _download_queue_old WHERE id = new.id),
                            format_id = (SELECT format_id FROM _download_queue_old WHERE id = new.id),
                            source_provider = (SELECT source_provider FROM _download_queue_old WHERE id = new.id)"""
                    )
                except Exception as _mig_err:
                    logger.warning("[Migration] Could not copy extra columns: %s", _mig_err)
                conn.execute("DROP TABLE _download_queue_old")
        except Exception as _mig_err:
            logger.error("[Migration] Table migration failed: %s", _mig_err, exc_info=True)

        # Index for frequent status+position queries (queue worker, reordering)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dq_status_position "
            "ON download_queue(status, position)"
        )
        conn.commit()
    finally:
        conn.close()


def add_to_queue(
    title,
    series_url,
    episodes,
    language,
    provider,
    username=None,
    custom_path_id=None,
    source="manual",
    upscale=False,
    format_id=None,
    source_provider=None,
):
    import json

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO download_queue (title, series_url, episodes, total_episodes, language, provider, username, custom_path_id, source, upscale, format_id, source_provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                series_url,
                json.dumps(episodes),
                len(episodes),
                language,
                provider,
                username,
                custom_path_id,
                source,
                1 if upscale else 0,
                format_id,
                source_provider,
            ),
        )
        row_id = cur.lastrowid
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?", (row_id, row_id)
        )
        conn.commit()
        return row_id
    finally:
        conn.close()


def is_series_queued_or_running(series_url, language=None, requested_episodes=None):
    """Check if a series already has an overlapping set of episodes in the queue."""
    import json
    series_url = series_url.strip().rstrip("/")
    conn = get_db()
    try:
        query = (
            "SELECT episodes FROM download_queue "
            "WHERE (series_url = ? OR series_url = ?) AND status IN ('queued', 'running')"
        )
        params = [series_url, series_url + "/"]
        if language:
            query += " AND language = ?"
            params.append(language)

        rows = conn.execute(query, tuple(params)).fetchall()
        if not rows:
            return False
            
        # If no specific episodes provided, fall back to "any item exists" (stricter)
        if not requested_episodes:
            return len(rows) > 0

        # Check if any requested episode URL is already in the existing items
        requested_set = set(requested_episodes)
        for row in rows:
            try:
                existing_episodes = set(json.loads(row["episodes"]))
                if not requested_set.isdisjoint(existing_episodes):
                    return True # Overlap found!
            except Exception:
                continue
        
        return False
    finally:
        conn.close()


def get_queue():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM download_queue WHERE hidden = 0 ORDER BY position ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_queue_item(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_next_queued():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE status = 'queued' "
            "ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_next_queued():
    """Atomically claim the next queued item and mark it as running.

    Uses BEGIN IMMEDIATE so the check-then-update is a single atomic
    operation even across multiple processes sharing the same SQLite file.
    Returns the claimed item dict, or None if nothing is available.

    Uses its own raw connection instead of get_db(), since this is called
    from the background queue worker thread which has no Flask request
    context to cache a connection on.

    Used by: mediaforge/web/queue_worker.py (background download worker loop).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT id FROM download_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT * FROM download_queue WHERE status = 'queued' "
            "ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        item = dict(row)
        conn.execute(
            "UPDATE download_queue SET status = 'running' WHERE id = ?",
            (item["id"],),
        )
        conn.execute("COMMIT")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def move_queue_item(queue_id, direction):
    """Swap position of a queued item with its neighbor. direction: 'up' or 'down'."""
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT id, position FROM download_queue WHERE id = ? AND status = 'queued'",
            (queue_id,),
        ).fetchone()
        if not item:
            return False, "Item not found or not queued"

        if direction == "up":
            neighbor = conn.execute(
                "SELECT id, position FROM download_queue "
                "WHERE status = 'queued' AND position < ? "
                "ORDER BY position DESC LIMIT 1",
                (item["position"],),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT id, position FROM download_queue "
                "WHERE status = 'queued' AND position > ? "
                "ORDER BY position ASC LIMIT 1",
                (item["position"],),
            ).fetchone()

        if not neighbor:
            return False, "Already at the edge"

        # Swap positions
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (neighbor["position"], item["id"]),
        )
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (item["position"], neighbor["id"]),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def get_running():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_queue_progress(queue_id, current_episode, current_url):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET current_episode = ?, current_url = ? WHERE id = ?",
            (current_episode, current_url, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_queue_status(queue_id, status):
    conn = get_db()
    try:
        if status in ("completed", "failed", "partial"):
            conn.execute(
                "UPDATE download_queue SET status = ?, completed_at = datetime('now') WHERE id = ?",
                (status, queue_id),
            )
        else:
            conn.execute(
                "UPDATE download_queue SET status = ? WHERE id = ?",
                (status, queue_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_queue_errors(queue_id, errors_json):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET errors = ? WHERE id = ?",
            (errors_json, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_queue_stats(queue_id, average_speed_mbps, total_size_mb):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET average_speed_mbps = ?, total_size_mb = ? WHERE id = ?",
            (average_speed_mbps, total_size_mb, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_queue_item(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] != "running":
            return False, "Can only cancel running items"
        conn.execute(
            "UPDATE download_queue SET status = 'cancelled' WHERE id = ?",
            (queue_id,),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def is_queue_cancelled(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return row and row["status"] == "cancelled"
    finally:
        conn.close()


def remove_from_queue(queue_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        status = row["status"]
        if status == "running":
            return False, "Cannot remove a running item (cancel it first)"
        if status == "queued":
            # Never ran — safe to delete permanently (no stats value)
            conn.execute("DELETE FROM download_queue WHERE id = ?", (queue_id,))
        else:
            # completed / failed / cancelled — hide so stats are preserved
            conn.execute(
                "UPDATE download_queue SET hidden = 1 WHERE id = ?", (queue_id,)
            )
        conn.commit()
        return True, None
    finally:
        conn.close()


def restart_queue_item_inplace(queue_id, episodes):
    """Reset an existing queue item back to 'queued' with the given episode list (in-place)."""
    import json as _json
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM download_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] == "running":
            return False, "Item is currently running"
        conn.execute(
            """UPDATE download_queue SET
                status = 'queued',
                hidden = 0,
                episodes = ?,
                total_episodes = ?,
                current_episode = 0,
                errors = '[]',
                current_url = NULL,
                completed_at = NULL
               WHERE id = ?""",
            (_json.dumps(episodes), len(episodes), queue_id),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def retry_single_episode(queue_id, ep_url):
    """Retry one failed episode in-place.

    Unlike restart_queue_item_inplace this preserves all OTHER errors so they
    remain visible in the UI.  Only the error entry for *ep_url* is removed.
    total_episodes is kept at the original value so the job still looks like
    the same job in the queue.
    """
    import json as _json
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status, errors, total_episodes FROM download_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] == "running":
            return False, "Item is currently running"

        # Remove only the error for the specific episode being retried.
        try:
            existing_errors = _json.loads(row["errors"] or "[]")
        except (ValueError, _json.JSONDecodeError):
            existing_errors = []
        kept_errors = [e for e in existing_errors if e.get("url") != ep_url]

        conn.execute(
            """UPDATE download_queue SET
                status = 'queued',
                hidden = 0,
                episodes = ?,
                current_episode = 0,
                errors = ?,
                current_url = NULL,
                completed_at = NULL
               WHERE id = ?""",
            (_json.dumps([ep_url]), _json.dumps(kept_errors), queue_id),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def delete_completed_queue_item(queue_id):
    """Delete a queue item only if its status is 'completed'. Used by auto-sync cleanup."""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM download_queue WHERE id = ? AND status = 'completed'",
            (queue_id,),
        )
        conn.commit()
    finally:
        conn.close()


def set_captcha_url(queue_id: int, url: str):
    """Store the current captcha URL for a running queue item."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET captcha_url = ? WHERE id = ?",
            (url, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def clear_captcha_url(queue_id: int):
    """Clear the captcha URL when captcha has been solved."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET captcha_url = NULL WHERE id = ?",
            (queue_id,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_completed():
    """Hide all finished entries from the queue UI while keeping them for statistics."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET hidden = 1 "
            "WHERE status IN ('completed', 'partial', 'failed', 'cancelled')"
        )
        conn.commit()
    finally:
        conn.close()


# ===== Custom Download Paths =====

_CREATE_CUSTOM_PATHS_TABLE = """\
CREATE TABLE IF NOT EXISTS custom_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    default_sites TEXT NOT NULL DEFAULT ''
);
"""


def init_custom_paths_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_CUSTOM_PATHS_TABLE)
        # Migration for existing installations. An empty value preserves the
        # old behaviour: the global download path remains selected.
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(custom_paths)").fetchall()
        }
        if "default_sites" not in columns:
            conn.execute(
                "ALTER TABLE custom_paths ADD COLUMN default_sites TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
    finally:
        conn.close()


def get_custom_paths():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, path, default_sites FROM custom_paths ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_custom_path(name, path, default_sites=""):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO custom_paths (name, path, default_sites) VALUES (?, ?, ?)",
            (name, path, default_sites),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_custom_path(path_id, name=None, path=None, default_sites=None):
    """Update the supplied fields of a custom download path."""
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if path is not None:
        fields.append("path = ?")
        values.append(path)
    if default_sites is not None:
        fields.append("default_sites = ?")
        values.append(default_sites)
    if not fields:
        return

    values.append(path_id)
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE custom_paths SET {', '.join(fields)} WHERE id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def is_custom_path_in_use(path_id):
    """Return True if any autosync job or active queue item currently references this custom path."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE custom_path_id = ? OR movie_custom_path_id = ?",
            (path_id, path_id),
        ).fetchone()
        if row and row["cnt"] > 0:
            return True
        row_queue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE custom_path_id = ? AND status IN ('queued', 'running')",
            (path_id,),
        ).fetchone()
        return bool(row_queue and row_queue["cnt"] > 0)
    finally:
        conn.close()


def remove_custom_path(path_id):
    """Delete a custom path. Returns (True, None) on success or (False, reason) if blocked."""
    conn = get_db()
    try:
        row_sync = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE custom_path_id = ? OR movie_custom_path_id = ?",
            (path_id, path_id),
        ).fetchone()
        if row_sync and row_sync["cnt"] > 0:
            return False, "Dieser Pfad wird von mindestens einem Auto-Sync-Job verwendet und kann nicht gelöscht werden."
        row_queue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE custom_path_id = ? AND status IN ('queued', 'running')",
            (path_id,),
        ).fetchone()
        if row_queue and row_queue["cnt"] > 0:
            return False, "Dieser Pfad wird noch von aktiven oder wartenden Downloads in der Warteschlange verwendet."
        conn.execute("DELETE FROM custom_paths WHERE id = ?", (path_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def get_custom_path_by_id(path_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, path, default_sites FROM custom_paths WHERE id = ?", (path_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ===== Auto-Sync Jobs =====

_CREATE_AUTOSYNC_TABLE = """\
CREATE TABLE IF NOT EXISTS autosync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    series_url TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'German Dub',
    provider TEXT NOT NULL DEFAULT 'VOE',
    custom_path_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_by TEXT,
    last_check TEXT,
    last_new_found TEXT,
    episodes_found INTEGER NOT NULL DEFAULT 0,
    local_episodes_found INTEGER NOT NULL DEFAULT 0,
    last_new_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    on_hold INTEGER NOT NULL DEFAULT 0,
    path_unavailable_action TEXT NOT NULL DEFAULT 'skip',
    retry_count INTEGER NOT NULL DEFAULT 0,
    episode_filter TEXT,
    movie_custom_path_id INTEGER,
    filter_dirty INTEGER NOT NULL DEFAULT 0,
    group_name TEXT,
    cover_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_autosync_db():
    """Create the autosync_jobs table and apply schema migrations.

    Same ad-hoc migration pattern as init_queue_db(): each new column is
    added via a best-effort ALTER TABLE, ignoring the error when it already
    exists. Also runs a couple of one-time data migrations (rewriting
    stale s.to URLs to serienstream.to, and adding a UNIQUE index on
    series_url after de-duplicating any pre-existing rows that would
    violate it).
    """
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_AUTOSYNC_TABLE)
        # Migration: add last_new_count for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN last_new_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Migration: add local_episodes_found for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN local_episodes_found INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Migration: add last_error for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN last_error TEXT"
            )
        except Exception:
            pass
        # Migration: add on_hold for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN on_hold INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Migration: add path_unavailable_action for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN path_unavailable_action TEXT NOT NULL DEFAULT 'skip'"
            )
        except Exception:
            pass
        # Migration: add retry_count for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Migration: add episode_filter (per-job season/episode filter, JSON) for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN episode_filter TEXT"
            )
        except Exception:
            pass
        # Migration: add movie_custom_path_id (separate path for movies/specials) for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN movie_custom_path_id INTEGER"
            )
        except Exception:
            pass
        # Migration: add filter_dirty (baseline-reset flag after filter change) for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN filter_dirty INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Migration: add group_name (manual grouping of sync jobs) for existing DBs
        try:
            conn.execute(
                "ALTER TABLE autosync_jobs ADD COLUMN group_name TEXT"
            )
        except Exception:
            pass
        # Migration: add cover_url (poster images) for existing DBs
        try:
            conn.execute("ALTER TABLE autosync_jobs ADD COLUMN cover_url TEXT")
        except Exception:
            pass
        # One-time migration: rewrite legacy s.to AutoSync URLs to serienstream.to
        # (the s.to domain was deactivated). Done per-row so the UNIQUE index on
        # series_url can't be violated: if the serienstream.to equivalent already
        # exists, the stale s.to duplicate is dropped instead.
        try:
            _sto_rows = conn.execute(
                "SELECT id, series_url FROM autosync_jobs "
                "WHERE series_url LIKE '%://s.to/%' OR series_url LIKE '%://www.s.to/%'"
            ).fetchall()
            _mig = 0
            for _r in _sto_rows:
                _old = _r["series_url"]
                _new = _old.replace("://www.s.to/", "://serienstream.to/").replace("://s.to/", "://serienstream.to/")
                if _new == _old:
                    continue
                _dup = conn.execute(
                    "SELECT 1 FROM autosync_jobs WHERE series_url = ? AND id != ?",
                    (_new, _r["id"]),
                ).fetchone()
                if _dup:
                    conn.execute("DELETE FROM autosync_jobs WHERE id = ?", (_r["id"],))
                else:
                    conn.execute("UPDATE autosync_jobs SET series_url = ? WHERE id = ?", (_new, _r["id"]))
                    _mig += 1
            if _mig:
                conn.commit()
                logger.info("[Migration] Rewrote %d AutoSync s.to URL(s) to serienstream.to", _mig)
        except Exception:
            logger.warning("[Migration] AutoSync s.to->serienstream.to rewrite failed", exc_info=True)
        # Add UNIQUE index on series_url (migration for existing DBs)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_autosync_series_url "
                "ON autosync_jobs (series_url)"
            )
        except sqlite3.IntegrityError:
            # Duplicates already exist — deduplicate keeping the lowest id
            conn.execute(
                "DELETE FROM autosync_jobs WHERE id NOT IN "
                "(SELECT MIN(id) FROM autosync_jobs GROUP BY series_url)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_autosync_series_url "
                "ON autosync_jobs (series_url)"
            )
        conn.commit()
    finally:
        conn.close()


def add_autosync_job(
    title, series_url, language, provider, custom_path_id=None, added_by=None,
    path_unavailable_action="skip", episode_filter=None, movie_custom_path_id=None,
    cover_url: str | None = None,
):
    """Create a new autosync job.

    last_check is set to the current UTC time on creation so the background
    worker does NOT immediately trigger a sync — the first run will happen
    after the configured interval has elapsed.  This prevents duplicate
    queue entries when the user creates a job and then also starts a manual
    download in the same browser session.
    """
    from datetime import datetime
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO autosync_jobs "
            "(title, series_url, language, provider, custom_path_id, added_by, "
            "path_unavailable_action, episode_filter, movie_custom_path_id, cover_url, last_check) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, series_url, language, provider, custom_path_id, added_by,
             path_unavailable_action, episode_filter, movie_custom_path_id, cover_url, now_str),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_autosync_jobs(username=None):
    """Return all sync jobs. If *username* is given, only that user's jobs."""
    conn = get_db()
    try:
        if username:
            rows = conn.execute(
                "SELECT * FROM autosync_jobs WHERE added_by = ? ORDER BY id",
                (username,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM autosync_jobs ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_autosync_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM autosync_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_autosync_by_url(series_url):
    """Return the first sync job that matches *series_url*, or None.
    
    Comparison is normalized: trailing slashes and case are ignored.
    """
    series_url_norm = (series_url or "").rstrip("/").lower()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM autosync_jobs WHERE LOWER(RTRIM(series_url, '/')) = ? LIMIT 1",
            (series_url_norm,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_autosync_job(job_id, **fields):
    """Update arbitrary columns on a sync job."""
    if not fields:
        return
    allowed = {
        "title",
        "series_url",
        "language",
        "provider",
        "custom_path_id",
        "enabled",
        "last_check",
        "last_new_found",
        "episodes_found",
        "local_episodes_found",
        "last_new_count",
        "last_error",
        "on_hold",
        "path_unavailable_action",
        "retry_count",
        "episode_filter",
        "movie_custom_path_id",
        "filter_dirty",
        "group_name",
        "cover_url",
    }
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [job_id]
    conn = get_db()
    try:
        conn.execute(f"UPDATE autosync_jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def remove_autosync_job(job_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM autosync_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return False, "Job not found"
        conn.execute("DELETE FROM autosync_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ===== Download History =====

_CREATE_DOWNLOAD_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    title TEXT NOT NULL,
    series_url TEXT,
    episode_url TEXT,
    season INTEGER,
    episode INTEGER,
    language TEXT,
    provider TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    username TEXT,
    target_path TEXT,
    size_mb REAL,
    avg_speed_mbps REAL,
    duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_download_history_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_DOWNLOAD_HISTORY_TABLE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_history_finished "
            "ON download_history(finished_at DESC)"
        )
        # Migration: add error column for existing DBs
        try:
            conn.execute("ALTER TABLE download_history ADD COLUMN error TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def add_download_history(
    title, *, queue_id=None, series_url=None, episode_url=None, season=None,
    episode=None, language=None, provider=None, source="manual", username=None,
    target_path=None, size_mb=None, avg_speed_mbps=None, duration_sec=None,
    status="completed", error=None, started_at=None, finished_at=None,
):
    """Record a single finished (or failed) episode download. Returns the new id."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO download_history "
            "(queue_id, title, series_url, episode_url, season, episode, language, "
            " provider, source, username, target_path, size_mb, avg_speed_mbps, "
            " duration_sec, status, error, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (queue_id, title, series_url, episode_url, season, episode, language,
             provider, source, username, target_path, size_mb, avg_speed_mbps,
             duration_sec, status, error, started_at, finished_at),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_download_history_meta_for_path(target_path: str):
    """Return {provider, title, season, episode} from the most recent
    download_history row whose target_path matches, or None if no match.

    Used by: telemetry instrumentation in routes/progress.py, to look up
    which provider/title a watched *file path* (all that
    api_progress_save() receives from the player) actually came from --
    needed both for the watch.* event payload and, critically, to apply the
    hanime_tv exclusion guard (sanitize.is_adult_provider()) correctly, since
    provider is not otherwise known at watch-progress time. Best-effort: a
    file played from outside the download history (e.g. manually placed in
    the library) simply yields no provider/title, and the caller treats a
    lookup miss as "unknown provider" (never as "safe to send").
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT provider, title, season, episode FROM download_history "
            "WHERE target_path = ? ORDER BY id DESC LIMIT 1",
            (target_path,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _history_where(username=None, search=None, status=None, source=None, since=None):
    """Build a (where_sql, params) pair shared by list/export/clear."""
    where = []
    params = []
    if username:
        where.append("username = ?")
        params.append(username)
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    if since:
        where.append("COALESCE(finished_at, created_at) >= ?")
        params.append(since)
    if search:
        where.append("title LIKE ?")
        params.append("%" + search + "%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def get_download_history(username=None, search=None, status=None, source=None,
                         since=None, limit=50, offset=0):
    """Return (entries, total). If *username* is given, scope to that user."""
    conn = get_db()
    try:
        where_sql, params = _history_where(username, search, status, source, since)
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_history" + where_sql, params
        ).fetchone()["cnt"]
        rows = conn.execute(
            "SELECT * FROM download_history" + where_sql +
            " ORDER BY COALESCE(finished_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_download_history_entry(entry_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_history WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_download_history_entry(entry_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM download_history WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


def delete_download_history_entries(ids, username=None):
    """Delete multiple history rows by id. If *username* is given, only that
    user's rows are removed. Returns the number of rows deleted."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in ids)
        sql = f"DELETE FROM download_history WHERE id IN ({placeholders})"
        params = list(ids)
        if username:
            sql += " AND username = ?"
            params.append(username)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_download_history(username=None, search=None, status=None, source=None, since=None):
    """Delete history rows, optionally limited to the given filters. Returns the
    number of rows deleted."""
    conn = get_db()
    try:
        where_sql, params = _history_where(username, search, status, source, since)
        cur = conn.execute("DELETE FROM download_history" + where_sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def prune_download_history(days):
    """Delete history rows older than *days* days. days<=0 disables pruning.
    Returns the number of rows deleted."""
    try:
        days = int(days)
    except (ValueError, TypeError):
        return 0
    if days <= 0:
        return 0
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM download_history "
            "WHERE COALESCE(finished_at, created_at) < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ===== Statistics =====


def get_sync_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM autosync_jobs").fetchone()[
            "cnt"
        ]
        enabled = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE enabled = 1"
        ).fetchone()["cnt"]
        disabled = total - enabled
        last_check = conn.execute(
            "SELECT MAX(last_check) AS lc FROM autosync_jobs"
        ).fetchone()["lc"]
        last_new = conn.execute(
            "SELECT MAX(last_new_found) AS ln FROM autosync_jobs"
        ).fetchone()["ln"]
        total_eps = conn.execute(
            "SELECT COALESCE(SUM(episodes_found), 0) AS s FROM autosync_jobs"
        ).fetchone()["s"]
        jobs = conn.execute(
            "SELECT id, title, series_url, language, provider, enabled, "
            "last_check, last_new_found, episodes_found, added_by, created_at "
            "FROM autosync_jobs ORDER BY id"
        ).fetchall()
        return {
            "total_jobs": total,
            "enabled": enabled,
            "disabled": disabled,
            "last_check": last_check,
            "last_new_found": last_new,
            "total_episodes_found": total_eps,
            "jobs": [dict(r) for r in jobs],
        }
    finally:
        conn.close()


def get_queue_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM download_queue").fetchone()[
            "cnt"
        ]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM download_queue GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]
        running = conn.execute(
            "SELECT id, title, current_episode, total_episodes, language, provider, source "
            "FROM download_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            r = dict(running)
            cur = r.get("current_episode") or 0
            tot = r.get("total_episodes") or 0
            r["progress_percent"] = round(cur / tot * 100) if tot > 0 else 0
        else:
            r = None
        return {
            "total": total,
            "by_status": by_status,
            "currently_running": r,
        }
    finally:
        conn.close()


def get_general_stats():
    conn = get_db()
    try:
        total_downloads = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial', 'failed')"
        ).fetchone()["cnt"]
        completed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'completed'"
        ).fetchone()["cnt"]
        failed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'failed'"
        ).fetchone()["cnt"]
        total_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial')"
        ).fetchone()["s"]
        last_24h = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') "
            "AND completed_at >= datetime('now', '-1 day')"
        ).fetchone()["cnt"]
        # Average duration (completed items with both timestamps)
        avg_dur = conn.execute(
            "SELECT AVG("
            "  (julianday(completed_at) - julianday(created_at)) * 86400"
            ") AS avg_s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND completed_at IS NOT NULL"
        ).fetchone()["avg_s"]
        # Most downloaded titles
        top_titles = conn.execute(
            "SELECT title, COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') GROUP BY title "
            "ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        # Episodes per language
        by_language = conn.execute(
            "SELECT language, COUNT(*) AS cnt, "
            "COALESCE(SUM(total_episodes), 0) AS eps "
            "FROM download_queue WHERE status IN ('completed', 'partial') "
            "GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        # Source breakdown (heuristic by URL)
        anime_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%aniworld.to%'"
        ).fetchone()["cnt"]
        anime_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%aniworld.to%'"
        ).fetchone()["s"]
        series_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%s.to%'"
        ).fetchone()["cnt"]
        series_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%s.to%'"
        ).fetchone()["s"]
        movie_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%filmpalast.to%'"
        ).fetchone()["cnt"]
        movie_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%filmpalast.to%'"
        ).fetchone()["s"]
        # Weekday activity (0=Sunday, 1=Monday, ...)
        weekday_rows = conn.execute(
            "SELECT strftime('%w', completed_at) as weekday, COUNT(*) as cnt "
            "FROM download_queue WHERE status IN ('completed', 'partial') AND completed_at IS NOT NULL "
            "GROUP BY weekday ORDER BY weekday"
        ).fetchall()
        weekday_activity = {r["weekday"]: r["cnt"] for r in weekday_rows}

        # Speed stats
        avg_speed = conn.execute(
            "SELECT AVG(average_speed_mbps) as avg_s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND average_speed_mbps IS NOT NULL"
        ).fetchone()["avg_s"]

        total_size = conn.execute(
            "SELECT SUM(total_size_mb) as s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND total_size_mb IS NOT NULL"
        ).fetchone()["s"]

        # Last 20 speeds for details modal
        last_speeds = conn.execute(
            "SELECT title, average_speed_mbps, total_size_mb, completed_at "
            "FROM download_queue WHERE status IN ('completed', 'partial') AND average_speed_mbps IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 20"
        ).fetchall()

        partial = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'partial'"
        ).fetchone()["cnt"]

        return {
            "total_downloads": total_downloads,
            "completed": completed,
            "failed": failed,
            "partial": partial,
            "total_episodes": total_episodes,
            "last_24h_completed": last_24h,
            "average_duration_seconds": round(avg_dur, 1) if avg_dur else None,
            "weekday_activity": weekday_activity,
            "average_speed_mbps": round(avg_speed, 3) if avg_speed else None,
            "total_size_mb": round(total_size, 1) if total_size else 0.0,
            "last_speeds": [
                {
                    "title": r["title"],
                    "speed": round(r["average_speed_mbps"], 3),
                    "size": round(r["total_size_mb"], 2),
                    "date": r["completed_at"]
                } for r in last_speeds
            ],
            "top_titles": [
                {"title": r["title"], "count": r["cnt"]} for r in top_titles
            ],
            "by_language": [
                {"language": r["language"], "downloads": r["cnt"], "episodes": r["eps"]}
                for r in by_language
            ],
            "anime_downloads": anime_count,
            "anime_episodes": anime_episodes,
            "series_downloads": series_count,
            "series_episodes": series_episodes,
            "movie_downloads": movie_count,
            "movie_files": movie_episodes,
        }
    finally:
        conn.close()


# ===== Favourites =====

_CREATE_FAVOURITES_TABLE = """\
CREATE TABLE IF NOT EXISTS favourites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_url TEXT NOT NULL,
    title TEXT NOT NULL,
    poster_url TEXT,
    added_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(series_url, added_by)
);
"""


def init_favourites_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_FAVOURITES_TABLE)
        conn.commit()
    finally:
        conn.close()


def add_favourite(series_url: str, title: str, poster_url: str | None, added_by: str | None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO favourites (series_url, title, poster_url, added_by) VALUES (?, ?, ?, ?)",
            (series_url, title, poster_url, added_by),
        )
        conn.commit()
    finally:
        conn.close()


def remove_favourite(series_url: str, added_by: str | None):
    # "OR added_by IS NULL" also matches legacy/no-auth rows that have no
    # owner, since SQLite treats NULL as distinct for the UNIQUE(series_url,
    # added_by) constraint and a plain "=" comparison would never match NULL.
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM favourites WHERE series_url = ? AND (added_by = ? OR added_by IS NULL)",
            (series_url, added_by),
        )
        conn.commit()
    finally:
        conn.close()


def get_favourites(added_by: str | None = None):
    conn = get_db()
    try:
        if added_by:
            rows = conn.execute(
                "SELECT * FROM favourites WHERE added_by = ? ORDER BY created_at DESC",
                (added_by,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM favourites ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_favourite(series_url: str, added_by: str | None) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM favourites WHERE series_url = ? AND (added_by = ? OR added_by IS NULL) LIMIT 1",
            (series_url, added_by),
        ).fetchone()
        return row is not None
    finally:
        conn.close()

# ============================================================
# Seerr hidden requests
# ============================================================

_CREATE_SEERR_HIDDEN_TABLE = """
CREATE TABLE IF NOT EXISTS seerr_hidden (
    user_id INTEGER NOT NULL,
    seerr_request_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    poster_url TEXT NOT NULL DEFAULT '',
    hidden_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, seerr_request_id)
);
"""


def init_seerr_hidden_db():
    conn = get_db()
    try:
        conn.execute(_CREATE_SEERR_HIDDEN_TABLE)
        conn.commit()
    finally:
        conn.close()


def hide_seerr_request(user_id: int, seerr_request_id: int, title: str = "", poster_url: str = "") -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO seerr_hidden (user_id, seerr_request_id, title, poster_url) VALUES (?, ?, ?, ?)",
            (user_id, seerr_request_id, title, poster_url),
        )
        conn.commit()
    finally:
        conn.close()


def unhide_seerr_request(user_id: int, seerr_request_id: int) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM seerr_hidden WHERE user_id = ? AND seerr_request_id = ?",
            (user_id, seerr_request_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_hidden_seerr_request_ids(user_id: int) -> set:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT seerr_request_id FROM seerr_hidden WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def get_hidden_seerr_requests(user_id: int) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT seerr_request_id, title, poster_url, hidden_at FROM seerr_hidden WHERE user_id = ? ORDER BY hidden_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# Library cache
# ============================================================

def init_library_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_cache (
                path_key  TEXT PRIMARY KEY,
                data      TEXT NOT NULL DEFAULT '[]',
                scanned_at REAL NOT NULL DEFAULT 0,
                is_scanning INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_all_library_cache():
    import json as _json
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT path_key, data, scanned_at, is_scanning FROM library_cache"
        ).fetchall()
        return {
            r["path_key"]: {
                "data": _json.loads(r["data"]),
                "scanned_at": r["scanned_at"],
                "is_scanning": bool(r["is_scanning"]),
            }
            for r in rows
        }
    finally:
        conn.close()


def set_library_cache(path_key, data, scanned_at=None):
    import json as _json, time as _time
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO library_cache (path_key, data, scanned_at, is_scanning)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(path_key) DO UPDATE SET
                   data       = excluded.data,
                   scanned_at = excluded.scanned_at,
                   is_scanning = 0""",
            (path_key, _json.dumps(data), scanned_at or _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def set_library_scanning(path_key, is_scanning: bool):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE library_cache SET is_scanning=? WHERE path_key=?",
            (int(is_scanning), path_key),
        )
        if not conn.execute("SELECT changes()").fetchone()[0]:
            conn.execute(
                "INSERT INTO library_cache (path_key, data, scanned_at, is_scanning) VALUES (?, '[]', 0, ?)",
                (path_key, int(is_scanning)),
            )
        conn.commit()
    finally:
        conn.close()


def invalidate_library_cache():
    """Mark all cache entries as stale (scanned_at=0) so next call triggers a rescan."""
    conn = get_db()
    try:
        conn.execute("UPDATE library_cache SET scanned_at=0")
        conn.commit()
    finally:
        conn.close()


# ============================================================
# App settings (persistent key-value store)
# ============================================================

def init_media_ignored_db():
    """Table that stores missing media slots the user chose to ignore.

    A row is (folder, slot): `folder` is the lower-cased series folder name
    (matching the merge key used by the Media statistics), `slot` is either a
    specific missing slot like "S1E3" / a whole missing season like "S2", or
    the sentinel "__all__" meaning the entire series is ignored. Ignored slots
    are subtracted from a series' missing list when computing statistics, so a
    series whose remaining gaps are all ignored counts as complete."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_ignored (
                folder     TEXT NOT NULL,
                slot       TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (folder, slot)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def add_media_ignores(folder: str, slots, title: str = "") -> None:
    """Mark one or more missing slots as ignored for a series folder.

    `slots` may be a single slot string or a list. Use the sentinel "__all__"
    to ignore the whole series."""
    import time as _time
    if not folder:
        return
    folder = folder.lower()
    if isinstance(slots, str):
        slots = [slots]
    conn = get_db()
    try:
        now = _time.time()
        for slot in slots:
            if not slot:
                continue
            conn.execute(
                """INSERT INTO media_ignored (folder, slot, title, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(folder, slot) DO UPDATE SET title = excluded.title""",
                (folder, str(slot), title or "", now),
            )
        conn.commit()
    finally:
        conn.close()


def remove_media_ignore(folder: str, slot: str = None, all_slots: bool = False) -> None:
    """Remove a single ignored slot, or all ignored slots for a folder."""
    if not folder:
        return
    folder = folder.lower()
    conn = get_db()
    try:
        if all_slots or slot is None:
            conn.execute("DELETE FROM media_ignored WHERE folder = ?", (folder,))
        else:
            conn.execute(
                "DELETE FROM media_ignored WHERE folder = ? AND slot = ?",
                (folder, str(slot)),
            )
        conn.commit()
    finally:
        conn.close()


def get_media_ignores() -> dict:
    """Return {folder_lower: {"title": str, "slots": set(...)}}."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT folder, slot, title FROM media_ignored"
        ).fetchall()
        out = {}
        for r in rows:
            entry = out.setdefault(r["folder"], {"title": "", "slots": set()})
            entry["slots"].add(r["slot"])
            if r["title"]:
                entry["title"] = r["title"]
        return out
    finally:
        conn.close()


def init_app_settings_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

    _migrate_plaintext_admin_password()
    _migrate_sensitive_settings()


def _migrate_sensitive_settings():
    """Re-encrypt any core sensitive settings that are still stored as plaintext.

    Only covers SENSITIVE_KEYS: runtime-registered module keys aren't known yet
    at DB-init time and are migrated by register_sensitive_keys() instead, when
    the module that owns them registers.
    """
    _encrypt_existing_plaintext(SENSITIVE_KEYS)


def _migrate_plaintext_admin_password():
    """Remove any plaintext admin password that was previously stored in app_settings.
    If no admin account exists yet, create one from the stored credentials first."""
    conn = get_db()
    try:
        # Check whether the app_settings table exists at all (very first run)
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
        ).fetchone()
        if not tbl:
            return

        stored_user = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'web_admin_user'"
        ).fetchone()
        stored_pass = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'web_admin_pass'"
        ).fetchone()

        if not stored_pass:
            return  # Nothing to migrate

        plaintext_pass = stored_pass["value"]
        plaintext_user = stored_user["value"] if stored_user else ""

        # If there is no admin yet and we have credentials, create the admin properly
        admin_exists = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
        ).fetchone()["cnt"] > 0

        if not admin_exists and plaintext_user and plaintext_pass:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (plaintext_user, generate_password_hash(plaintext_pass), "admin"),
                )
                conn.commit()
                logger.info(
                    "Migrated plaintext admin credentials to hashed user account '%s'",
                    plaintext_user,
                )
            except Exception:
                logger.warning("Could not migrate plaintext admin credentials", exc_info=True)

        # Always remove the plaintext values from app_settings
        conn.execute("DELETE FROM app_settings WHERE key IN ('web_admin_pass', 'web_admin_user')")
        conn.commit()
        logger.info("Removed plaintext admin credentials from settings storage")
    except Exception:
        logger.warning("Error during admin credentials cleanup", exc_info=True)
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        val = row["value"]
        # Decrypt when the key is registered as sensitive *or* when the stored
        # value carries the encryption prefix: a module that registered a key
        # via register_sensitive_keys() and was later disabled/uninstalled
        # leaves an encrypted value behind, and reading it back must not hand
        # out the ciphertext just because nothing registered the key this run.
        if is_sensitive_key(key) or (val or "").startswith(_ENC_PREFIX):
            val = _decrypt_value(val)
        return val
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    try:
        stored = _encrypt_value(value) if is_sensitive_key(key) else value
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, stored),
        )
        conn.commit()
    finally:
        conn.close()
    _notify_setting_listeners(key, value)


# ---------------------------------------------------------------------------
# Setting-change listeners
# ---------------------------------------------------------------------------
#
# So that "a setting changed" can be an event instead of something every
# interested party polls for. web/thirdparties/ subscribes exactly once and
# turns a write to "module:<id>:<key>" into that module's on_settings_changed()
# hook plus a restart of its background worker -- which is what every module
# with a bot was hand-rolling as a config poll on a 20-second timer.
#
# Listeners are called AFTER the value is committed (so a listener that reads
# the setting back sees the new one) and never inside the DB transaction.

_SETTING_LISTENERS = []


def add_setting_listener(fn) -> None:
    """Call ``fn(key, value)`` after every successful set_setting().

    `value` is the plaintext that was passed in, not what is stored (a sensitive
    key is encrypted at rest, and a listener has no business decrypting it just
    to be told what it already got).

    A listener must not raise and must be quick -- it runs on the thread that
    saved the setting, i.e. usually inside an HTTP request. Anything slow
    (restarting a bot) belongs on a thread of the listener's own; see
    web/thirdparties/__init__.py's _on_setting_changed(), which does exactly
    that.
    """
    if callable(fn) and fn not in _SETTING_LISTENERS:
        _SETTING_LISTENERS.append(fn)


def _notify_setting_listeners(key: str, value: str) -> None:
    """Fire every listener, swallowing (but logging) whatever they raise: a
    module with a broken handler must not be able to make saving a setting
    fail."""
    for fn in list(_SETTING_LISTENERS):
        try:
            fn(key, value)
        except Exception:
            logger.warning("Setting listener %r failed for key %r", fn, key, exc_info=True)



def get_encoding_ffmpeg_opts():
    """Read the encoding_* app_settings and build a dict with vcodec, acodec,
    vopts ready for ffmpeg.output() kwargs.

    Structure:
        {
            "vcodec": str | None,   # None means expert flags override via vopts
            "acodec": str | None,
            "vopts":  dict,         # extra encoder kwargs (preset, crf, etc.)
        }

    Note: as of this audit, no other module in the repo calls this function
    (grepped the whole tree) — routes/encoding.py reads/writes the same
    encoding_* settings directly via get_setting()/set_setting() instead.
    Kept here for whichever download/transcode step is meant to consume it.
    """
    import shlex

    def _parse_expert_flags(flags_str):
        """Parse '-c:v libx265 -preset slow -crf 18' -> dict for ffmpeg-python."""
        if not flags_str:
            return {}
        try:
            tokens = shlex.split(flags_str.strip())
        except Exception:
            return {}
        result = {}
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.startswith("-"):
                key = t.lstrip("-")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    val = tokens[i + 1]
                    try:
                        val = int(val)
                    except ValueError:
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                    result[key] = val
                    i += 2
                else:
                    i += 1
            else:
                i += 1
        return result

    mode = get_setting("encoding_mode", "copy") or "copy"

    if mode == "copy":
        audio = get_setting("encoding_audio_copy", "copy") or "copy"
        audio_map = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        acodec = audio_map.get(audio, "copy")
        return {"vcodec": "copy", "acodec": acodec, "vopts": {}}

    if mode in ("h264", "h265"):
        hw      = get_setting(f"encoding_hw_{mode}", "cpu") or "cpu"
        preset  = get_setting(f"encoding_preset_{mode}", "medium") or "medium"
        crf_def = "23" if mode == "h264" else "28"
        crf     = int(get_setting(f"encoding_crf_{mode}", crf_def) or crf_def)
        audio   = get_setting(f"encoding_audio_{mode}", "copy") or "copy"

        # Map hw + mode -> encoder name
        codec_map = {
            "h264": {
                "cpu":          "libx264",
                "nvenc":        "h264_nvenc",
                "vaapi":        "h264_vaapi",
                "videotoolbox": "h264_videotoolbox",
            },
            "h265": {
                "cpu":          "libx265",
                "nvenc":        "hevc_nvenc",
                "vaapi":        "hevc_vaapi",
                "videotoolbox": "hevc_videotoolbox",
            },
        }
        vcodec = codec_map[mode].get(hw, "libx264" if mode == "h264" else "libx265")

        # Encoder-specific quality options
        vopts = {}
        if hw == "nvenc":
            vopts["preset"] = preset
            vopts["rc"]     = "vbr"
            vopts["cq"]     = crf      # NVENC quality knob
        elif hw == "vaapi":
            vopts["vf"]             = "format=nv12,hwupload"
            vopts["global_quality"] = crf
        elif hw == "videotoolbox":
            pass  # VideoToolbox quality is controlled differently per stream
        else:
            # CPU (libx264 / libx265)
            vopts["preset"] = preset
            vopts["crf"]    = crf

        audio_map = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        acodec = audio_map.get(audio, "copy")
        return {"vcodec": vcodec, "acodec": acodec, "vopts": vopts}

    if mode == "expert":
        video_flags = get_setting("encoding_expert_video", "") or ""
        audio_flags = get_setting("encoding_expert_audio", "") or ""
        vparsed = _parse_expert_flags(video_flags)
        aparsed = _parse_expert_flags(audio_flags)
        # Extract vcodec/acodec from parsed flags if present
        vcodec = vparsed.pop("c:v", vparsed.pop("vcodec", "copy"))
        acodec = aparsed.pop("c:a", aparsed.pop("acodec", "copy"))
        # Merge remaining audio opts into vopts (prefix a: to scope them to audio stream)
        vopts = dict(vparsed)
        for k, v in aparsed.items():
            vopts[f"a:{k}"] = v
        return {"vcodec": vcodec, "acodec": acodec, "vopts": vopts}

    # Fallback
    return {"vcodec": "copy", "acodec": "copy", "vopts": {}}


def delete_setting(key: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def delete_settings_by_prefix(prefix: str) -> int:
    """Delete every app_settings row whose key starts with `prefix`, returning
    how many were removed.

    Exists for uninstalling a thirdparty module: everything a module stores
    lives under the "module:<module_id>:" prefix (see
    web/thirdparties/registry.py's module_setting_key()), so removing the
    module's folder can also remove its settings instead of leaving orphaned
    rows behind forever. `prefix` is escaped for LIKE (a module id containing
    % or _ would otherwise match far more than its own keys) -- note "_" is a
    LIKE wildcard and folder names use it constantly, which is exactly the
    trap this avoids.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM app_settings WHERE key LIKE ? ESCAPE '\\'", (escaped + "%",)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ============================================================
# Notification tables: per-user prefs + push subscriptions
# ============================================================

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


# ---------------------------------------------------------------------------
# Push subscriptions (DB-backed)
# ---------------------------------------------------------------------------

def db_add_push_subscription(
    endpoint: str, auth: str, p256dh: str, user_id: "int | None" = None
) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, user_id, auth, p256dh) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(endpoint) DO UPDATE SET"
            "   user_id = excluded.user_id,"
            "   auth    = excluded.auth,"
            "   p256dh  = excluded.p256dh",
            (endpoint, user_id, auth, p256dh),
        )
        conn.commit()
    finally:
        conn.close()



def db_remove_push_subscription(endpoint):
    conn = get_db()
    try:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()
    finally:
        conn.close()


def db_get_push_subscriptions(user_id=None):
    conn = get_db()
    try:
        if user_id is not None:
            rows = conn.execute(
                "SELECT endpoint, user_id, auth, p256dh FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT endpoint, user_id, auth, p256dh FROM push_subscriptions"
            ).fetchall()
        return [
            {
                "endpoint": r["endpoint"],
                "user_id":  r["user_id"],
                "keys":     {"auth": r["auth"], "p256dh": r["p256dh"]},
            }
            for r in rows
        ]
    finally:
        conn.close()


# ============================================================
# TMDB / CineInfo result cache (persistent, 24 h TTL)
# ============================================================

def init_tmdb_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tmdb_cache (
                cache_key  TEXT    PRIMARY KEY,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL
            )
            """
        )
        # Remove expired entries so the table does not grow unboundedly
        conn.execute(
            "DELETE FROM tmdb_cache WHERE cached_at < strftime('%s', 'now') - 86400"
        )
        conn.commit()
    finally:
        conn.close()


def get_tmdb_cache(cache_key: str, ttl: float = 86400.0) -> "dict | None":
    """Return cached TMDB data if it exists and is within TTL, else None."""
    import time as _time
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM tmdb_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row and (_time.time() - row["cached_at"]) < ttl:
            import json as _json
            return _json.loads(row["data_json"])
        return None
    finally:
        conn.close()


def get_tmdb_cache_bulk(cache_keys: list, ttl: float = 86400.0) -> dict:
    """Return dict mapping cache_key -> parsed JSON data for keys within TTL."""
    if not cache_keys:
        return {}
    import time as _time
    import json as _json
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in cache_keys)
        rows = conn.execute(
            f"SELECT cache_key, data_json, cached_at FROM tmdb_cache WHERE cache_key IN ({placeholders})",
            cache_keys,
        ).fetchall()
        out = {}
        now = _time.time()
        for row in rows:
            if (now - row["cached_at"]) < ttl:
                try:
                    out[row["cache_key"]] = _json.loads(row["data_json"])
                except Exception:
                    pass
        return out
    finally:
        conn.close()


def set_tmdb_cache(cache_key: str, data: dict) -> None:
    """Persist a TMDB result. Upserts so repeated calls refresh the TTL."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO tmdb_cache (cache_key, data_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                cached_at = excluded.cached_at
            """,
            (cache_key, _json.dumps(data, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_tmdb_cache() -> None:
    """Wipe all cached TMDB entries (e.g. after API-key change)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM tmdb_cache")
        conn.commit()
    finally:
        conn.close()


def evict_tmdb_cache(ttl: float = 86400.0) -> int:
    """Delete entries older than *ttl* seconds. Returns number of rows removed."""
    import time as _time
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM tmdb_cache WHERE cached_at < ?",
            (_time.time() - ttl,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============================================================
# Generic provider-availability cache (persistent, 24 h TTL)
# ============================================================
# Same shape/behaviour as the TMDB cache above, but namespaced so several
# independent providers (Crunchyroll, Fernsehserien.de, ...) can share one
# table without key collisions. Used so pill lookups survive a restart
# instead of living only in a process-memory dict.

def init_provider_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_cache (
                namespace  TEXT    NOT NULL,
                cache_key  TEXT    NOT NULL,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            )
            """
        )
        # Remove expired entries so the table does not grow unboundedly
        conn.execute(
            "DELETE FROM provider_cache WHERE cached_at < strftime('%s', 'now') - 86400"
        )
        conn.commit()
    finally:
        conn.close()


def get_provider_cache(namespace: str, cache_key: str, ttl: float = 86400.0) -> "dict | None":
    """Return cached provider data if it exists and is within TTL, else None."""
    import time as _time
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM provider_cache WHERE namespace = ? AND cache_key = ?",
            (namespace, cache_key),
        ).fetchone()
        if row and (_time.time() - row["cached_at"]) < ttl:
            import json as _json
            return _json.loads(row["data_json"])
        return None
    finally:
        conn.close()


def set_provider_cache(namespace: str, cache_key: str, data: dict) -> None:
    """Persist a provider-lookup result. Upserts so repeated calls refresh the TTL."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO provider_cache (namespace, cache_key, data_json, cached_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                cached_at = excluded.cached_at
            """,
            (namespace, cache_key, _json.dumps(data, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_provider_cache(namespace: "str | None" = None) -> None:
    """Wipe cached entries for *namespace* (e.g. after credential changes), or all if None."""
    conn = get_db()
    try:
        if namespace is None:
            conn.execute("DELETE FROM provider_cache")
        else:
            conn.execute("DELETE FROM provider_cache WHERE namespace = ?", (namespace,))
        conn.commit()
    finally:
        conn.close()


def evict_provider_cache(ttl: float = 86400.0) -> int:
    """Delete entries older than *ttl* seconds across all namespaces."""
    import time as _time
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM provider_cache WHERE cached_at < ?",
            (_time.time() - ttl,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ===========================================================================
# Calendar Watcher database tables & helpers
# ===========================================================================

def init_calendar_db() -> None:
    conn = get_db()
    try:
        # title    = primary/German display string
        # title_en = English display string (NULL until the watcher fills it)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_media (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id      INTEGER NOT NULL UNIQUE,
                title        TEXT    NOT NULL,
                title_en     TEXT,
                poster_path  TEXT,
                last_updated REAL    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_episodes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id  INTEGER NOT NULL REFERENCES calendar_media(id) ON DELETE CASCADE,
                season    INTEGER, -- NULL for movies
                episode   INTEGER, -- NULL for movies
                name      TEXT,
                name_en   TEXT,
                air_date  TEXT,    -- YYYY-MM-DD
                still_path TEXT,
                UNIQUE(media_id, season, episode)
            )
            """
        )
        # Migrations for existing DBs (add the English columns if missing)
        for stmt in (
            "ALTER TABLE calendar_media ADD COLUMN title_en TEXT",
            "ALTER TABLE calendar_episodes ADD COLUMN name_en TEXT",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def save_calendar_media(tmdb_id: int, title: str, title_en: str, poster_path: str) -> int:
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO calendar_media (tmdb_id, title, title_en, poster_path, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET
                title = excluded.title,
                title_en = excluded.title_en,
                poster_path = excluded.poster_path,
                last_updated = excluded.last_updated
            """,
            (tmdb_id, title, title_en, poster_path, _time.time()),
        )
        row = conn.execute("SELECT id FROM calendar_media WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def save_calendar_episode(media_id: int, season: int, episode: int, name: str, name_en: str, air_date: str, still_path: str) -> None:
    conn = get_db()
    try:
        if season is None and episode is None:
            # Movies have NULL season/episode. SQLite treats NULLs as distinct in
            # UNIQUE constraints, so ON CONFLICT never fires here — replace the
            # existing movie row manually to avoid accumulating duplicates.
            conn.execute(
                "DELETE FROM calendar_episodes WHERE media_id = ? AND season IS NULL AND episode IS NULL",
                (media_id,),
            )
            conn.execute(
                """
                INSERT INTO calendar_episodes (media_id, season, episode, name, name_en, air_date, still_path)
                VALUES (?, NULL, NULL, ?, ?, ?, ?)
                """,
                (media_id, name, name_en, air_date, still_path),
            )
        else:
            conn.execute(
                """
                INSERT INTO calendar_episodes (media_id, season, episode, name, name_en, air_date, still_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id, season, episode) DO UPDATE SET
                    name = excluded.name,
                    name_en = excluded.name_en,
                    air_date = excluded.air_date,
                    still_path = excluded.still_path
                """,
                (media_id, season, episode, name, name_en, air_date, still_path),
            )
        conn.commit()
    finally:
        conn.close()


def delete_calendar_episodes_except(media_id: int, keep_episodes: list) -> None:
    """Delete episodes for a media that are not in the keep list (tuples of (season, episode))."""
    if not keep_episodes:
        conn = get_db()
        try:
            conn.execute("DELETE FROM calendar_episodes WHERE media_id = ?", (media_id,))
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_db()
    try:
        conds = []
        params = [media_id]
        for s, e in keep_episodes:
            if s is None and e is None:
                conds.append("(season IS NULL AND episode IS NULL)")
            else:
                conds.append("(season = ? AND episode = ?)")
                params.extend([s, e])
        
        query = f"DELETE FROM calendar_episodes WHERE media_id = ? AND NOT ({' OR '.join(conds)})"
        conn.execute(query, tuple(params))
        conn.commit()
    finally:
        conn.close()


def get_cached_calendar_media(tmdb_ids: list) -> dict:
    """Return dict mapping tmdb_id -> last_updated time from database."""
    if not tmdb_ids:
        return {}
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in tmdb_ids)
        rows = conn.execute(
            f"SELECT tmdb_id, last_updated FROM calendar_media WHERE tmdb_id IN ({placeholders})",
            tmdb_ids,
        ).fetchall()
        return {row["tmdb_id"]: row["last_updated"] for row in rows}
    finally:
        conn.close()


def get_calendar_episodes_from_db(tmdb_ids: list) -> list:
    """Fetch stored calendar episodes for a list of TMDB IDs."""
    if not tmdb_ids:
        return []
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in tmdb_ids)
        rows = conn.execute(
            f"""
            SELECT m.tmdb_id, m.title, m.title_en, m.poster_path,
                   e.season, e.episode, e.name, e.name_en, e.air_date, e.still_path
            FROM calendar_media m
            JOIN calendar_episodes e ON m.id = e.media_id
            WHERE m.tmdb_id IN ({placeholders})
            """,
            tmdb_ids,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_calendar_media_titles() -> list:
    """Return ``[(tmdb_id, title, title_en, max_air_date)]`` for all stored
    calendar media, where ``max_air_date`` is the latest episode date (or None).

    Lets a caller map a known show title to the TMDB id it's already synced under
    (by any source) so Crunchyroll title resolution can reuse the authoritative id
    instead of a wrong/duplicate one from a blind title search.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT m.tmdb_id, m.title, m.title_en, MAX(e.air_date) AS max_air
            FROM calendar_media m
            LEFT JOIN calendar_episodes e ON m.id = e.media_id
            GROUP BY m.id
            """
        ).fetchall()
        return [(r["tmdb_id"], r["title"], r["title_en"], r["max_air"]) for r in rows]
    finally:
        conn.close()


# ===========================================================================
# Upscale Queue
# ===========================================================================

_CREATE_UPSCALE_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS upscale_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    file_path        TEXT    NOT NULL,
    output_path      TEXT,
    files            TEXT,
    total_files      INTEGER NOT NULL DEFAULT 1,
    current_file_idx INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'queued'
                         CHECK(status IN ('queued','running','completed','failed','cancelled')),
    progress_pct     REAL    NOT NULL DEFAULT 0.0,
    error            TEXT,
    source           TEXT    NOT NULL DEFAULT 'manual',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    position         INTEGER NOT NULL DEFAULT 0
);
"""


def init_upscale_queue_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_UPSCALE_QUEUE_TABLE)
        # Migrate existing DBs: add new columns if missing
        for col, definition in [
            ("files",            "TEXT"),
            ("total_files",      "INTEGER NOT NULL DEFAULT 1"),
            ("current_file_idx", "INTEGER NOT NULL DEFAULT 0"),
            ("position",         "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE upscale_queue ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        # Backfill position = id for rows that still have 0
        conn.execute("UPDATE upscale_queue SET position = id WHERE position = 0")
        conn.commit()
    finally:
        conn.close()


def add_to_upscale_queue(title, file_path, output_path=None, source="manual", files=None):
    """Add one upscale job.
    files: list of {file_path, output_path} for multi-file (batch) jobs.
    When files is set, file_path/output_path are taken from files[0].
    """
    import json as _json
    conn = get_db()
    try:
        if files:
            fp  = files[0]["file_path"]
            out = files[0].get("output_path") or fp
            files_json = _json.dumps(files)
            total = len(files)
        else:
            fp  = str(file_path)
            out = str(output_path) if output_path else fp
            files_json = None
            total = 1
        cur = conn.execute(
            "INSERT INTO upscale_queue (title, file_path, output_path, files, total_files, source) VALUES (?, ?, ?, ?, ?, ?)",
            (title, fp, out, files_json, total, source),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE upscale_queue SET position = ? WHERE id = ?", (new_id, new_id))
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_upscale_queue():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM upscale_queue ORDER BY position ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_upscale_item(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_next_upscale_queued():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM upscale_queue WHERE status = 'queued' ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_next_upscale_queued():
    """Atomically claim the next upscale item and mark it as running.

    Uses BEGIN IMMEDIATE for the same reason as claim_next_queued — prevents
    double-processing when multiple threads call the worker simultaneously.
    Returns the claimed item dict, or None if nothing is available.

    Used by: mediaforge/web/upscale_worker.py (background upscale worker loop).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT id FROM upscale_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT * FROM upscale_queue WHERE status = 'queued' ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        item = dict(row)
        conn.execute(
            "UPDATE upscale_queue SET status = 'running' WHERE id = ?",
            (item["id"],),
        )
        conn.execute("COMMIT")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_upscale_running():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM upscale_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_upscale_status(item_id, status):
    conn = get_db()
    try:
        if status in ("completed", "failed"):
            conn.execute(
                "UPDATE upscale_queue SET status = ?, completed_at = datetime('now') WHERE id = ?",
                (status, item_id),
            )
        else:
            conn.execute(
                "UPDATE upscale_queue SET status = ? WHERE id = ?",
                (status, item_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_upscale_progress(item_id, progress_pct, current_file_idx=None):
    conn = get_db()
    try:
        if current_file_idx is not None:
            conn.execute(
                "UPDATE upscale_queue SET progress_pct = ?, current_file_idx = ? WHERE id = ?",
                (round(float(progress_pct), 1), current_file_idx, item_id),
            )
        else:
            conn.execute(
                "UPDATE upscale_queue SET progress_pct = ? WHERE id = ?",
                (round(float(progress_pct), 1), item_id),
            )
        conn.commit()
    finally:
        conn.close()


def set_upscale_error(item_id, error_msg):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE upscale_queue SET error = ? WHERE id = ?",
            (str(error_msg), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def remove_from_upscale_queue(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] == "running":
            return False, "Cannot remove a running item (cancel it first)"
        conn.execute("DELETE FROM upscale_queue WHERE id = ?", (item_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def cancel_upscale_item(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] not in ("running", "queued"):
            return False, "Can only cancel queued or running items"
        conn.execute(
            "UPDATE upscale_queue SET status = 'cancelled' WHERE id = ?", (item_id,)
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def is_upscale_cancelled(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        return row and row["status"] == "cancelled"
    finally:
        conn.close()


def clear_upscale_completed():
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM upscale_queue WHERE status IN ('completed', 'failed', 'cancelled')"
        )
        conn.commit()
    finally:
        conn.close()


def get_upscale_badge_count():
    """Return number of queued + running upscale items (for sidebar badge)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM upscale_queue WHERE status IN ('queued', 'running')"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def move_upscale_queue_item(item_id, direction):
    """Swap position of a queued upscale item with its neighbor."""
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT id, position FROM upscale_queue WHERE id = ? AND status = 'queued'",
            (item_id,),
        ).fetchone()
        if not item:
            return False, "Item not found or not queued"
        if direction == "up":
            neighbor = conn.execute(
                "SELECT id, position FROM upscale_queue "
                "WHERE status = 'queued' AND position < ? "
                "ORDER BY position DESC LIMIT 1",
                (item["position"],),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT id, position FROM upscale_queue "
                "WHERE status = 'queued' AND position > ? "
                "ORDER BY position ASC LIMIT 1",
                (item["position"],),
            ).fetchone()
        if not neighbor:
            return False, "Already at edge"
        conn.execute("UPDATE upscale_queue SET position = ? WHERE id = ?", (neighbor["position"], item["id"]))
        conn.execute("UPDATE upscale_queue SET position = ? WHERE id = ?", (item["position"], neighbor["id"]))
        conn.commit()
        return True, None
    finally:
        conn.close()


def reset_running_upscale_items():
    """On startup: reset any stuck 'running' items back to 'queued'."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE upscale_queue SET status = 'queued', progress_pct = 0 WHERE status = 'running'"
        )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Encoding Queue
#
# Mirrors the Upscale Queue above exactly (same columns, same function
# shapes) — see that section's comments for the reasoning. Used to defer
# H.264/H.265 transcoding out of the download queue when
# encoding_timing == "after_download" (see web/encoding_worker.py and
# models/common/common.py's _get_ffmpeg_codec_opts_for_download()).
# ===========================================================================

_CREATE_ENCODING_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS encoding_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    file_path        TEXT    NOT NULL,
    output_path      TEXT,
    files            TEXT,
    total_files      INTEGER NOT NULL DEFAULT 1,
    current_file_idx INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'queued'
                         CHECK(status IN ('queued','running','completed','failed','cancelled')),
    progress_pct     REAL    NOT NULL DEFAULT 0.0,
    error            TEXT,
    source           TEXT    NOT NULL DEFAULT 'manual',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    position         INTEGER NOT NULL DEFAULT 0
);
"""


def init_encoding_queue_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_ENCODING_QUEUE_TABLE)
        conn.execute("UPDATE encoding_queue SET position = id WHERE position = 0")
        conn.commit()
    finally:
        conn.close()


def add_to_encoding_queue(title, file_path, output_path=None, source="manual", files=None):
    """Add one encoding job.
    files: list of {file_path, output_path} for multi-file (batch) jobs.
    When files is set, file_path/output_path are taken from files[0].
    """
    import json as _json
    conn = get_db()
    try:
        if files:
            fp  = files[0]["file_path"]
            out = files[0].get("output_path") or fp
            files_json = _json.dumps(files)
            total = len(files)
        else:
            fp  = str(file_path)
            out = str(output_path) if output_path else fp
            files_json = None
            total = 1
        cur = conn.execute(
            "INSERT INTO encoding_queue (title, file_path, output_path, files, total_files, source) VALUES (?, ?, ?, ?, ?, ?)",
            (title, fp, out, files_json, total, source),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE encoding_queue SET position = ? WHERE id = ?", (new_id, new_id))
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_encoding_queue():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM encoding_queue ORDER BY position ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_encoding_item(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM encoding_queue WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_next_encoding_queued():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM encoding_queue WHERE status = 'queued' ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_next_encoding_queued():
    """Atomically claim the next encoding item and mark it as running.

    Uses BEGIN IMMEDIATE for the same reason as claim_next_upscale_queued —
    prevents double-processing when multiple threads call the worker
    simultaneously. Returns the claimed item dict, or None if nothing is
    available.

    Used by: mediaforge/web/encoding_worker.py (background encoding worker loop).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT id FROM encoding_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT * FROM encoding_queue WHERE status = 'queued' ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        item = dict(row)
        conn.execute(
            "UPDATE encoding_queue SET status = 'running' WHERE id = ?",
            (item["id"],),
        )
        conn.execute("COMMIT")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_encoding_running():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM encoding_queue WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_encoding_status(item_id, status):
    conn = get_db()
    try:
        if status in ("completed", "failed"):
            conn.execute(
                "UPDATE encoding_queue SET status = ?, completed_at = datetime('now') WHERE id = ?",
                (status, item_id),
            )
        else:
            conn.execute(
                "UPDATE encoding_queue SET status = ? WHERE id = ?",
                (status, item_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_encoding_progress(item_id, progress_pct, current_file_idx=None):
    conn = get_db()
    try:
        if current_file_idx is not None:
            conn.execute(
                "UPDATE encoding_queue SET progress_pct = ?, current_file_idx = ? WHERE id = ?",
                (round(float(progress_pct), 1), current_file_idx, item_id),
            )
        else:
            conn.execute(
                "UPDATE encoding_queue SET progress_pct = ? WHERE id = ?",
                (round(float(progress_pct), 1), item_id),
            )
        conn.commit()
    finally:
        conn.close()


def set_encoding_error(item_id, error_msg):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE encoding_queue SET error = ? WHERE id = ?",
            (str(error_msg), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def remove_from_encoding_queue(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM encoding_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] == "running":
            return False, "Cannot remove a running item (cancel it first)"
        conn.execute("DELETE FROM encoding_queue WHERE id = ?", (item_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def cancel_encoding_item(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM encoding_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return False, "Item not found"
        if row["status"] not in ("running", "queued"):
            return False, "Can only cancel queued or running items"
        conn.execute(
            "UPDATE encoding_queue SET status = 'cancelled' WHERE id = ?", (item_id,)
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def is_encoding_cancelled(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM encoding_queue WHERE id = ?", (item_id,)
        ).fetchone()
        return row and row["status"] == "cancelled"
    finally:
        conn.close()


def clear_encoding_completed():
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM encoding_queue WHERE status IN ('completed', 'failed', 'cancelled')"
        )
        conn.commit()
    finally:
        conn.close()


def get_encoding_badge_count():
    """Return number of queued + running encoding items (for sidebar badge)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM encoding_queue WHERE status IN ('queued', 'running')"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def move_encoding_queue_item(item_id, direction):
    """Swap position of a queued encoding item with its neighbor."""
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT id, position FROM encoding_queue WHERE id = ? AND status = 'queued'",
            (item_id,),
        ).fetchone()
        if not item:
            return False, "Item not found or not queued"
        if direction == "up":
            neighbor = conn.execute(
                "SELECT id, position FROM encoding_queue "
                "WHERE status = 'queued' AND position < ? "
                "ORDER BY position DESC LIMIT 1",
                (item["position"],),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT id, position FROM encoding_queue "
                "WHERE status = 'queued' AND position > ? "
                "ORDER BY position ASC LIMIT 1",
                (item["position"],),
            ).fetchone()
        if not neighbor:
            return False, "Already at edge"
        conn.execute("UPDATE encoding_queue SET position = ? WHERE id = ?", (neighbor["position"], item["id"]))
        conn.execute("UPDATE encoding_queue SET position = ? WHERE id = ?", (item["position"], neighbor["id"]))
        conn.commit()
        return True, None
    finally:
        conn.close()


def reset_running_encoding_items():
    """On startup: reset any stuck 'running' items back to 'queued'."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE encoding_queue SET status = 'queued', progress_pct = 0 WHERE status = 'running'"
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# Browse list cache (persistent, survives restart)
# ============================================================

def init_browse_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS browse_cache (
                cache_key  TEXT    PRIMARY KEY,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_browse_cache_stale(cache_key: str) -> "tuple | None":
    """Return (data_list, cached_at) regardless of TTL — for stale-while-revalidate."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM browse_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row:
            import json as _json
            return (_json.loads(row["data_json"]), row["cached_at"])
        return None
    finally:
        conn.close()


def set_browse_cache(cache_key: str, data: list) -> None:
    """Persist browse results. Upserts to refresh the timestamp."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO browse_cache (cache_key, data_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                cached_at = excluded.cached_at
            """,
            (cache_key, _json.dumps(data, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  MediaScan cache
# ─────────────────────────────────────────────────────────────────────────────

def init_mediascan_db() -> None:
    """Create the mediascan_cache table if it does not exist yet."""
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mediascan_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id    TEXT,
                imdb_id    TEXT,
                tvdb_id    TEXT,
                title      TEXT,
                media_type TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mediascan_tmdb ON mediascan_cache (tmdb_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mediascan_imdb ON mediascan_cache (imdb_id)"
        )
        conn.commit()
    finally:
        conn.close()


def replace_mediascan_cache(entries: list) -> None:
    """
    Atomically replace the entire mediascan_cache with *entries*.
    Each entry is a dict with keys: tmdb_id, imdb_id, tvdb_id, title, media_type.
    """
    import time as _time
    now = _time.time()
    conn = get_db()
    try:
        conn.execute("DELETE FROM mediascan_cache")
        conn.executemany(
            """
            INSERT INTO mediascan_cache (tmdb_id, imdb_id, tvdb_id, title, media_type, updated_at)
            VALUES (:tmdb_id, :imdb_id, :tvdb_id, :title, :media_type, :updated_at)
            """,
            [
                {
                    "tmdb_id":    str(e.get("tmdb_id") or "").strip() or None,
                    "imdb_id":    str(e.get("imdb_id") or "").strip() or None,
                    "tvdb_id":    str(e.get("tvdb_id") or "").strip() or None,
                    "title":      str(e.get("title") or "").strip() or None,
                    "media_type": str(e.get("media_type") or "").strip() or None,
                    "updated_at": now,
                }
                for e in entries
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_mediascan_ids() -> dict:
    """Return sets of tmdb_ids, imdb_ids and normalised titles from the mediascan cache."""
    import re as _re
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tmdb_id, imdb_id, title FROM mediascan_cache"
        ).fetchall()
        tmdb_ids = {r["tmdb_id"] for r in rows if r["tmdb_id"]}
        imdb_ids = {r["imdb_id"] for r in rows if r["imdb_id"]}
        # Normalise titles for fuzzy front-end matching:
        # lowercase, strip year/season suffixes, collapse whitespace
        def _norm(t):
            t = (t or "").lower()
            t = _re.sub(r"\s*\(\d{4}\)\s*$", "", t)   # (2013)
            t = _re.sub(r"\s*:?\s*season\s+\d+\s*$", "", t)
            t = _re.sub(r"\s*:?\s*staffel\s+\d+\s*$", "", t)
            t = _re.sub(r"\s*:?\s*part\s+\d+\s*$", "", t)
            t = _re.sub(r"[^\w\s]", "", t)               # strip punctuation
            return " ".join(t.split())
        titles = {_norm(r["title"]) for r in rows if r["title"]}
        titles.discard("")  # remove empty strings
        return {"tmdb_ids": list(tmdb_ids), "imdb_ids": list(imdb_ids), "titles": list(titles)}
    finally:
        conn.close()


def get_mediascan_ids_by_type(media_type: str) -> set:
    """Same tmdb_id set get_mediascan_ids() returns, but scoped to one
    media_type ('movie' or 'tv') via mediascan_cache's own media_type
    column -- get_mediascan_ids() intentionally merges both into one flat
    set for its existing callers (front-end "is this downloaded" checks,
    where the type is already known from context), but a caller that needs
    to look up TMDB detail by id (movie ids and tv ids are separate TMDB
    namespaces, an id can coincidentally exist in both) needs the type-
    scoped version to avoid querying the wrong endpoint for every id --
    see web/thirdparties/mediacalendar/service.py's _resolve_library()."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tmdb_id FROM mediascan_cache WHERE media_type = ? AND tmdb_id IS NOT NULL AND tmdb_id != ''",
            (media_type,),
        ).fetchall()
        return {r["tmdb_id"] for r in rows}
    finally:
        conn.close()


def get_mediascan_count() -> int:
    """Return the number of entries in the mediascan cache."""
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM mediascan_cache").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def get_mediascan_last_updated() -> "float | None":
    """Return the most recent updated_at timestamp, or None if cache is empty."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS ts FROM mediascan_cache"
        ).fetchone()
        return row["ts"] if row and row["ts"] else None
    finally:
        conn.close()


def clear_mediascan_cache() -> None:
    """Wipe all mediascan entries."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM mediascan_cache")
        conn.commit()
    finally:
        conn.close()


def get_mediascan_series() -> list:
    """Return all series from mediascan_cache as a list of dicts with tmdb_id, title, imdb_id."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tmdb_id, imdb_id, title FROM mediascan_cache WHERE media_type = 'tv'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()



# ============================================================
# Watch Progress
# ============================================================

# Watch progress is tracked per user. ``username == ""`` represents the
# single-user / no-auth case (and is also where legacy, pre-per-user rows are
# migrated to), so existing setups keep their resume positions unchanged.
_CREATE_WATCH_PROGRESS_TABLE = """
CREATE TABLE IF NOT EXISTS watch_progress (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL DEFAULT '',
    file_path        TEXT    NOT NULL,
    position_seconds REAL    NOT NULL DEFAULT 0,
    duration_seconds REAL    NOT NULL DEFAULT 0,
    watched          INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(file_path, username)
)
"""


def _normalize_user(username) -> str:
    """Map any falsy user (None, anonymous) to the shared '' bucket."""
    return str(username) if username else ""


def init_watch_progress_db() -> None:
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_WATCH_PROGRESS_TABLE)
        conn.commit()
        # ── Migrate legacy schema (UNIQUE on file_path, no username column) ──
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(watch_progress)").fetchall()]
        if "username" not in cols:
            # Rebuild the table with the new per-user schema, assigning all
            # existing rows to the shared '' user.
            conn.execute("ALTER TABLE watch_progress RENAME TO watch_progress_legacy")
            conn.execute(_CREATE_WATCH_PROGRESS_TABLE)
            conn.execute(
                """INSERT INTO watch_progress
                       (username, file_path, position_seconds, duration_seconds, watched, updated_at)
                   SELECT '', file_path, position_seconds, duration_seconds, watched, updated_at
                   FROM watch_progress_legacy"""
            )
            conn.execute("DROP TABLE watch_progress_legacy")
            conn.commit()
    finally:
        conn.close()


def save_watch_progress(file_path: str, position: float, duration: float, username=None) -> None:
    """Upsert watch position for a file and user. Marks as watched when >= 95%."""
    watched = 1 if duration > 0 and position / duration >= 0.95 else 0
    user = _normalize_user(username)
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO watch_progress (username, file_path, position_seconds, duration_seconds, watched, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(file_path, username) DO UPDATE SET
                   position_seconds = excluded.position_seconds,
                   duration_seconds = excluded.duration_seconds,
                   watched          = excluded.watched,
                   updated_at       = excluded.updated_at""",
            (user, str(file_path), float(position), float(duration), watched),
        )
        conn.commit()
    finally:
        conn.close()


def get_watch_progress(file_path: str, username=None) -> dict:
    """Return progress dict for one file and user. Keys: position, duration, percent, watched."""
    user = _normalize_user(username)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT position_seconds, duration_seconds, watched FROM watch_progress WHERE file_path = ? AND username = ?",
            (str(file_path), user),
        ).fetchone()
        if not row:
            return {"position": 0.0, "duration": 0.0, "percent": 0.0, "watched": False}
        pos  = float(row["position_seconds"])
        dur  = float(row["duration_seconds"])
        pct  = round(pos / dur * 100, 1) if dur > 0 else 0.0
        return {"position": pos, "duration": dur, "percent": pct, "watched": bool(row["watched"])}
    finally:
        conn.close()


def get_watch_progress_bulk(file_paths: list, username=None) -> dict:
    """Return {file_path: progress_dict} for a list of paths, for one user."""
    if not file_paths:
        return {}
    user = _normalize_user(username)
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in file_paths)
        rows = conn.execute(
            f"SELECT file_path, position_seconds, duration_seconds, watched "
            f"FROM watch_progress WHERE username = ? AND file_path IN ({placeholders})",
            [user, *file_paths],
        ).fetchall()
        result = {}
        for row in rows:
            pos = float(row["position_seconds"])
            dur = float(row["duration_seconds"])
            pct = round(pos / dur * 100, 1) if dur > 0 else 0.0
            result[row["file_path"]] = {
                "position": pos, "duration": dur,
                "percent": pct, "watched": bool(row["watched"]),
            }
        return result
    finally:
        conn.close()


# ── UpTime monitoring ─────────────────────────────────────────────────────────
_CREATE_UPTIME_TABLE = """
CREATE TABLE IF NOT EXISTS uptime_heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    response_ms INTEGER,
    http_status INTEGER,
    message     TEXT
)
"""
_CREATE_UPTIME_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_uptime_source_ts "
    "ON uptime_heartbeats(source, ts)"
)


def init_uptime_db():
    """Create the uptime_heartbeats table used by the UpTime monitor."""
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_UPTIME_TABLE)
        conn.execute(_CREATE_UPTIME_INDEX)
        conn.commit()
    finally:
        conn.close()


def record_uptime_heartbeat(source, status, response_ms=None,
                            http_status=None, message=None, ts=None):
    """Persist a single heartbeat. status is 'up' | 'degraded' | 'down'."""
    import time as _t
    if ts is None:
        ts = int(_t.time())
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO uptime_heartbeats "
            "(source, ts, status, response_ms, http_status, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, int(ts), status, response_ms, http_status, message),
        )
        conn.commit()
    finally:
        conn.close()


def prune_uptime_heartbeats(retention_days):
    """Delete heartbeats older than the retention window (and orphan sources)."""
    import time as _t
    try:
        days = float(retention_days)
    except (TypeError, ValueError):
        days = 7.0
    cutoff = int(_t.time()) - int(days * 86400)
    conn = get_db()
    try:
        conn.execute("DELETE FROM uptime_heartbeats WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def get_uptime_summary(source, since_ts, bar_limit=50):
    """Return {stats, latest, bars} for one source over [since_ts, now].

    stats: total, up_count (status != 'down'), avg_ms
    latest: most recent heartbeat (any time) or None
    bars: last ``bar_limit`` heartbeats within the window, oldest first
    """
    conn = get_db()
    try:
        stat = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'down' THEN 0 ELSE 1 END) AS up_count, "
            "AVG(response_ms) AS avg_ms "
            "FROM uptime_heartbeats WHERE source = ? AND ts >= ?",
            (source, since_ts),
        ).fetchone()
        latest = conn.execute(
            "SELECT ts, status, response_ms, http_status, message "
            "FROM uptime_heartbeats WHERE source = ? ORDER BY ts DESC LIMIT 1",
            (source,),
        ).fetchone()
        bars = conn.execute(
            "SELECT ts, status, response_ms, message FROM uptime_heartbeats "
            "WHERE source = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (source, since_ts, int(bar_limit)),
        ).fetchall()
        total = (stat["total"] if stat else 0) or 0
        up_count = (stat["up_count"] if stat else 0) or 0
        avg_ms = stat["avg_ms"] if stat and stat["avg_ms"] is not None else None
        return {
            "stats": {
                "total": total,
                "up_count": up_count,
                "uptime_pct": round(up_count / total * 100, 2) if total else None,
                "avg_ms": round(avg_ms) if avg_ms is not None else None,
            },
            "latest": dict(latest) if latest else None,
            "bars": [dict(r) for r in reversed(bars)],
        }
    finally:
        conn.close()


def get_uptime_range(source, start_ts, end_ts, n_buckets=50):
    """Aggregate heartbeats of one source over [start_ts, end_ts) into
    ``n_buckets`` equal time buckets (for the UpTime history bars).

    Returns {stats, latest, buckets, bucket_seconds}. Each bucket:
      {start, end, status, total, avg_ms, msg, issue_ts}
    status is 'up' | 'degraded' | 'down' | 'nodata' (empty bucket).
    stats (uptime_pct/avg_ms/total) are over the whole selected range;
    latest is the globally most recent heartbeat (independent of range).
    """
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    if end_ts <= start_ts:
        end_ts = start_ts + 1
    n_buckets = max(1, int(n_buckets))
    span = end_ts - start_ts
    size = max(1, span // n_buckets)

    conn = get_db()
    try:
        agg = {}
        for r in conn.execute(
            "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS downc, "
            "SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degc, "
            "SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) AS upc, "
            "SUM(response_ms) AS rt_sum, "
            "SUM(CASE WHEN response_ms IS NOT NULL THEN 1 ELSE 0 END) AS rt_n "
            "FROM uptime_heartbeats WHERE source=? AND ts>=? AND ts<? GROUP BY b",
            (start_ts, size, source, start_ts, end_ts),
        ).fetchall():
            idx = r["b"]
            idx = 0 if idx < 0 else (n_buckets - 1 if idx >= n_buckets else idx)
            a = agg.setdefault(idx, {"total": 0, "down": 0, "deg": 0, "up": 0, "rt_sum": 0, "rt_n": 0})
            a["total"] += r["total"] or 0
            a["down"] += r["downc"] or 0
            a["deg"] += r["degc"] or 0
            a["up"] += r["upc"] or 0
            a["rt_sum"] += r["rt_sum"] or 0
            a["rt_n"] += r["rt_n"] or 0

        issues = {}
        for r in conn.execute(
            "SELECT ts, status, message FROM uptime_heartbeats "
            "WHERE source=? AND ts>=? AND ts<? AND status!='up' ORDER BY ts ASC",
            (source, start_ts, end_ts),
        ).fetchall():
            idx = (r["ts"] - start_ts) // size
            idx = 0 if idx < 0 else (n_buckets - 1 if idx >= n_buckets else idx)
            issues[idx] = {"ts": r["ts"], "status": r["status"], "message": r["message"]}

        buckets = []
        for i in range(n_buckets):
            b_start = start_ts + i * size
            b_end = end_ts if i == n_buckets - 1 else start_ts + (i + 1) * size
            a = agg.get(i)
            if not a or a["total"] == 0:
                buckets.append({"start": b_start, "end": b_end, "status": "nodata",
                                "total": 0, "avg_ms": None, "msg": None, "issue_ts": None})
                continue
            st = "down" if a["down"] else ("degraded" if a["deg"] else ("up" if a["up"] else "nodata"))
            avg = round(a["rt_sum"] / a["rt_n"]) if a["rt_n"] else None
            iss = issues.get(i)
            buckets.append({"start": b_start, "end": b_end, "status": st, "total": a["total"],
                            "avg_ms": avg, "msg": iss["message"] if iss else None,
                            "issue_ts": iss["ts"] if iss else None})

        stat = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='down' THEN 0 ELSE 1 END) AS up_count, "
            "AVG(response_ms) AS avg_ms "
            "FROM uptime_heartbeats WHERE source=? AND ts>=? AND ts<?",
            (source, start_ts, end_ts),
        ).fetchone()
        latest = conn.execute(
            "SELECT ts, status, response_ms, http_status, message "
            "FROM uptime_heartbeats WHERE source=? ORDER BY ts DESC LIMIT 1",
            (source,),
        ).fetchone()
        total = (stat["total"] if stat else 0) or 0
        up_count = (stat["up_count"] if stat else 0) or 0
        avg_ms = stat["avg_ms"] if stat and stat["avg_ms"] is not None else None
        return {
            "stats": {
                "total": total,
                "up_count": up_count,
                "uptime_pct": round(up_count / total * 100, 2) if total else None,
                "avg_ms": round(avg_ms) if avg_ms is not None else None,
            },
            "latest": dict(latest) if latest else None,
            "buckets": buckets,
            "bucket_seconds": size,
        }
    finally:
        conn.close()


def get_uptime_heartbeats_between(source, start_ts, end_ts, limit=1000):
    """Raw heartbeats for one source within [start_ts, end_ts] (detail view)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ts, status, response_ms, http_status, message "
            "FROM uptime_heartbeats WHERE source=? AND ts>=? AND ts<=? "
            "ORDER BY ts ASC LIMIT ?",
            (source, int(start_ts), int(end_ts), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Dev Infos (remote changelog/status feed) ──────────────────────────────────
_CREATE_DEVINFO_TABLE = """
CREATE TABLE IF NOT EXISTS devinfo_posts (
    id                TEXT    PRIMARY KEY,
    title             TEXT,
    body              TEXT,
    type              TEXT,
    author            TEXT,
    remote_created_at TEXT,
    fetched_at        INTEGER
)
"""

# Read-state lives in its own table, deliberately separate from devinfo_posts:
# replace_devinfo_posts() below does a full DELETE + reinsert of that table on
# every poll round (every 5 min, plus on-page-visit), so a "read" flag stored
# as a column on devinfo_posts would get silently wiped the next time the feed
# refreshes. Keying this table by the post's own id (not a local rowid) means
# a read post stays read across those wipes, as long as the remote server
# keeps handing back the same id for it.
_CREATE_DEVINFO_READ_TABLE = """
CREATE TABLE IF NOT EXISTS devinfo_read (
    id      TEXT    PRIMARY KEY,
    read_at INTEGER
)
"""


def init_devinfos_db():
    """Create the devinfo_posts + devinfo_read tables used by the Dev Info feed."""
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_DEVINFO_TABLE)
        conn.execute(_CREATE_DEVINFO_READ_TABLE)
        # Migration: add author column for existing DBs (this table predates the
        # devInfo server exposing who wrote each post via /api/posts).
        try:
            conn.execute("ALTER TABLE devinfo_posts ADD COLUMN author TEXT")
        except Exception:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def replace_devinfo_posts(posts):
    """Replace the entire cached Dev Info post set with a fresh batch.

    Small, low-frequency dataset fetched wholesale from the remote server, so
    a clear-and-reinsert transaction is simpler and just as correct as an
    upsert-by-id. ``posts`` is a list of dicts with keys: id, title, body,
    type, author, remote_created_at (already mapped from the remote payload's
    ``created_at`` by the caller).

    Also prunes devinfo_read down to only the ids still present in this batch
    -- otherwise a post that's gone (deleted upstream, or an old id that will
    never come back) leaves a permanent, pointless row behind.
    """
    import time as _t
    now = int(_t.time())
    posts = posts or []
    conn = get_db()
    try:
        conn.execute("DELETE FROM devinfo_posts")
        for p in posts:
            conn.execute(
                "INSERT OR REPLACE INTO devinfo_posts "
                "(id, title, body, type, author, remote_created_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(p.get("id")),
                    p.get("title"),
                    p.get("body"),
                    p.get("type"),
                    p.get("author"),
                    p.get("remote_created_at"),
                    now,
                ),
            )
        current_ids = [str(p.get("id")) for p in posts]
        if current_ids:
            placeholders = ",".join("?" * len(current_ids))
            conn.execute(
                f"DELETE FROM devinfo_read WHERE id NOT IN ({placeholders})",
                current_ids,
            )
        else:
            conn.execute("DELETE FROM devinfo_read")
        conn.commit()
    finally:
        conn.close()


def get_devinfo_posts():
    """Return all cached Dev Info posts as a list of dicts, newest first.

    Each dict includes ``is_read`` (bool) from a LEFT JOIN against
    devinfo_read -- a post with no matching row there is unread.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT p.id, p.title, p.body, p.type, p.author, p.remote_created_at, "
            "p.fetched_at, (r.id IS NOT NULL) AS is_read "
            "FROM devinfo_posts p LEFT JOIN devinfo_read r ON r.id = p.id "
            "ORDER BY p.remote_created_at DESC, p.fetched_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["is_read"] = bool(d["is_read"])
            out.append(d)
        return out
    finally:
        conn.close()


def get_devinfo_count():
    """Return the number of *unread* cached Dev Info posts (for the sidebar badge)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM devinfo_posts p "
            "WHERE p.id NOT IN (SELECT id FROM devinfo_read)"
        ).fetchone()
        return (row["n"] if row else 0) or 0
    finally:
        conn.close()


def mark_devinfo_read(post_id) -> bool:
    """Mark a single Dev Info post as read. Idempotent -- marking an already-read
    (or nonexistent) id again is a harmless no-op.

    Returns True if the post id exists in devinfo_posts (so the caller can
    tell a real post from a stale/garbage id), False otherwise -- the read
    row is inserted either way, since a post that arrives moments later with
    that id shouldn't un-hide itself as unread.

    Used by: routes/devinfos.py's POST /api/devinfos/<id>/read.
    """
    import time as _t
    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM devinfo_posts WHERE id = ?", (str(post_id),)
        ).fetchone() is not None
        conn.execute(
            "INSERT OR IGNORE INTO devinfo_read (id, read_at) VALUES (?, ?)",
            (str(post_id), int(_t.time())),
        )
        conn.commit()
        return exists
    finally:
        conn.close()
