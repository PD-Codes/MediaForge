"""Auto-Sync jobs.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


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
    extra_languages TEXT,
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
        # Migration: add extra_languages (JSON list) for existing DBs. The
        # additional languages whose audio tracks are muxed into the file
        # `language` produces -- `language` stays the primary one and keeps
        # deciding folder and file name, exactly as it always has, so a job
        # written before this column behaves identically with it NULL/empty.
        # Deliberately not folded into `language`: every existing row, index
        # and comparison in this table treats that column as one label.
        try:
            conn.execute("ALTER TABLE autosync_jobs ADD COLUMN extra_languages TEXT")
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
    cover_url: str | None = None, extra_languages: str | None = None,
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
            "path_unavailable_action, episode_filter, movie_custom_path_id, cover_url, "
            "extra_languages, last_check) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, series_url, language, provider, custom_path_id, added_by,
             path_unavailable_action, episode_filter, movie_custom_path_id, cover_url,
             extra_languages, now_str),
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
        "extra_languages",
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
