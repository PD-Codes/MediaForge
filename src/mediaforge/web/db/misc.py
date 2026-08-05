"""Watch progress, reading progress and bookmarks, uptime heartbeats, dev infos.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import _sql_chunks, get_db

logger = get_logger(__name__)


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


def clear_watch_progress(file_paths, username=None) -> int:
    """Forget the watch position for one or more files. Returns rows removed.

    Deleting the row rather than writing position 0 with watched=0: those two
    states look the same to every reader except "Continue watching", which
    lists *unfinished* positions and would happily offer a title you had just
    said you never watched.

    Scoped to one user, like every other read of this table -- marking
    something unwatched is a personal statement, not a fact about the file.
    """
    paths = [str(p) for p in (file_paths or []) if p]
    if not paths:
        return 0
    user = _normalize_user(username)
    conn = get_db()
    try:
        removed = 0
        for chunk, placeholders in _sql_chunks(paths):
            cur = conn.execute(
                "DELETE FROM watch_progress WHERE username = ? AND file_path IN (%s)"
                % placeholders, [user] + chunk)
            removed += cur.rowcount or 0
        conn.commit()
        return removed
    except sqlite3.Error:
        return 0
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


def get_recent_watch_progress(username=None, limit: int = 15) -> list:
    """Most recently touched, *unfinished* playback positions for one user.

    Feeds the home page's "Continue watching" row. The filters are what makes
    that row useful rather than noisy: anything already marked watched is out,
    so is a position under 30 s (opened the wrong episode) and anything past
    95 % of its runtime (finished in practice, even if the player never got to
    write watched=1 because the tab was closed on the credits).
    """
    limit = max(1, min(int(limit or 15), 100))
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT file_path, position_seconds, duration_seconds, watched, updated_at
                 FROM watch_progress
                WHERE username = ?
                  AND watched = 0
                  AND position_seconds > 30
                  AND (duration_seconds <= 0
                       OR position_seconds < duration_seconds * 0.95)
             ORDER BY updated_at DESC
                LIMIT ?""",
            (_normalize_user(username), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_watch_progress_bulk(file_paths: list, username=None) -> dict:
    """Return {file_path: progress_dict} for a list of paths, for one user."""
    if not file_paths:
        return {}
    user = _normalize_user(username)
    conn = get_db()
    try:
        result = {}
        for chunk, placeholders in _sql_chunks(file_paths):
            rows = conn.execute(
                f"SELECT file_path, position_seconds, duration_seconds, watched "
                f"FROM watch_progress WHERE username = ? AND file_path IN ({placeholders})",
                [user, *chunk],
            ).fetchall()
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


# ══════════════════════════════════════════════════════════════════════════
#  Reading progress (eBooks)
# ══════════════════════════════════════════════════════════════════════════
#  Same shape as watch_progress above, with one deliberate difference: the
#  key is the BOOK, not the file. A book routinely exists as EPUB, MOBI and
#  PDF at once (see web/books/identity.py), and someone who starts in the
#  EPUB and later opens the PDF has not started a different book.
#
#  `username` is a TEXT column rather than a user id, for the same reason
#  watch_progress uses one: the no-auth install has no user table to point at,
#  and '' is the shared bucket every such row lands in.

_CREATE_READING_PROGRESS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_progress (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL DEFAULT '',
    book_key   TEXT    NOT NULL,
    location   TEXT    NOT NULL DEFAULT '',
    percent    REAL    NOT NULL DEFAULT 0,
    finished   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(book_key, username)
)
"""


def init_reading_progress_db() -> None:
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_READING_PROGRESS_TABLE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reading_progress_user "
            "ON reading_progress(username, updated_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def save_reading_progress(book_key: str, location: str, percent: float, username=None) -> None:
    """Upsert the reading position for one book and user.

    A book counts as finished at 98%: unlike a film, the last pages of a book
    are usually an afterword or an index, so 95% would mark a book finished
    while a chapter is still open.
    """
    finished = 1 if percent >= 98 else 0
    user = _normalize_user(username)
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO reading_progress (username, book_key, location, percent, finished, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(book_key, username) DO UPDATE SET
                    location   = excluded.location,
                    percent    = excluded.percent,
                    finished   = excluded.finished,
                    updated_at = datetime('now')""",
            (user, book_key, location, float(percent), finished),
        )
        conn.commit()
    finally:
        conn.close()


