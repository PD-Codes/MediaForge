"""The Anime4K upscale queue.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import DB_PATH, get_db
from .encoding import _busy_paths, _row_paths

logger = get_logger(__name__)


_CREATE_UPSCALE_QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS upscale_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_item_id    INTEGER,
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
            ("queue_item_id",    "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE upscale_queue ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        # Backfill position = id for rows that still have 0
        conn.execute("UPDATE upscale_queue SET position = id WHERE position = 0")
        # Lookup for append_download_upscale_file(): one row per download job.
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_upscale_queue_item "
                "ON upscale_queue (queue_item_id, status)"
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def add_to_upscale_queue(title, file_path, output_path=None, source="manual",
                         files=None, queue_item_id=None):
    """Add one upscale job.
    files: list of {file_path, output_path} for multi-file (batch) jobs.
    When files is set, file_path/output_path are taken from files[0].
    queue_item_id: download queue row this job belongs to, so later episodes
    of the same download can be appended to it (see
    append_download_upscale_file).
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
            "INSERT INTO upscale_queue (title, file_path, output_path, files, total_files, source, queue_item_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, fp, out, files_json, total, source, queue_item_id),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE upscale_queue SET position = ? WHERE id = ?", (new_id, new_id))
        conn.commit()
        return new_id
    finally:
        conn.close()


def _upscale_files_of(row):
    """File list of an upscale row: the JSON column, or the single-file pair."""
    import json as _json

    raw = row["files"] if "files" in row.keys() else None
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return parsed
        except Exception:
            pass
    fp = row["file_path"]
    return [{"file_path": fp, "output_path": row["output_path"] or fp}]


