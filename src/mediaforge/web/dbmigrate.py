"""Versioned schema migrations and pre-upgrade snapshots for the SQLite DB.

Why this exists
---------------
Before this module, the schema was created and evolved exclusively by the
``init_*_db()`` functions in :mod:`mediaforge.web.db`: a pile of
``CREATE TABLE IF NOT EXISTS`` statements plus hand-written ``ALTER TABLE``
paths guarded by ``PRAGMA table_info`` checks. That is idempotent, which is
why it worked -- but it has two properties that make it unsuitable for an
installation anybody depends on:

* There is no recorded schema version, so nothing can tell whether a database
  has been through a given change. The only "version" is the shape of the
  tables themselves, re-derived on every start.
* There is no way back. A release that changes the schema cannot be undone,
  because the old code has no idea what the new code did. Downgrading, or
  recovering from an upgrade that failed halfway, means restoring a backup
  the user hopefully made.

This module adds the missing half. The ``init_*_db()`` functions keep doing
what they do (they are the "create the world from nothing" path, and they are
well tested); this module owns *changes over time* and the safety net around
applying them:

1. A ``schema_migrations`` table records which numbered migrations ran, when,
   and under which app version.
2. Existing databases are *baselined* rather than migrated: on first contact,
   every already-known migration is marked applied without running it, since
   the lazy ``init_*_db()`` path already produced that shape. Only migrations
   added after this module shipped actually execute.
3. Before any pending migration runs, the database file is snapshotted with
   SQLite's online backup API (not a file copy -- see ``snapshot()``), so a
   failed or unwanted upgrade can be rolled back to the exact bytes that were
   there before.

Adding a migration
------------------
Append to ``_register_migrations()`` at the bottom of this file::

    @migration(3, "add foo.bar column")
    def _m3(conn):
        conn.execute("ALTER TABLE foo ADD COLUMN bar TEXT")

Rules for migration bodies:

* Never ``commit()`` -- the engine owns the transaction.
* Never import from :mod:`mediaforge.web.db` at module import time (circular);
  import inside the function if you really need a helper.
* Be defensive: a user may have a database that a previous, buggy version
  half-migrated. ``column_exists()``/``table_exists()`` are provided for that.
* Numbers are permanent. Never renumber or reuse one.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import threading

from ..config import MEDIAFORGE_CONFIG_DIR
from ..logger import get_logger

logger = get_logger(__name__)

# Where pre-upgrade snapshots live. Deliberately a sibling of the database
# rather than inside a temp dir: a rollback is most needed exactly when the
# app will not start, and the user has to be able to find these by hand.
SNAPSHOT_DIR = MEDIAFORGE_CONFIG_DIR / "db_snapshots"

# How many automatic snapshots to keep. Manual ones (reason="manual") are
# never pruned -- the user asked for those explicitly.
MAX_AUTO_SNAPSHOTS = 10

_MIGRATIONS: dict[int, tuple[str, callable]] = {}
_lock = threading.Lock()

# The highest migration whose effect the pre-engine ``init_*_db()`` path
# already produces. A database that predates this module is recorded as having
# applied everything up to and including this number, without running it.
#
# This is a constant, not "whatever is registered": raising it means claiming
# that an untouched old database already has that migration's tables, which is
# only true for the schema as it stood when the engine was introduced.
BASELINE_VERSION = 1


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def migration(version: int, name: str):
    """Decorator registering a migration function under a permanent number."""
    def _wrap(fn):
        if version in _MIGRATIONS:
            raise RuntimeError(
                "Duplicate migration version %d (%r vs %r). Migration numbers "
                "are permanent and must never be reused."
                % (version, _MIGRATIONS[version][0], name)
            )
        _MIGRATIONS[version] = (name, fn)
        return fn
    return _wrap


def known_versions() -> list[int]:
    return sorted(_MIGRATIONS)


def latest_version() -> int:
    return max(_MIGRATIONS) if _MIGRATIONS else 0


# ---------------------------------------------------------------------------
# Small helpers migrations may use
# ---------------------------------------------------------------------------

def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn, table: str, column: str) -> bool:
    if not table_exists(conn, table):
        return False
    # PRAGMA takes no bound parameters, hence the interpolation. `table` is
    # never user input here -- every caller passes a literal from this file.
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    names = {(r["name"] if hasattr(r, "keys") else r[1]) for r in rows}
    return column in names


def add_column(conn, table: str, column: str, ddl: str) -> bool:
    """Idempotent ``ALTER TABLE ... ADD COLUMN``. Returns True if it added one."""
    if not table_exists(conn, table) or column_exists(conn, table, column):
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))
    return True


# ---------------------------------------------------------------------------
# Version bookkeeping
# ---------------------------------------------------------------------------

_SCHEMA_TABLE = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    -- 1 when the row was written by baselining an existing database rather
    -- than by actually executing the migration. Purely informational, but it
    -- is the difference between "we know this ran" and "we assumed it did",
    -- which matters when debugging a database that came from an old install.
    baselined   INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_schema_table(conn) -> None:
    conn.execute(_SCHEMA_TABLE)


def applied_versions(conn) -> set[int]:
    _ensure_schema_table(conn)
    return {
        (r["version"] if hasattr(r, "keys") else r[0])
        for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }


def _looks_like_existing_install(conn) -> bool:
    """True when this database predates the migration engine.

    Checked against ``app_settings`` rather than ``users``: the users table
    only exists when authentication is enabled, so a no-auth install would
    otherwise be mistaken for a fresh database and have every migration run
    against tables the ``init_*_db()`` path had already brought up to date.
    """
    return table_exists(conn, "app_settings") or table_exists(conn, "download_queue")


def _record(conn, version: int, name: str, app_version: str, baselined: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations "
        "(version, name, applied_at, app_version, baselined) VALUES (?,?,?,?,?)",
        (version, name, _dt.datetime.now().isoformat(timespec="seconds"),
         app_version, 1 if baselined else 0),
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def _app_version() -> str:
    try:
        from .version_info import get_version_info
        return str(get_version_info().get("version") or "")
    except Exception:
        try:
            from importlib.metadata import version
            return version("mediaforge")
        except Exception:
            return ""


def snapshot(reason: str = "auto", note: str = "") -> dict | None:
    """Copy the live database to a timestamped snapshot and return its record.

    Uses SQLite's online backup API instead of ``shutil.copy``. That is not a
    detail: the database runs in WAL mode, so at any moment part of the
    committed state lives in ``-wal`` and not in the main file. Copying only
    ``mediaforge.db`` yields a database that is *valid* but silently missing
    the most recent transactions -- the worst possible failure mode for a
    rollback target. The backup API checkpoints for us and produces one
    self-contained file.
    """
    from .db import DB_PATH

    if not os.path.exists(DB_PATH):
        return None

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    app_ver = _app_version()
    base = "mediaforge-%s-%s" % (stamp, reason)
    target = SNAPSHOT_DIR / (base + ".db")

    # Collision guard: two snapshots in the same second (upgrade + manual)
    # would otherwise overwrite each other.
    n = 1
    while target.exists():
        n += 1
        target = SNAPSHOT_DIR / ("%s-%d.db" % (base, n))

    src = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    meta = {
        "id": target.stem,
        "file": target.name,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "note": note,
        "app_version": app_ver,
        "size": target.stat().st_size,
    }
    (SNAPSHOT_DIR / (target.stem + ".json")).write_text(
        json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("[Migrate] Database snapshot created: %s (%s)", target.name, reason)
    _prune_snapshots()
    return meta


def list_snapshots() -> list[dict]:
    """Newest first."""
    if not SNAPSHOT_DIR.exists():
        return []
    out = []
    for meta_file in SNAPSHOT_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        db_file = SNAPSHOT_DIR / meta.get("file", "")
        if not db_file.exists():
            continue
        meta["size"] = db_file.stat().st_size
        out.append(meta)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def _prune_snapshots() -> int:
    autos = [m for m in list_snapshots() if m.get("reason") != "manual"]
    removed = 0
    for meta in autos[MAX_AUTO_SNAPSHOTS:]:
        removed += 1 if delete_snapshot(meta["id"]) else 0
    return removed


def _snapshot_path(snapshot_id: str):
    """Resolve a snapshot id to its file, refusing anything outside the dir.

    The id reaches this from an HTTP route, so it is untrusted: without the
    containment check, ``../../mediaforge.db`` would let an admin-level
    request delete or restore arbitrary files through this API.
    """
    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
        return None
    path = (SNAPSHOT_DIR / (snapshot_id + ".db")).resolve()
    try:
        if path.parent != SNAPSHOT_DIR.resolve():
            return None
    except Exception:
        return None
    return path if path.exists() else None


def delete_snapshot(snapshot_id: str) -> bool:
    path = _snapshot_path(snapshot_id)
    if not path:
        return False
    try:
        path.unlink()
    except Exception as exc:
        logger.warning("[Migrate] Could not delete snapshot %s: %s", snapshot_id, exc)
        return False
    meta = SNAPSHOT_DIR / (snapshot_id + ".json")
    if meta.exists():
        try:
            meta.unlink()
        except Exception:
            pass
    return True


def verify_snapshot(snapshot_id: str) -> dict:
    """Open a snapshot read-only and prove it is restorable.

    A backup nobody has ever restored is not a backup. This does the cheap
    half of that proof on demand: integrity check, foreign-key check, and a
    row count for the tables that would actually hurt to lose.
    """
    path = _snapshot_path(snapshot_id)
    if not path:
        return {"ok": False, "error": "not_found"}

    result: dict = {"ok": False, "id": snapshot_id, "tables": {}}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity"] = integrity
            result["foreign_key_errors"] = len(
                conn.execute("PRAGMA foreign_key_check").fetchall())
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone() if table_exists(conn, "schema_migrations") else None
            result["schema_version"] = row[0] if row else 0

            for table in ("users", "app_settings", "download_queue", "favourites",
                          "autosync_jobs", "download_history", "watch_progress",
                          "custom_paths"):
                if table_exists(conn, table):
                    result["tables"][table] = conn.execute(
                        "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            result["ok"] = (integrity == "ok" and result["foreign_key_errors"] == 0)
        finally:
            conn.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def restore_snapshot(snapshot_id: str) -> dict:
    """Replace the live database with a snapshot.

    Refuses to run unless the snapshot passes ``verify_snapshot()`` -- restoring
    a corrupt file over a working database turns a recoverable situation into
    an unrecoverable one. Takes a snapshot of the *current* state first, so the
    restore itself is undoable.

    The caller must restart the app afterwards: open connections (workers,
    request contexts) still point at the replaced inode.
    """
    from .db import DB_PATH

    check = verify_snapshot(snapshot_id)
    if not check.get("ok"):
        return {"ok": False, "error": "verify_failed", "detail": check}

    path = _snapshot_path(snapshot_id)
    pre = snapshot(reason="pre-restore", note="automatic, before restoring %s" % snapshot_id)

    try:
        # Remove the sidecars explicitly. A leftover -wal from the old database
        # applied on top of the restored main file would replay transactions
        # that the snapshot deliberately does not contain.
        for suffix in ("-wal", "-shm"):
            side = str(DB_PATH) + suffix
            if os.path.exists(side):
                os.remove(side)
        shutil.copyfile(str(path), str(DB_PATH))
    except Exception as exc:
        logger.error("[Migrate] Restore of %s failed: %s", snapshot_id, exc)
        return {"ok": False, "error": str(exc), "rollback_snapshot": (pre or {}).get("id")}

    logger.warning("[Migrate] Database restored from snapshot %s -- restart required",
                   snapshot_id)
    return {"ok": True, "restored": snapshot_id, "rollback_snapshot": (pre or {}).get("id")}


# ---------------------------------------------------------------------------
# Running migrations
# ---------------------------------------------------------------------------

def status() -> dict:
    from .db import get_db
    conn = get_db()
    try:
        done = applied_versions(conn)
        rows = conn.execute(
            "SELECT version, name, applied_at, app_version, baselined "
            "FROM schema_migrations ORDER BY version"
        ).fetchall() if table_exists(conn, "schema_migrations") else []
        return {
            "current": max(done) if done else 0,
            "latest": latest_version(),
            "pending": sorted(set(_MIGRATIONS) - done),
            "applied": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def run_pending(*, allow_snapshot: bool = True) -> dict:
    """Bring the database up to ``latest_version()``.

    Called once from app startup, before anything else touches the schema.
    Safe to call repeatedly and from a single process only -- guarded by a
    module lock, and by SQLite's own write lock across processes.
    """
    from .db import get_db

    with _lock:
        conn = get_db()
        try:
            _ensure_schema_table(conn)
            # Do not trust the record on its own: a shipped bug once marked
            # migrations applied without running them. See repair_missing().
            repaired = repair_missing(conn)
            done = applied_versions(conn)
            pending = sorted(set(_MIGRATIONS) - done)
            app_ver = _app_version()

            if not pending:
                conn.commit()
                return {"ok": True, "applied": [], "baselined": [],
                        "repaired": repaired,
                        "current": max(done) if done else 0}

            # Existing install that has never seen this engine: the shape the
            # init_*_db() path produces is exactly what BASELINE_VERSION
            # describes, so mark that much applied without running it.
            #
            # Only up to BASELINE_VERSION. Baselining *everything* pending was
            # the obvious-looking version of this and it is wrong: migrations
            # above the baseline create tables no init_*_db() function knows
            # about, so marking them applied means those tables are never
            # created and the first request that needs one fails with
            # "no such table". Baseline describes the past; anything after it
            # has to actually run.
            baselined = []
            if not done and _looks_like_existing_install(conn):
                baselined = [v for v in pending if v <= BASELINE_VERSION]
                for version in baselined:
                    _record(conn, version, _MIGRATIONS[version][0], app_ver, baselined=True)
                conn.commit()
                pending = [v for v in pending if v > BASELINE_VERSION]
                logger.info("[Migrate] Existing database baselined at schema version %d",
                            BASELINE_VERSION)
                if not pending:
                    return {"ok": True, "applied": [], "baselined": baselined, "repaired": repaired,
                            "current": BASELINE_VERSION}

            snap = snapshot(reason="pre-migration",
                            note="before schema %d" % max(pending)) if allow_snapshot else None

            applied = []
            for version in pending:
                name, fn = _MIGRATIONS[version]
                logger.info("[Migrate] Applying migration %d (%s)", version, name)
                try:
                    # One transaction per migration: a failure leaves every
                    # earlier migration committed and this one fully undone,
                    # so a fixed release can resume from exactly here.
                    conn.execute("BEGIN")
                    fn(conn)
                    _record(conn, version, name, app_ver, baselined=False)
                    conn.commit()
                    applied.append(version)
                except Exception as exc:
                    conn.rollback()
                    logger.error("[Migrate] Migration %d (%s) FAILED: %s", version, name, exc)
                    return {
                        "ok": False, "failed": version, "error": str(exc),
                        "applied": applied,
                        "snapshot": (snap or {}).get("id"),
                        "current": max(applied) if applied else (max(done) if done else 0),
                    }

            logger.info("[Migrate] Database at schema version %d", max(applied))
            return {
                "ok": True, "applied": applied, "baselined": baselined,
                "repaired": repaired,
                "snapshot": (snap or {}).get("id"), "current": max(applied),
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The migrations
# ---------------------------------------------------------------------------
# Version 1 is the baseline: everything the init_*_db() functions created
# before this engine existed. It is intentionally a no-op -- its only job is
# to give existing databases a version number to be baselined at.

@migration(1, "baseline (pre-migration-engine schema)")
def _m1_baseline(conn):
    return None


@migration(2, "user groups, permissions and library scoping")
def _m2_groups(conn):
    from .groups import create_schema
    create_schema(conn)


@migration(3, "download rules engine")
def _m3_rules(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            priority    INTEGER NOT NULL DEFAULT 100,
            stop        INTEGER NOT NULL DEFAULT 0,
            conditions  TEXT NOT NULL DEFAULT '[]',
            actions     TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_rules_order "
        "ON download_rules (enabled, priority)")


@migration(4, "per-title language profiles")
def _m4_language_profiles(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS language_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            -- Ordered fallback chain, JSON array of language codes.
            chain       TEXT NOT NULL DEFAULT '[]',
            -- 1 = download every language in the chain, 0 = first match wins.
            grab_all    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS title_language_profile (
            -- Series URL is the only identifier every provider path shares;
            -- TMDB ids are absent for a good part of the catalogue.
            series_url  TEXT PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT '',
            profile_id  INTEGER NOT NULL REFERENCES language_profiles(id) ON DELETE CASCADE,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


@migration(5, "maintenance windows and scheduled throttling")
def _m5_maintenance(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_windows (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            -- Bitmask, Monday = bit 0 .. Sunday = bit 6.
            days_mask     INTEGER NOT NULL DEFAULT 127,
            start_minute  INTEGER NOT NULL DEFAULT 0,
            end_minute    INTEGER NOT NULL DEFAULT 1440,
            max_downloads INTEGER NOT NULL DEFAULT 1,
            allow_encoding INTEGER NOT NULL DEFAULT 0,
            allow_upscale  INTEGER NOT NULL DEFAULT 0,
            allow_scan     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


@migration(6, "worker heartbeats")
def _m6_worker_heartbeats(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worker_heartbeats (
            worker      TEXT PRIMARY KEY,
            pid         INTEGER NOT NULL DEFAULT 0,
            host        TEXT NOT NULL DEFAULT '',
            mode        TEXT NOT NULL DEFAULT 'inprocess',
            state       TEXT NOT NULL DEFAULT 'idle',
            detail      TEXT NOT NULL DEFAULT '',
            last_beat   TEXT NOT NULL DEFAULT (datetime('now')),
            last_run    TEXT,
            next_run    TEXT,
            last_error  TEXT NOT NULL DEFAULT '',
            error_at    TEXT
        )
    """)


