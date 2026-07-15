"""Full & Selective Backup: export/import of settings and user data.

This module owns the portable backup format used by the "Backup" settings tab.
It serializes a chosen set of *categories* (settings + user-data tables, never
caches) into a single ``.mfbackup`` JSON envelope and restores them again.

Design notes
------------
* Non-sensitive values are stored as plain text so a backup stays portable and
  diff-able across installations.
* Sensitive settings (see ``db.SENSITIVE_KEYS`` / ``is_sensitive_key``) are
  encrypted at rest with an install-specific Fernet key derived from
  ``~/.mediaforge/.flask_secret`` -- that key is NOT portable. So on export we
  decrypt them with the local key and re-encrypt them under a key derived from
  the user-supplied backup password (PBKDF2-SHA256 -> Fernet). On import we
  decrypt with the password and hand the plain values to ``set_setting()``,
  which re-encrypts them with the *target* install's local key.
* A backup password is always required (even when no secrets are present), per
  product decision.

Public API: ``list_categories``, ``export_backup``, ``preview_backup``,
``import_backup``. Flask wiring lives in ``routes/backup.py``.
"""

import base64
import json
import time

from ..logger import get_logger
from . import db as _db
from .db import get_db, get_setting, is_sensitive_key, set_setting

logger = get_logger(__name__)

FORMAT_NAME = "mediaforge-backup"
FORMAT_VERSION = 1
_PBKDF2_ITERATIONS = 600_000

# Settings keys that are install-specific / derived and must never be migrated
# to another installation.
_SETTING_DENYLIST = frozenset({
    "env_migrated",
})

# Category catalog. "settings" is special-cased; every other category is a
# set of concrete tables. Cache tables (tmdb_cache, provider_cache,
# browse_cache, library_cache, mediascan_cache, uptime_heartbeats,
# devinfo_posts) are deliberately absent -- they are never backed up.
BACKUP_CATEGORIES: dict = {
    "settings":       {"kind": "settings", "default": True},
    "favourites":     {"kind": "tables", "default": True,
                       "tables": ["favourites", "media_ignored", "seerr_hidden"]},
    "history":        {"kind": "tables", "default": True,
                       "tables": ["download_history"]},
    "watch_progress": {"kind": "tables", "default": True,
                       "tables": ["watch_progress"]},
    "custom_paths":   {"kind": "tables", "default": True,
                       "tables": ["custom_paths"]},
    "users":          {"kind": "tables", "default": True,
                       "tables": ["users", "user_notification_prefs"]},
    "queues":         {"kind": "tables", "default": False,
                       "tables": ["download_queue", "autosync_jobs",
                                  "encoding_queue", "upscale_queue"]},
    "calendar":       {"kind": "tables", "default": False,
                       "tables": ["calendar_media", "calendar_episodes"]},
    "push":           {"kind": "tables", "default": False,
                       "tables": ["push_subscriptions"]},
}

# Categories registered at runtime by third-party modules (see
# register_backup_category). Kept separate from the frozen core catalog above so
# a module can add its own tables to the backup without a core release.
# Module *settings* (keys prefixed ``module:<id>:``) are already covered by the
# "settings" category, so this is only needed for modules that own extra tables.
_MODULE_CATEGORIES: dict = {}


def register_backup_category(category_id: str, tables, default: bool = False) -> bool:
    """Let a third-party module add its own tables to the backup catalog.

    ``category_id`` must be unique (a core id or an already-registered one is
    rejected). ``tables`` is the list of table names to export/import for this
    category. Returns True if newly registered.

    Example (from a module's setup):
        from mediaforge.web.backup import register_backup_category
        register_backup_category("mymodule", ["mymodule_items"], default=True)
    """
    if not category_id or category_id in BACKUP_CATEGORIES or category_id in _MODULE_CATEGORIES:
        return False
    _MODULE_CATEGORIES[category_id] = {
        "kind": "tables",
        "default": bool(default),
        "tables": list(tables or []),
    }
    return True


def _all_categories() -> dict:
    """Core catalog plus any module-registered categories."""
    return {**BACKUP_CATEGORIES, **_MODULE_CATEGORIES}


class BackupError(Exception):
    """Raised for any user-facing backup/restore failure (bad password,
    unsupported format, corrupt file, ...)."""


# ---------------------------------------------------------------------------
# Password-based encryption (PBKDF2-SHA256 -> Fernet)
# ---------------------------------------------------------------------------