def get_reading_progress(book_key: str, username=None) -> dict:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT location, percent, finished FROM reading_progress "
            "WHERE book_key = ? AND username = ?",
            (book_key, _normalize_user(username)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    return {
        "location": row["location"],
        "percent": round(row["percent"], 2),
        "finished": bool(row["finished"]),
    }


def get_reading_progress_bulk(book_keys, username=None) -> dict:
    keys = [k for k in (book_keys or []) if k]
    if not keys:
        return {}
    user = _normalize_user(username)
    out: dict = {}
    conn = get_db()
    try:
        # Chunked because SQLite caps the number of bound variables (999 by
        # default) -- a shelf with a thousand books would otherwise raise.
        for chunk, placeholders in _sql_chunks(keys):
            rows = conn.execute(
                "SELECT book_key, location, percent, finished FROM reading_progress "
                "WHERE username = ? AND book_key IN (%s)" % placeholders,
                [user] + chunk,
            ).fetchall()
            for row in rows:
                out[row["book_key"]] = {
                    "location": row["location"],
                    "percent": round(row["percent"], 2),
                    "finished": bool(row["finished"]),
                }
    finally:
        conn.close()
    return out


def get_recent_reading_progress(username=None, limit: int = 15) -> list:
    """Books that are started but not finished -- the "continue reading" row."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT book_key, location, percent, updated_at FROM reading_progress "
            "WHERE username = ? AND finished = 0 AND percent > 1 "
            "ORDER BY updated_at DESC LIMIT ?",
            (_normalize_user(username), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_reading_progress(book_key: str, username=None) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM reading_progress WHERE book_key = ? AND username = ?",
            (book_key, _normalize_user(username)),
        )
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Bookmarks (eBooks)
# ══════════════════════════════════════════════════════════════════════════
#  A position and a bookmark answer different questions. reading_progress
#  holds exactly one row per book -- where you stopped -- and is overwritten
#  every few seconds. A bookmark is a place you chose, there can be many, and
#  nothing but the reader deleting it may ever remove one.
#
#  Keyed by the book rather than the file for the same reason the position is:
#  a bookmark set in the EPUB should still be in the list when the PDF of the
#  same book is opened. It will not resolve there -- a CFI means nothing to a
#  page number -- so `kind` records which engine wrote it and the reader only
#  offers the ones it can actually jump to.

_CREATE_READING_BOOKMARKS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_bookmarks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL DEFAULT '',
    book_key   TEXT    NOT NULL,
    location   TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'epub',
    label      TEXT    NOT NULL DEFAULT '',
    percent    REAL    NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(book_key, username, location)
)
"""

# One reader cannot meaningfully keep hundreds of marks in one book, and an
# unbounded list is an unbounded response on every open.
MAX_BOOKMARKS_PER_BOOK = 200


def init_reading_bookmarks_db() -> None:
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_READING_BOOKMARKS_TABLE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reading_bookmarks_book "
            "ON reading_bookmarks(username, book_key, created_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def add_reading_bookmark(book_key, location, kind="epub", label="", percent=0.0, username=None):
    """Set a bookmark. Setting the same place twice is not an error."""
    user = _normalize_user(username)
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_bookmarks WHERE username = ? AND book_key = ?",
            (user, book_key),
        ).fetchone()
        if existing and existing["n"] >= MAX_BOOKMARKS_PER_BOOK:
            return False
        conn.execute(
            """INSERT INTO reading_bookmarks
                    (username, book_key, location, kind, label, percent, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(book_key, username, location) DO UPDATE SET
                    label   = excluded.label,
                    percent = excluded.percent""",
            (user, book_key, location, kind or "epub", label or "", float(percent or 0)),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def list_reading_bookmarks(book_key: str, username=None) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT location, kind, label, percent, created_at FROM reading_bookmarks "
            "WHERE username = ? AND book_key = ? ORDER BY percent ASC, created_at ASC",
            (_normalize_user(username), book_key),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_reading_bookmark(book_key: str, location: str, username=None) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM reading_bookmarks WHERE username = ? AND book_key = ? AND location = ?",
            (_normalize_user(username), book_key, location),
        )
        conn.commit()
    finally:
        conn.close()


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
    """Delete heartbeats older than the retention window.

    Age is the only criterion. Rows belonging to a source that no longer
    exists (an uninstalled third-party monitor site) are deliberately left to
    expire on their own rather than deleted eagerly: reinstalling the module
    within the retention window then still shows its history, and the retention
    window caps the wasted space at a few days either way.
    """
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

    uptime_pct counts only genuinely 'up' heartbeats. It used to count
    everything that was not 'down', which quietly inflated the number: the
    failure-threshold debounce deliberately records the first
    ``failure_threshold - 1`` rounds of a real outage as 'degraded', so every
    outage was reported shorter than it was. down_count/degraded_count are
    returned alongside so a caller can still tell the two apart.
    """
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    if end_ts <= start_ts:
        end_ts = start_ts + 1
    n_buckets = max(1, int(n_buckets))
    span = end_ts - start_ts
    # Bucket index by proportion, not by a truncated fixed width. With
    # ``size = span // n_buckets`` the integer remainder (up to n_buckets-1
    # seconds, i.e. 49 s on a 7-day range) fell outside the last bucket's
    # nominal end and got clamped into it, so that one bar aggregated more
    # checks than every other bar and read as busier than it was.
    size = max(1, span // n_buckets)  # reported as bucket_seconds (nominal)

    def _bucket_edge(i):
        # Ceiling, so this is the exact inverse of the floor-division index
        # below (and of the same expression in SQL). Flooring here instead
        # would put a heartbeat one second outside the bar it was counted in,
        # which the tooltip then reported as a span not containing its own
        # checks on any range that does not divide evenly.
        return start_ts + (i * span + n_buckets - 1) // n_buckets

    def _bucket_index(ts):
        idx = ((int(ts) - start_ts) * n_buckets) // span
        return 0 if idx < 0 else (n_buckets - 1 if idx >= n_buckets else idx)

    conn = get_db()
    try:
        agg = {}
        for r in conn.execute(
            "SELECT CAST(((ts - ?) * ?) / ? AS INTEGER) AS b, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS downc, "
            "SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degc, "
            "SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) AS upc, "
            "SUM(response_ms) AS rt_sum, "
            "SUM(CASE WHEN response_ms IS NOT NULL THEN 1 ELSE 0 END) AS rt_n "
            "FROM uptime_heartbeats WHERE source=? AND ts>=? AND ts<? GROUP BY b",
            (start_ts, n_buckets, span, source, start_ts, end_ts),
        ).fetchall():
            idx = r["b"] or 0
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
            issues[_bucket_index(r["ts"])] = {
                "ts": r["ts"], "status": r["status"], "message": r["message"]}

        buckets = []
        for i in range(n_buckets):
            b_start = _bucket_edge(i)
            b_end = end_ts if i == n_buckets - 1 else _bucket_edge(i + 1)
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
            "SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) AS up_count, "
            "SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS down_count, "
            "SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degraded_count, "
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
        down_count = (stat["down_count"] if stat else 0) or 0
        degraded_count = (stat["degraded_count"] if stat else 0) or 0
        avg_ms = stat["avg_ms"] if stat and stat["avg_ms"] is not None else None
        return {
            "stats": {
                "total": total,
                "up_count": up_count,
                "uptime_pct": round(up_count / total * 100, 2) if total else None,
                "down_count": down_count,
                "degraded_count": degraded_count,
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
    fetched_at        INTEGER,
    -- Only populated for type="release" posts: the version the post announces
    -- plus that release's notes, as delivered by the devInfo server's
    -- /api/posts (which resolves them from its cached GitHub release list).
    -- Cached here with the post so the changelog box still renders when the
    -- devInfo server is unreachable, and so nothing in this app ever talks to
    -- GitHub directly.
    release_tag       TEXT,
    release_name      TEXT,
    release_notes     TEXT,
    release_url       TEXT,
    release_published_at TEXT
)
"""

