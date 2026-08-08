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
        # Alternative names for a folder on disk, so "is this already
        # downloaded?" stops depending on which provider you arrived from. A
        # folder is named after whichever provider downloaded it first; every
        # other provider then compares its own spelling of the title against
        # that name, and a provider that calls the show something else entirely
        # ("Kyoukaisen-jou no Horizon" vs "Horizon in the Middle of Nowhere")
        # could never match no matter how good the string comparison was.
        #
        # Its own table rather than a column on library_cache: that table holds
        # ONE row per scan target with the whole title tree as an opaque JSON
        # blob, so it can be neither queried nor updated per folder. And not in
        # tmdb_cache either -- that is a 24 h TTL key-value store keyed by the
        # asking title, whereas this has to survive eviction (it is a fact about
        # a folder, not a cached response) and be searchable by any of the names.
        #
        # `folder` is stored lower-cased, matching get_media_ignores()'s
        # convention -- the same folder arriving with different casing from two
        # scan roots must not become two rows.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_aliases (
                folder      TEXT PRIMARY KEY,
                tmdb_id     TEXT NOT NULL DEFAULT '',
                media_type  TEXT NOT NULL DEFAULT '',
                aliases     TEXT NOT NULL DEFAULT '[]',
                resolved_at REAL NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def set_library_aliases(folder, aliases, tmdb_id="", media_type="") -> None:
    """Record the alternative names *folder* is known by.

    An empty *aliases* list is a meaningful result and is stored as such: it
    means "we asked and TMDB had nothing useful", and writing it is what stops
    the resolver asking about the same folder on every pass. `resolved_at` is
    what a caller uses to decide a row is old enough to be worth re-checking.
    """
    import json as _json
    import time as _time
    key = str(folder or "").strip().lower()
    if not key:
        return
    clean = []
    for name in (aliases or []):
        text = str(name or "").strip()
        if text and text not in clean:
            clean.append(text)
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO library_aliases (folder, tmdb_id, media_type, aliases, resolved_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(folder) DO UPDATE SET
                   tmdb_id = excluded.tmdb_id,
                   media_type = excluded.media_type,
                   aliases = excluded.aliases,
                   resolved_at = excluded.resolved_at""",
            (key, str(tmdb_id or ""), str(media_type or ""),
             _json.dumps(clean, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_library_aliases() -> dict:
    """``{folder_lower: {"tmdb_id", "media_type", "aliases", "resolved_at"}}``.

    One query for the whole table on purpose: the callers ("does any folder
    hold this title?") need to test an arbitrary name against every folder, so
    a per-folder lookup would be a query per card.
    """
    import json as _json
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT folder, tmdb_id, media_type, aliases, resolved_at FROM library_aliases"
        ).fetchall()
    except Exception:
        # Table not created yet (a caller that runs before init_library_db()).
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            aliases = _json.loads(r["aliases"] or "[]")
        except Exception:
            aliases = []
        out[r["folder"]] = {
            "tmdb_id": r["tmdb_id"] or "",
            "media_type": r["media_type"] or "",
            "aliases": aliases if isinstance(aliases, list) else [],
            "resolved_at": r["resolved_at"] or 0,
        }
    return out


def prune_library_aliases(known_folders) -> int:
    """Drop rows for folders that are no longer on disk. Returns the count.

    Without this the table grows forever: a renamed or deleted folder leaves a
    row that keeps claiming its aliases, so a title could report as downloaded
    on the strength of a folder that does not exist any more.
    """
    keep = {str(f or "").strip().lower() for f in (known_folders or []) if f}
    conn = get_db()
    try:
        rows = conn.execute("SELECT folder FROM library_aliases").fetchall()
        stale = [r["folder"] for r in rows if r["folder"] not in keep]
        if not stale:
            return 0
        # Chunked: SQLite caps the number of bound parameters, and a large
        # library can exceed it in one statement.
        for i in range(0, len(stale), 400):
            chunk = stale[i:i + 400]
            conn.execute(
                "DELETE FROM library_aliases WHERE folder IN (%s)"
                % ",".join("?" * len(chunk)),
                chunk,
            )
        conn.commit()
        return len(stale)
    except Exception:
        return 0
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
