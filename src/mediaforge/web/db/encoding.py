"""The encoding queue.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import DB_PATH, get_db

logger = get_logger(__name__)


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
    position         INTEGER NOT NULL DEFAULT 0,
    -- Which download queue item produced this job, and whether that download
    -- asked for upscaling. Encoding and upscaling must not run on the same
    -- file at the same time, so with both set to "after download" the upscale
    -- job is no longer queued next to the encode -- the encoding worker hands
    -- the finished file over instead. See web/encoding_worker.py.
    queue_item_id    INTEGER,
    upscale_after    INTEGER NOT NULL DEFAULT 0
);
"""


def init_encoding_queue_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_ENCODING_QUEUE_TABLE)
        # Migrate existing DBs: add new columns if missing
        for col, definition in [
            ("queue_item_id", "INTEGER"),
            ("upscale_after", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE encoding_queue ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        conn.execute("UPDATE encoding_queue SET position = id WHERE position = 0")
        # Used by encoding_pending_for_paths() on every upscale claim.
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_encoding_queue_status "
                "ON encoding_queue (status)"
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def _row_paths(row):
    """Every path a queue row touches: input and output, single or batch.

    Both queues write a temp file and then move it over their target, so an
    overlap on EITHER side is a collision -- the input one job reads may be the
    output another is about to replace.
    """
    import json as _json

    out = set()
    keys = row.keys()
    for key in ("file_path", "output_path"):
        if key in keys and row[key]:
            out.add(str(row[key]))
    raw = row["files"] if "files" in keys else None
    if raw:
        try:
            entries = _json.loads(raw) or []
        except Exception:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("file_path", "output_path"):
                val = entry.get(key)
                if val:
                    out.add(str(val))
    return out


def _busy_paths(conn, table, statuses):
    """All paths held by rows of `table` in one of `statuses`."""
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT file_path, output_path, files FROM {table} WHERE status IN ({marks})",
        tuple(statuses),
    ).fetchall()
    busy = set()
    for row in rows:
        busy |= _row_paths(row)
    return busy


def add_to_encoding_queue(title, file_path, output_path=None, source="manual", files=None,
                          queue_item_id=None, upscale_after=False):
    """Add one encoding job.
    files: list of {file_path, output_path} for multi-file (batch) jobs.
    When files is set, file_path/output_path are taken from files[0].
    queue_item_id/upscale_after: set by the after-download trigger so the
    encoding worker can hand the finished file to the upscale queue itself
    instead of both queues racing on the same file.
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
            "INSERT INTO encoding_queue (title, file_path, output_path, files, total_files, source,"
            " queue_item_id, upscale_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, fp, out, files_json, total, source,
             queue_item_id, 1 if upscale_after else 0),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE encoding_queue SET position = ? WHERE id = ?", (new_id, new_id))
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_encoding_queue():
    """Every encoding job, oldest position first.

    `username` is joined in from the download row this job came from
    (`queue_item_id`): the encoding queue has no owner of its own, but every job
    that a download produced belongs to whoever queued that download. NULL for
    a job started straight from the library -- that route is admin-only, so
    those are the instance's own.

    Used by routes/encoding.py to show a non-admin their own jobs in full and
    everyone else's as anonymous placeholders.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT e.*, d.username AS username FROM encoding_queue e "
            "LEFT JOIN download_queue d ON d.id = e.queue_item_id "
            "ORDER BY e.position ASC, e.id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_encoding_item(item_id):
    """One encoding job, with the owner joined in -- see get_encoding_queue()."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT e.*, d.username AS username FROM encoding_queue e "
            "LEFT JOIN download_queue d ON d.id = e.queue_item_id "
            "WHERE e.id = ?", (item_id,)
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
        # Mirror of the upscale claim, with one deliberate asymmetry: only a
        # RUNNING upscale blocks an encode, never a merely queued one. If both
        # sides waited on each other's queued rows, an item sitting in both
        # queues would block itself forever -- each claim skipping it because
        # the other queue holds it. Encoding is first in the chain, so it wins
        # the tie; a running upscale is finite and will release the file.
        blocked = _busy_paths(conn, "upscale_queue", ("running",))
        rows = conn.execute(
            "SELECT * FROM encoding_queue WHERE status = 'queued' ORDER BY position ASC, id ASC"
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


def clear_encoding_completed(username=None):
    """Delete finished encoding jobs.

    `username` limits it to jobs that came from that account's downloads, so a
    non-admin tidying up cannot clear everyone else's rows. None means all of
    them, which is the admin case and the previous behaviour. Jobs with no
    owner (started from the library, an admin-only route) count as the
    instance's and are only cleared by an admin.
    """
    conn = get_db()
    try:
        query = ("DELETE FROM encoding_queue "
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