# Read-state lives in its own table, deliberately separate from devinfo_posts:
# replace_devinfo_posts() below does a full DELETE + reinsert of that table on
# every poll round (every 5 min, plus on-page-visit), so a "read" flag stored
# as a column on devinfo_posts would get silently wiped the next time the feed
# refreshes. Keying this table by the post's own id (not a local rowid) means
# a read post stays read across those wipes, as long as the remote server
# keeps handing back the same id for it.
#
# That last condition is why the id stored here must be the devInfo server's
# ``uid`` (a UUID), not its integer ``id``: the integer is an SQLite rowid on
# that side and gets handed out again after a delete, so a brand-new post
# could land on the number of a deleted one and inherit its row below --
# arriving already marked as read. devinfos_monitor.py reads ``uid`` first for
# exactly this reason. The stale numeric rows left over from before that
# change need no migration: replace_devinfo_posts() prunes every read id that
# is not in the current batch, and after the switch no batch contains them.
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
        # Migration: release columns, added with the "release" post type. Same
        # try/except-per-column shape as above -- SQLite has no
        # "ADD COLUMN IF NOT EXISTS", and a duplicate-column error is the
        # cheapest possible "already migrated" check.
        for _col in ("release_tag", "release_name", "release_notes",
                     "release_url", "release_published_at"):
            try:
                conn.execute(f"ALTER TABLE devinfo_posts ADD COLUMN {_col} TEXT")
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
                "(id, title, body, type, author, remote_created_at, fetched_at, "
                " release_tag, release_name, release_notes, release_url, "
                " release_published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(p.get("id")),
                    p.get("title"),
                    p.get("body"),
                    p.get("type"),
                    p.get("author"),
                    p.get("remote_created_at"),
                    now,
                    p.get("release_tag"),
                    p.get("release_name"),
                    p.get("release_notes"),
                    p.get("release_url"),
                    p.get("release_published_at"),
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
            "p.fetched_at, p.release_tag, p.release_name, p.release_notes, "
            "p.release_url, p.release_published_at, (r.id IS NOT NULL) AS is_read "
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
