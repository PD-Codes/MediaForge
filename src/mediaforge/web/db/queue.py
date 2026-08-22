"""The download queue: enqueue, claim, progress, cancel, retry.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import DB_PATH, get_db

logger = get_logger(__name__)


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
        # Add replace_paths column (migration for existing DBs) — {episode_url:
        # [old file paths]} for a language upgrade queued by auto-sync: once the
        # episode has been re-downloaded in the better language, the worker
        # deletes the listed files (see queue_worker's _delete_replaced_files).
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN replace_paths TEXT NOT NULL DEFAULT '{}'"
            )
        except sqlite3.OperationalError as _mig_err:
            if "duplicate column" not in str(_mig_err).lower():
                logger.warning("[Migration] Unexpected error adding column: %s", _mig_err)
        # Add path_language column (migration for existing DBs) — the language
        # that decides the target FOLDER and FILE NAME, which is not always the
        # language being downloaded. A multi-language download is queued as one
        # row per language; the secondary rows only contribute an extra audio
        # track to the file the primary row produced, so they must resolve
        # {language} and the language-separation folder against the PRIMARY
        # language or they would write a second, near-identical file next to it.
        # NULL for every ordinary job, which then falls back to `language`.
        try:
            conn.execute(
                "ALTER TABLE download_queue ADD COLUMN path_language TEXT"
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
                    ("replace_paths", "ALTER TABLE download_queue ADD COLUMN replace_paths TEXT NOT NULL DEFAULT '{}'"),
                    ("path_language", "ALTER TABLE download_queue ADD COLUMN path_language TEXT"),
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
                            source_provider = (SELECT source_provider FROM _download_queue_old WHERE id = new.id),
                            replace_paths = COALESCE((SELECT replace_paths FROM _download_queue_old WHERE id = new.id), '{}'),
                            path_language = (SELECT path_language FROM _download_queue_old WHERE id = new.id)"""
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
        # get_queue() filters on hidden and orders by position, id -- the index
        # above starts with status and does not serve it, so every queue poll
        # (every 2-5s per open tab) was a full scan plus a sort.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dq_hidden_position "
            "ON download_queue(hidden, position, id)"
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
    replace_paths=None,
    path_language=None,
):
    """Queue a download.

    `replace_paths` is {episode_url: [old file paths]} for a language upgrade:
    the listed files are deleted once that episode has been downloaded again in
    the better language. Only auto-sync sets it (see autosync_worker), so a
    manual download never removes anything.

    `path_language` overrides which language the target folder and file name are
    derived from. Set only on the secondary rows of a multi-language download,
    where it names the primary language so every row writes to the same file.
    """
    import json

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO download_queue (title, series_url, episodes, total_episodes, language, provider, username, custom_path_id, source, upscale, format_id, source_provider, replace_paths, path_language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                json.dumps(replace_paths or {}),
                path_language,
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


def clear_completed(username=None):
    """Hide finished entries from the queue UI while keeping them for statistics.

    `username` limits it to that account's own rows -- what a non-admin gets,
    so tidying up after yourself cannot clear everyone else's history too.
    None means every row, which is the admin case and the previous behaviour.
    """
    conn = get_db()
    try:
        query = (
            "UPDATE download_queue SET hidden = 1 "
            "WHERE status IN ('completed', 'partial', 'failed', 'cancelled')"
        )
        params = ()
        if username:
            query += " AND username = ?"
            params = (username,)
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()