def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    """Derive a urlsafe-base64 Fernet key from *password* and *salt*."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _encrypt_blob(obj, password: str, salt: bytes) -> str:
    """Encrypt a JSON-serializable *obj* into a Fernet token string."""
    from cryptography.fernet import Fernet

    token = Fernet(_derive_fernet_key(password, salt)).encrypt(
        json.dumps(obj).encode("utf-8")
    )
    return token.decode("ascii")


def _decrypt_blob(token: str, password: str, salt: bytes):
    """Decrypt a Fernet token string back into the original object.

    Raises ``BackupError`` on a wrong password or tampered/corrupt token.
    """
    from cryptography.fernet import Fernet, InvalidToken

    try:
        raw = Fernet(_derive_fernet_key(password, salt)).decrypt(
            token.encode("ascii")
        )
    except InvalidToken as exc:
        raise BackupError("wrong password or corrupted backup") from exc
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# BLOB-safe JSON encoding for table rows
# ---------------------------------------------------------------------------

def _encode_value(val):
    """Make a SQLite cell value JSON-serializable (bytes -> tagged base64)."""
    if isinstance(val, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(val)).decode("ascii")}
    return val


def _decode_value(val):
    """Reverse of :func:`_encode_value`."""
    if isinstance(val, dict) and "__bytes__" in val:
        return base64.b64decode(val["__bytes__"])
    return val


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _collect_settings():
    """Return ``(plain, secret)`` dicts of every app_settings key.

    ``plain`` holds non-sensitive keys as plain text; ``secret`` holds
    sensitive keys as plain text (they are encrypted under the backup password
    by the caller). Denylisted/derived keys are skipped.

    A value is routed to ``secret`` if its key is registered sensitive *or* the
    value is stored encrypted at rest (``enc:`` prefix). The second check is a
    security guard: a secret left behind by a currently-disabled module is not
    registered as sensitive this run, so without it the decrypted plaintext
    would leak into the portable ``plain`` section.
    """
    conn = get_db()
    try:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    finally:
        conn.close()

    plain: dict = {}
    secret: dict = {}
    for row in rows:
        key = row["key"]
        if key in _SETTING_DENYLIST:
            continue
        raw = row["value"]
        value = get_setting(key)  # decrypts sensitive/enc: values transparently
        if value is None:
            continue
        if is_sensitive_key(key) or (raw or "").startswith(_db._ENC_PREFIX):
            secret[key] = value
        else:
            plain[key] = value
    return plain, secret


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _collect_table(conn, table: str):
    """Return all rows of *table* as a list of JSON-safe dicts."""
    if not _table_exists(conn, table):
        return []
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
    return [{k: _encode_value(r[k]) for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_categories() -> list:
    """Return category metadata + current row counts for the UI."""
    conn = get_db()
    try:
        out = []
        for cid, meta in _all_categories().items():
            if meta["kind"] == "settings":
                cnt = conn.execute("SELECT COUNT(*) AS c FROM app_settings").fetchone()["c"]
            else:
                cnt = 0
                for tbl in meta["tables"]:
                    if _table_exists(conn, tbl):
                        cnt += conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()["c"]
            out.append({"id": cid, "default": meta["default"], "count": int(cnt)})
        return out
    finally:
        conn.close()


def export_backup(categories, password: str, allow_no_password: bool = False) -> bytes:
    """Serialise *categories* into a ``.mfbackup`` byte string.

    Normally a non-empty *password* is required and sensitive settings are
    encrypted under a key derived from it. If *allow_no_password* is True and no
    password is given, the backup is written **unencrypted**: sensitive values
    (API keys, passwords, tokens) are stored as readable plaintext alongside the
    rest. This is a deliberate, dangerous escape hatch — the caller must have
    confirmed the risk with the user first.
    """
    import os

    no_password = not password
    if no_password and not allow_no_password:
        raise BackupError("a backup password is required")

    catalog = _all_categories()
    selected = [c for c in categories if c in catalog]
    if not selected:
        raise BackupError("no valid categories selected")

    data: dict = {}
    secrets: dict = {}

    conn = get_db()
    try:
        for cid in selected:
            meta = catalog[cid]
            if meta["kind"] == "settings":
                plain, secret = _collect_settings()
                # In no-password mode secrets have nowhere safe to go, so they
                # are inlined into the plaintext settings section.
                data["settings"] = plain if not no_password else {**plain, **secret}
                if not no_password:
                    secrets.update(secret)
            else:
                for tbl in meta["tables"]:
                    data[tbl] = _collect_table(conn, tbl)
    finally:
        conn.close()

    envelope: dict = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app_version": _app_version(),
        "categories": selected,
        "encrypted": not no_password,
        "data": data,
    }
    if not no_password:
        salt = os.urandom(16)
        envelope["kdf"] = {
            "scheme": "pbkdf2-sha256",
            "salt": base64.b64encode(salt).decode("ascii"),
            "iterations": _PBKDF2_ITERATIONS,
        }
        # Present even with no secrets so a wrong password is still detectable.
        envelope["secrets"] = _encrypt_blob(secrets, password, salt)
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def _parse_envelope(file_bytes: bytes) -> dict:
    try:
        env = json.loads(file_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError("not a valid backup file") from exc
    if not isinstance(env, dict) or env.get("format") != FORMAT_NAME:
        raise BackupError("not a MediaForge backup file")
    if int(env.get("format_version", 0)) > FORMAT_VERSION:
        raise BackupError("backup was created by a newer version of MediaForge")
    return env


def preview_backup(file_bytes: bytes, password: str) -> dict:
    """Inspect a backup without writing anything.

    Returns metadata + per-category counts and whether *password* decrypts the
    secrets blob. Never mutates the database.
    """
    env = _parse_envelope(file_bytes)
    data = env.get("data", {})

    counts: dict = {}
    for cid in env.get("categories", []):
        meta = _all_categories().get(cid)
        if not meta:
            continue
        if meta["kind"] == "settings":
            counts[cid] = len(data.get("settings", {}))
        else:
            counts[cid] = sum(len(data.get(t, [])) for t in meta["tables"])

    encrypted = env.get("encrypted", True)
    password_ok = None
    if encrypted and password:
        salt = base64.b64decode(env["kdf"]["salt"])
        try:
            _decrypt_blob(env.get("secrets", ""), password, salt)
            password_ok = True
        except BackupError:
            password_ok = False

    return {
        "format_version": env.get("format_version"),
        "app_version": env.get("app_version"),
        "created_utc": env.get("created_utc"),
        "categories": env.get("categories", []),
        "counts": counts,
        "encrypted": encrypted,
        "password_ok": password_ok,
    }


def import_backup(file_bytes: bytes, password: str, categories, mode: str = "merge") -> dict:
    """Restore selected *categories* from a backup.

    ``mode`` is ``"merge"`` (INSERT OR REPLACE per row; settings overwrite
    existing keys) or ``"replace"`` (DELETE the category's tables first). All
    writes happen in a single transaction; any error rolls the whole import
    back.
    """
    if mode not in ("merge", "replace"):
        raise BackupError("invalid import mode")

    env = _parse_envelope(file_bytes)
    data = env.get("data", {})

    # Unencrypted backups carry their (former) secrets inline in data.settings,
    # so no password/decryption is needed.
    if env.get("encrypted", True):
        if not password:
            raise BackupError("a backup password is required")
        salt = base64.b64decode(env["kdf"]["salt"])
        secrets = _decrypt_blob(env.get("secrets", ""), password, salt)
    else:
        secrets = {}

    catalog = _all_categories()
    available = set(env.get("categories", []))
    selected = [c for c in categories if c in catalog and c in available]
    if not selected:
        raise BackupError("no matching categories to import")

    report: dict = {}
    conn = get_db()
    try:
        conn.execute("BEGIN")
        for cid in selected:
            meta = catalog[cid]
            if meta["kind"] == "settings":
                report[cid] = _restore_settings(conn, data.get("settings", {}), secrets)
            else:
                total = 0
                for tbl in meta["tables"]:
                    total += _restore_table(conn, tbl, data.get(tbl, []), mode)
                report[cid] = total
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("Backup import finished: %s (mode=%s)", report, mode)
    return report


# ---------------------------------------------------------------------------
# Restore helpers
# ---------------------------------------------------------------------------

def _restore_settings(conn, plain: dict, secrets: dict) -> int:
    """Apply plain + secret settings via set_setting (re-encrypts locally).

    Settings are always merged (keys present in the backup overwrite the target
    value); keys absent from the backup are left untouched.
    """
    count = 0
    for key, value in {**plain, **secrets}.items():
        if key in _SETTING_DENYLIST:
            continue
        set_setting(key, value)
        count += 1
    return count


def _restore_table(conn, table: str, rows, mode: str) -> int:
    """Insert *rows* into *table*, keeping only columns that exist locally.

    Table names come exclusively from the fixed ``BACKUP_CATEGORIES`` catalog,
    so interpolating them into SQL is safe.
    """
    if not _table_exists(conn, table):
        return 0

    existing_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if mode == "replace":
        conn.execute(f"DELETE FROM {table}")

    inserted = 0
    for row in rows:
        cols = [c for c in row.keys() if c in existing_cols]
        if not cols:
            continue
        placeholders = ", ".join("?" for _ in cols)
        collist = ", ".join(cols)
        values = [_decode_value(row[c]) for c in cols]
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})",
            values,
        )
        inserted += 1
    return inserted


def _app_version() -> str:
    try:
        from ..config import VERSION
        return VERSION or "unknown"
    except Exception:
        return "unknown"
