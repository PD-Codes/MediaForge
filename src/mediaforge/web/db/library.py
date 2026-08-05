"""The library scan cache and the ignored-media list.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


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


def get_library_cache_status():
    """Scan state and timestamps only -- without the cached listing.

    get_all_library_cache() selects the `data` column and json.loads() it for
    every scan target, i.e. the entire title/season/episode tree. The status
    endpoint is polled every few seconds and only needs these two numbers; on
    a 20k-episode library that was several MB of JSON parsed per poll and per
    open tab.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            # length() is computed by SQLite without materialising the value,
            # so "is there a listing?" costs nothing here.
            "SELECT path_key, scanned_at, is_scanning, length(data) AS data_len "
            "FROM library_cache"
        ).fetchall()
        return {
            r["path_key"]: {
                "scanned_at": r["scanned_at"],
                "is_scanning": bool(r["is_scanning"]),
                # '[]' (the column default) counts as empty.
                "has_data": (r["data_len"] or 0) > 2,
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


def prune_library_cache(valid_keys):
    """Delete library_cache rows whose path_key is no longer a scan target.

    Rows are keyed by path_key ("default" or a custom path's id). Nothing used
    to remove them: deleting a custom path dropped its custom_paths row but
    left the cached scan behind forever, and since a re-created path gets a
    fresh autoincrement id, the old row could never be overwritten either.

    That stale row still contained a full title/season/episode tree, and
    get_all_library_cache() hands out every row — so the Statistics media
    counts and, much more visibly, the duplicate check saw the same episode
    twice (once from the live scan, once from the orphan) and reported the
    entire library as duplicated.

    Returns the number of rows deleted.

    Used by: routes/library.py::_lib_do_scan(), after a full scan, with the
    path_keys it just refreshed.
    """
    keys = [str(k) for k in (valid_keys or [])]
    conn = get_db()
    try:
        if not keys:
            # Defensive: an empty target list means "we know nothing", not
            # "delete everything" -- never wipe the cache on a bad call.
            return 0
        placeholders = ",".join("?" for _ in keys)
        cur = conn.execute(
            f"DELETE FROM library_cache WHERE path_key NOT IN ({placeholders})", keys
        )
        conn.commit()
        return cur.rowcount or 0
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