@migration(7, "API key scopes")
def _m7_api_keys(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            -- Only the hash is stored. The plaintext key is shown once, at
            -- creation, and is unrecoverable afterwards -- a stolen database
            -- must not hand the attacker working API credentials.
            key_hash    TEXT NOT NULL UNIQUE,
            key_prefix  TEXT NOT NULL DEFAULT '',
            scopes      TEXT NOT NULL DEFAULT '[]',
            user_id     INTEGER,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT,
            last_used   TEXT
        )
    """)


# Tables each migration is responsible for creating. Used by repair_missing()
# below, which is the answer to "the bookkeeping says this ran, but the table
# is not there".
#
# Only tables a migration CREATES belong here. A migration that adds a column
# has nothing to verify this way and simply has no entry.
_MIGRATION_TABLES: dict[int, tuple[str, ...]] = {
    2: ("user_groups", "user_group_members"),
    3: ("download_rules",),
    4: ("language_profiles", "title_language_profile"),
    5: ("maintenance_windows",),
    6: ("worker_heartbeats",),
    7: ("api_keys",),
}


def repair_missing(conn) -> list[int]:
    """Un-record migrations whose tables are not actually there.

    This exists because of a real incident, and the shape of it is worth
    keeping in mind for any future migration engine: an early version of
    ``run_pending()`` baselined *everything* pending on an existing database
    rather than stopping at BASELINE_VERSION. Databases that started the app
    once with that version came away with rows 2..7 marked applied and none of
    those tables created. The bug was fixed, but the fix cannot help them --
    the engine now correctly believes it has nothing to do, and the app fails
    at runtime with "no such table: worker_heartbeats".

    So the record is not trusted on its own. Before running pending
    migrations, every applied migration that owns tables is checked against
    ``sqlite_master``; any whose tables are missing is un-recorded so the
    normal path re-runs it. Every migration body is written with
    ``IF NOT EXISTS``, so a partially-applied one heals rather than conflicts.

    Returns the versions it reset.
    """
    reset = []
    for version, tables in sorted(_MIGRATION_TABLES.items()):
        missing = [t for t in tables if not table_exists(conn, t)]
        if not missing:
            continue
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone()
        if not row:
            continue          # not claimed as applied -- nothing to repair
        logger.warning(
            "[Migrate] Migration %d is recorded as applied but %s missing -- "
            "re-running it", version, ", ".join(missing))
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
        reset.append(version)
    if reset:
        conn.commit()
    return reset


def _register_migrations() -> None:
    """No-op kept as the documented anchor for where migrations are declared.

    They register at import time via the decorator; this function exists so
    that grepping for "register migrations" lands in the right file.
    """
    return None
