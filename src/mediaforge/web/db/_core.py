"""Connection handling, the instance lock, and secret encryption.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import os
import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

logger = get_logger(__name__)

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
    "opensubtitles_api_key",
    "opensubtitles_password",
    "comicvine_api_key",
    # The PIN that guards leaving a restricted home mode. A short secret, but
    # a secret: stored encrypted like every other one, and never sent back to
    # the client (the settings form writes it, it never reads it).
    "home_kids_pin",
    # Per-install device secret for signed telemetry requests. Issued once by
    # the devInfo server and never re-issued, so losing it means re-enrolling;
    # leaking it means somebody else can post telemetry as this install and
    # request the deletion of its data. Encrypted at rest like every other
    # secret, and never returned by GET /api/settings/telemetry.
    "telemetry_device_secret",
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
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user', 'kids')),
    auth_method TEXT NOT NULL DEFAULT 'local',
    sso_subject TEXT,
    sso_issuer TEXT,
    -- Added later by _migrate_db()'s ALTER for databases that predate it, and
    -- listed here too so a table CREATED from this statement has the same
    -- shape as a migrated one. It was missing, and _migrate_role_check()'s
    -- rebuild then tried to copy a `language` column into a table that had
    -- none -- which is how that migration failed.
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SSO_INDEX = """\
CREATE UNIQUE INDEX IF NOT EXISTS idx_sso_identity
ON users (sso_issuer, sso_subject)
WHERE sso_issuer IS NOT NULL AND sso_subject IS NOT NULL;
"""


def _configure_connection(conn):
    """Apply the per-connection PRAGMAs. Every connection gets the same set.

    synchronous was never set, so SQLite used its default (FULL): an fsync on
    every single commit, 5-20 ms on an HDD, a NAS or a Docker volume. With WAL
    enabled, NORMAL is still crash-safe for the application (only an OS-level
    crash can lose the very last transaction) and removes that cost from the
    workers, which commit constantly.

    foreign_keys is deliberately NOT set here, even though the schema declares
    two ON DELETE CASCADE constraints (user_notification_prefs -> users and
    calendar_episodes -> calendar_media) that therefore never fire. Turning it
    on globally breaks no-auth mode: init_db() -- the only thing that creates
    the users table -- runs only when auth is enabled, while no-auth requests
    run as the pseudo-user id 0 (see app.py's _set_noauth_session) and save
    notification prefs for it. With enforcement on, that INSERT fails with
    "no such table: main.users", or with "FOREIGN KEY constraint failed" once
    the table does exist, because no users row 0 is ever created.

    Enabling it needs the FK on user_notification_prefs dropped first (a table
    rebuild, exactly like user_ui_prefs which documents the same pseudo-user
    problem as its reason for having no FK). Until then every caller must clean
    up per-user rows explicitly -- see delete_user().
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-16000")   # 16 MB page cache, negative = KiB
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


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
                g.db_conn = _configure_connection(sqlite3.connect(
                    str(DB_PATH), timeout=30, check_same_thread=False,
                    factory=ContextConnection))
            return g.db_conn
    except Exception:
        pass

    return _configure_connection(sqlite3.connect(
        str(DB_PATH), timeout=30, check_same_thread=False,
        factory=ContextConnection))


# How many values go into one `IN (...)` list. SQLite caps the number of bound
# variables per statement (SQLITE_MAX_VARIABLE_NUMBER, 999 on the builds Python
# still ships on several platforms) and caps expression depth at 1000. Both
# limits are compile-time options of SQLite, so the only fix available here is
# to keep every generated statement a fixed size regardless of how much data it
# covers. 400 leaves room for the handful of extra parameters (username, media
# id, ...) that these queries bind alongside the list.
_SQL_IN_CHUNK = 400


def _sql_chunks(values, size: int = _SQL_IN_CHUNK):
    """Yield (chunk, placeholders) pairs for building bounded `IN (...)` lists.

    Any query whose parameter count scales with the number of rows it touches
    belongs here -- a library with a thousand episodes, a calendar with a
    thousand entries, or a client-supplied list of ids are all normal inputs,
    and none of them should be able to make a query fail to *parse*.
    """
    values = list(values)
    for start in range(0, len(values), size):
        chunk = values[start:start + size]
        yield chunk, ",".join("?" for _ in chunk)


def _migrate_db(conn):
    """Add columns to the users table that were introduced after the
    initial CREATE TABLE, so existing databases stay compatible.

    Each column is added only if missing (checked via PRAGMA table_info),
    so this is safe to call on every startup.
    """
    # First: put the table back if a previous run's rebuild was interrupted.
    # Has to happen before the column checks below, or they would run against
    # the half-built table and "fix" the wrong one.
    _recover_interrupted_user_rebuild(conn)

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

    _migrate_role_check(conn)

    conn.execute(_CREATE_SSO_INDEX)
    conn.commit()


# Every role the app knows. 'kids' is a RESTRICTION, not a rank: it sits
# below 'user' and is checked by name everywhere rather than by comparing
# against 'admin', because "not an admin" was never meant to mean "allowed
# to do everything else".
USER_ROLES = ("admin", "user", "kids")


def _table_columns(conn, table):
    return [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _recover_interrupted_user_rebuild(conn):
    """Undo a users-table rebuild that did not finish.

    This exists because the first version of the rebuild below lost people
    their accounts, and it is worth being precise about how, because the
    mechanism is not obvious:

    Python's sqlite3 module (legacy transaction control) only opens a
    transaction before DML -- INSERT/UPDATE/DELETE. **DDL runs in
    autocommit.** So `ALTER TABLE users RENAME TO users_old` and the
    `CREATE TABLE users` after it were committed the moment they ran. When
    the following INSERT then failed, `conn.rollback()` had nothing to undo,
    and the recovery `ALTER TABLE users_old RENAME TO users` could not work
    either -- a `users` table already existed, the new empty one. The result
    was an empty users table, an instance asking for first-run setup, and
    every account still sitting in `users_old`.

    Nothing was lost, and this puts it back: if `users_old` exists, it is the
    real data and whatever is in `users` is at most a partial copy.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users_old'"
    ).fetchone()
    if not exists:
        return

    old_count = conn.execute("SELECT COUNT(*) AS c FROM users_old").fetchone()["c"]
    try:
        new_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    except sqlite3.Error:
        new_count = -1                      # no users table at all

    logger.warning(
        "Found an interrupted users-table rebuild (users_old: %d row(s), "
        "users: %d) — restoring the original table", old_count, new_count)

    # The rebuild only ever fails BEFORE the copy, so `users` here is the
    # empty new table and dropping it loses nothing. Guarded anyway: if it
    # somehow holds more rows than the original, keep both and let a human
    # look rather than delete data on a guess.
    if new_count > old_count:
        logger.error(
            "users has MORE rows than users_old — leaving both tables in place. "
            "This needs a manual look; nothing has been deleted.")
        return

    try:
        conn.execute("DROP TABLE IF EXISTS users")
        conn.execute("ALTER TABLE users_old RENAME TO users")
        conn.execute(_CREATE_SSO_INDEX)
        conn.commit()
        logger.warning("Restored %d user account(s) from users_old", old_count)
    except sqlite3.Error:
        logger.exception("Could not restore users_old — it is still there, untouched")


def _migrate_role_check(conn):
    """Widen users.role's CHECK constraint to include 'kids'.

    SQLite cannot ALTER a CHECK constraint, so the only way is to rebuild the
    table. Done once and only when needed: the stored CREATE statement is read
    back from sqlite_master and the rebuild is skipped when 'kids' is already
    in it, so this costs one cheap query per startup on an up-to-date DB.

    Deliberately NOT a "drop the constraint" migration: the CHECK is what stops
    a typo'd role from becoming a silently privileged account, and losing it
    on upgrade would be worse than the rebuild.

    Written defensively because DDL here is NOT transactional (see
    _recover_interrupted_user_rebuild): the copy is verified before anything
    is dropped, and a failure leaves the ORIGINAL table in place under its
    original name rather than trusting a rollback that cannot happen.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    sql = (row["sql"] if row else "") or ""
    if not sql or "'kids'" in sql:
        return

    logger.info("Migrating users.role to allow the 'kids' role")
    before = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

    # Copy only the columns BOTH tables have. The old table may carry columns
    # this build no longer creates, and _CREATE_TABLE may carry ones an old
    # database never got -- copying either list blindly is what broke this the
    # first time round.
    old_columns = _table_columns(conn, "users")
    conn.execute("DROP TABLE IF EXISTS users_new_kids")   # leftover of a failed run
    # .replace(..., 1) hits the table name in "CREATE TABLE IF NOT EXISTS
    # users" and nothing else -- the column list below it contains no such word.
    conn.execute(_CREATE_TABLE.replace("users", "users_new_kids", 1))
    new_columns = _table_columns(conn, "users_new_kids")
    shared = [c for c in old_columns if c in new_columns]
    names = ", ".join(shared)

    # foreign_keys is per-connection and off by default here, but say so
    # explicitly: a rebuild with it ON would cascade-delete rows that
    # reference users while the old table is dropped.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(
            "INSERT INTO users_new_kids (%s) SELECT %s FROM users" % (names, names))
        after = conn.execute("SELECT COUNT(*) AS c FROM users_new_kids").fetchone()["c"]
        if after != before:
            raise sqlite3.IntegrityError(
                "copied %d of %d user row(s)" % (after, before))
        # Only now is the original touched. Up to this point a crash leaves a
        # stray users_new_kids table and nothing else.
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new_kids RENAME TO users")
        conn.execute(_CREATE_SSO_INDEX)
        conn.commit()
        logger.info("users.role migration done (%d account(s) kept)", after)
    except sqlite3.Error:
        logger.exception(
            "users.role migration failed — the 'kids' role is unavailable. "
            "Your accounts are untouched.")
        try:
            conn.execute("DROP TABLE IF EXISTS users_new_kids")
            conn.commit()
        except sqlite3.Error:
            pass
    finally:
        # Restore the connection's normal state, which is OFF (see
        # _configure_connection). This used to turn enforcement ON here, so
        # whichever caller went on using this connection silently got a
        # different pragma than every other code path.
        conn.execute("PRAGMA foreign_keys=OFF")