def append_download_upscale_file(queue_item_id, title, file_path, output_path):
    """Attach ONE finished episode to the open upscale job of a download.

    A season download gets a single upscale queue entry that grows episode by
    episode: the first finished episode creates it, every later one is
    appended. That way upscaling starts while the rest of the season is still
    downloading, and no job is ever queued for files that don't exist yet.

    The whole read-modify-write runs in one BEGIN IMMEDIATE transaction, so two
    episodes finishing at the same moment can't clobber each other's append.
    An entry is only reused while it is still 'queued' or 'running' AND the
    worker hasn't passed the end of the current list yet (current_file_idx <
    total_files); otherwise a fresh entry is created, because appending to a
    job the worker is about to finalize would silently drop the file.

    Returns (upscale_id, created) — created=True when a new entry was made,
    (None, False) when the user cancelled this download's upscale job (that
    decision applies to the rest of the season as well).

    Used by: web/upscale_worker.py (_trigger_episode_after_download_upscale).
    """
    import json as _json

    fp  = str(file_path)
    out = str(output_path) if output_path else fp
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = None
        if queue_item_id is not None:
            row = conn.execute(
                "SELECT * FROM upscale_queue "
                "WHERE queue_item_id = ? AND status IN ('queued','running') "
                "  AND current_file_idx < total_files "
                "ORDER BY id DESC LIMIT 1",
                (queue_item_id,),
            ).fetchone()

        if row is not None:
            files = _upscale_files_of(row)
            if any(f.get("file_path") == fp for f in files):
                conn.execute("ROLLBACK")
                return row["id"], False
            files.append({"file_path": fp, "output_path": out})
            conn.execute(
                "UPDATE upscale_queue SET files = ?, total_files = ? WHERE id = ?",
                (_json.dumps(files), len(files), row["id"]),
            )
            conn.execute("COMMIT")
            return row["id"], False

        if queue_item_id is not None:
            # The user cancelled this download's upscale job -> that decision
            # covers the rest of the season too, no new entry.
            cancelled = conn.execute(
                "SELECT 1 FROM upscale_queue WHERE queue_item_id = ? AND status = 'cancelled' LIMIT 1",
                (queue_item_id,),
            ).fetchone()
            if cancelled:
                conn.execute("ROLLBACK")
                return None, False

        cur = conn.execute(
            "INSERT INTO upscale_queue (title, file_path, output_path, files, total_files, source, queue_item_id) "
            "VALUES (?, ?, ?, ?, ?, 'download', ?)",
            (title, fp, out, _json.dumps([{"file_path": fp, "output_path": out}]), 1, queue_item_id),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE upscale_queue SET position = ? WHERE id = ?", (new_id, new_id))
        conn.execute("COMMIT")
        return new_id, True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_upscale_files(item_id):
    """Current file list of an upscale job (re-read while it runs).

    The worker calls this before every file because a download can append
    further episodes to a job that is already running.

    Used by: web/upscale_worker.py.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return []
        return _upscale_files_of(row)
    finally:
        conn.close()


def finalize_upscale_item(item_id, processed_count):
    """Close a finished upscale job — unless new files were appended meanwhile.

    Marks the job as done by setting current_file_idx = total_files inside a
    BEGIN IMMEDIATE transaction, which is the same lock
    append_download_upscale_file() takes. Either the append wins (and this
    returns False, so the worker keeps going) or this wins (and the append
    creates its own new entry). Without that handshake the last episode of a
    season could land in a job the worker had already walked past.

    Returns True when the job is really done.

    Used by: web/upscale_worker.py.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT total_files FROM upscale_queue WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return True
        if int(row["total_files"] or 0) > int(processed_count):
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE upscale_queue SET current_file_idx = total_files WHERE id = ?",
            (item_id,),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_upscale_queue():
    """Every upscale job, oldest position first.

    `username` is joined in from the download row this job came from
    (`queue_item_id`): the upscale queue has no owner of its own, but every job
    that a download produced belongs to whoever queued that download. NULL for
    a job started straight from the library -- that route is admin-only, so
    those are the instance's own.

    Used by routes/upscale.py to show a non-admin their own jobs in full and
    everyone else's as anonymous placeholders.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT u.*, d.username AS username FROM upscale_queue u "
            "LEFT JOIN download_queue d ON d.id = u.queue_item_id "
            "ORDER BY u.position ASC, u.id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_upscale_item(item_id):
    """One upscale job, with the owner joined in -- see get_upscale_queue()."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT u.*, d.username AS username FROM upscale_queue u "
            "LEFT JOIN download_queue d ON d.id = u.queue_item_id "
            "WHERE u.id = ?", (item_id,)
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
        # Skip anything an encode still has to touch, and take the next
        # candidate instead of stalling on the head of the queue. Encoding
        # comes first in the chain (Download → Encoding → Upscaling), so a
        # merely QUEUED encode is enough to hold an upscale back.
        blocked = _busy_paths(conn, "encoding_queue", ("queued", "running"))
        rows = conn.execute(
            "SELECT * FROM upscale_queue WHERE status = 'queued' ORDER BY position ASC, id ASC"
        ).fetchall()
        row = None
        for candidate in rows:
            if not (_row_paths(candidate) & blocked):
                row = candidate
                break
        if row is None:
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


def clear_upscale_completed(username=None):
    """Delete finished upscale jobs.

    `username` limits it to jobs that came from that account's downloads, so a
    non-admin tidying up cannot clear everyone else's rows. None means all of
    them, which is the admin case and the previous behaviour. Jobs with no
    owner (started from the library, an admin-only route) count as the
    instance's and are only cleared by an admin.
    """
    conn = get_db()
    try:
        query = ("DELETE FROM upscale_queue "
                 "WHERE status IN ('completed', 'failed', 'cancelled')")
        params = ()
        if username:
            query += (" AND queue_item_id IN "
                      "(SELECT id FROM download_queue WHERE username = ?)")
            params = (username,)
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def get_queue_badge_info():
    """Number of queued/running download items plus their series URLs.

    Everything the sidebar badge and the "running" markers on browse cards
    need. The badge poll used to call get_queue(), which is a SELECT * over
    the whole table including the episodes JSON blob (often several KB per
    job) -- a few hundred KB every 5 seconds per open tab, to display one
    number.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, series_url, title, status, current_episode, total_episodes, "
            "username "
            "FROM download_queue "
            "WHERE status IN ('queued', 'running') AND hidden = 0"
        ).fetchall()
        # The home page's run strip needs the running job itself, not just the
        # count -- and it may not poll /api/queue at all (that endpoint is only
        # alive while the queue hub is open). These are five scalar columns, so
        # the reason this query exists (no episodes JSON blob) still holds.
        running = None
        queued = 0
        for r in rows:
            if r["status"] == "running":
                if running is None:
                    running = {
                        "id": r["id"],
                        "title": r["title"],
                        "status": "running",
                        "series_url": r["series_url"],
                        "current_episode": r["current_episode"],
                        "total_episodes": r["total_episodes"],
                        # So the caller can tell whether this job is the
                        # current account's before showing its title.
                        "username": r["username"],
                    }
            else:
                queued += 1
        return {
            "active": len(rows),
            "urls": [r["series_url"] for r in rows if r["series_url"]],
            # The same list per owner. `urls` drives the "currently
            # downloading" marker on browse cards, so handing the whole list
            # to every account would say which series the others are fetching
            # -- visible without ever opening the queue. The caller picks
            # which of the two it may use (see routes/queue.py's badge).
            "urls_by_owner": {
                owner: [r["series_url"] for r in rows
                        if r["series_url"] and r["username"] == owner]
                for owner in {r["username"] for r in rows}
            },
            "running": running,
            "queued": queued,
        }
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
