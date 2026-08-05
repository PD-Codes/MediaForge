"""Per-episode download history and its retention prune.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import datetime as _dt
import sqlite3
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import _sql_chunks, get_db

logger = get_logger(__name__)


_CREATE_DOWNLOAD_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    title TEXT NOT NULL,
    series_url TEXT,
    episode_url TEXT,
    season INTEGER,
    episode INTEGER,
    language TEXT,
    provider TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    username TEXT,
    target_path TEXT,
    size_mb REAL,
    avg_speed_mbps REAL,
    duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_download_history_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_DOWNLOAD_HISTORY_TABLE)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_history_finished "
            "ON download_history(finished_at DESC)"
        )
        # The history page filters by user and by status before ordering. With
        # only the finished_at index those filters were a full scan of a table
        # that is meant to grow into six figures.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_history_user_finished "
            "ON download_history(username, finished_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_history_status "
            "ON download_history(status)"
        )
        # Migration: add error column for existing DBs
        try:
            conn.execute("ALTER TABLE download_history ADD COLUMN error TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def add_download_history(
    title, *, queue_id=None, series_url=None, episode_url=None, season=None,
    episode=None, language=None, provider=None, source="manual", username=None,
    target_path=None, size_mb=None, avg_speed_mbps=None, duration_sec=None,
    status="completed", error=None, started_at=None, finished_at=None,
):
    """Record a single finished (or failed) episode download. Returns the new id."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO download_history "
            "(queue_id, title, series_url, episode_url, season, episode, language, "
            " provider, source, username, target_path, size_mb, avg_speed_mbps, "
            " duration_sec, status, error, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (queue_id, title, series_url, episode_url, season, episode, language,
             provider, source, username, target_path, size_mb, avg_speed_mbps,
             duration_sec, status, error, started_at, finished_at),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_download_history_meta_for_path(target_path: str):
    """Return {provider, title, season, episode} from the most recent
    download_history row whose target_path matches, or None if no match.

    Used by: telemetry instrumentation in routes/progress.py, to look up
    which provider/title a watched *file path* (all that
    api_progress_save() receives from the player) actually came from --
    needed both for the watch.* event payload and, critically, to apply the
    hanime_tv exclusion guard (sanitize.is_adult_provider()) correctly, since
    provider is not otherwise known at watch-progress time. Best-effort: a
    file played from outside the download history (e.g. manually placed in
    the library) simply yields no provider/title, and the caller treats a
    lookup miss as "unknown provider" (never as "safe to send").
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT provider, title, season, episode FROM download_history "
            "WHERE target_path = ? ORDER BY id DESC LIMIT 1",
            (target_path,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _history_where(username=None, search=None, status=None, source=None, since=None,
                   provider=None, language=None):
    """Build a (where_sql, params) pair shared by list/summary/export/clear."""
    where = []
    params = []
    if username:
        where.append("username = ?")
        params.append(username)
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    if since:
        where.append("COALESCE(finished_at, created_at) >= ?")
        params.append(since)
    if provider and provider != "all":
        where.append("provider = ?")
        params.append(provider)
    if language and language != "all":
        where.append("language = ?")
        params.append(language)
    if search:
        # Escape the LIKE wildcards so a title containing % or _ searches for
        # those characters instead of matching everything.
        esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("title LIKE ? ESCAPE '\\'")
        params.append("%" + esc + "%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


# Sort key -> ORDER BY expression. A whitelist, never string interpolation of
# the raw parameter: the value comes straight from a query string.
HISTORY_SORT_COLUMNS = {
    "date": "COALESCE(finished_at, created_at)",
    "title": "title COLLATE NOCASE",
    "size": "COALESCE(size_mb, -1)",
    "duration": "COALESCE(duration_sec, -1)",
    "speed": "COALESCE(avg_speed_mbps, -1)",
    "status": "status",
}


def get_download_history(username=None, search=None, status=None, source=None,
                         since=None, limit=50, offset=0,
                         provider=None, language=None, sort="date", direction="desc"):
    """Return (entries, total). If *username* is given, scope to that user."""
    conn = get_db()
    try:
        where_sql, params = _history_where(username, search, status, source, since,
                                           provider, language)
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_history" + where_sql, params
        ).fetchone()["cnt"]
        order_expr = HISTORY_SORT_COLUMNS.get(sort, HISTORY_SORT_COLUMNS["date"])
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        rows = conn.execute(
            "SELECT * FROM download_history" + where_sql +
            f" ORDER BY {order_expr} {order_dir}, id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_download_history_summary(username=None, search=None, status=None, source=None,
                                 since=None, provider=None, language=None, days=30):
    """Aggregate the *filtered* history into chart-ready figures.

    Everything is grouped in SQLite rather than in Python, so the cost tracks
    the number of result rows, not the size of the history table (which the
    Statistics rework showed can be six figures).

    Returns totals, a per-day series over the last `days` days, and the
    breakdown by status / provider / source / language.
    """
    days = max(1, min(365, int(days or 30)))
    conn = get_db()
    try:
        where_sql, params = _history_where(username, search, status, source, since,
                                           provider, language)

        tot = conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS ok_cnt, "
            "       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_cnt, "
            "       SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_cnt, "
            "       SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_cnt, "
            "       COALESCE(SUM(size_mb), 0) AS size_mb, "
            "       AVG(avg_speed_mbps) AS spd, MAX(avg_speed_mbps) AS max_spd, "
            "       COALESCE(SUM(duration_sec), 0) AS dur, "
            "       COUNT(DISTINCT title) AS titles "
            "FROM download_history" + where_sql, params
        ).fetchone()
        cnt = tot["cnt"] or 0
        ok = tot["ok_cnt"] or 0

        # Per-day series. localtime() so "per day" matches the user's calendar.
        day_where = where_sql + (" AND " if where_sql else " WHERE ") + \
            "COALESCE(finished_at, created_at) IS NOT NULL " \
            "AND date(COALESCE(finished_at, created_at), 'localtime') >= date('now', 'localtime', ?)"
        day_rows = conn.execute(
            "SELECT date(COALESCE(finished_at, created_at), 'localtime') AS d, "
            "       COUNT(*) AS cnt, "
            "       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS ok_cnt, "
            "       SUM(CASE WHEN status <> 'completed' THEN 1 ELSE 0 END) AS bad_cnt, "
            "       COALESCE(SUM(size_mb), 0) AS size_mb "
            "FROM download_history" + day_where + " GROUP BY d ORDER BY d",
            params + [f"-{days - 1} days"],
        ).fetchall()
        by_day = {r["d"]: r for r in day_rows}
        today = _dt.datetime.now().date()
        daily = []
        for i in range(days - 1, -1, -1):
            key = (today - _dt.timedelta(days=i)).isoformat()
            r = by_day.get(key)
            daily.append({
                "date": key,
                "downloads": r["cnt"] if r else 0,
                "completed": (r["ok_cnt"] or 0) if r else 0,
                "failed": (r["bad_cnt"] or 0) if r else 0,
                "size_mb": round(r["size_mb"] or 0.0, 2) if r else 0.0,
            })

        def _group(column):
            # Column names are literals from this function only, never input.
            return [
                {"name": r["k"] or "", "count": r["cnt"],
                 "size_mb": round(r["size_mb"] or 0.0, 2)}
                for r in conn.execute(
                    f"SELECT COALESCE({column}, '') AS k, COUNT(*) AS cnt, "
                    "COALESCE(SUM(size_mb), 0) AS size_mb "
                    "FROM download_history" + where_sql +
                    " GROUP BY k ORDER BY cnt DESC LIMIT 12", params
                ).fetchall()
            ]

        return {
            "totals": {
                "entries": cnt,
                "completed": ok,
                "failed": tot["failed_cnt"] or 0,
                "cancelled": tot["cancelled_cnt"] or 0,
                "skipped": tot["skipped_cnt"] or 0,
                "titles": tot["titles"] or 0,
                "size_mb": round(tot["size_mb"] or 0.0, 2),
                "avg_speed_mbps": round(tot["spd"], 3) if tot["spd"] else None,
                "max_speed_mbps": round(tot["max_spd"], 3) if tot["max_spd"] else None,
                "duration_sec": round(tot["dur"] or 0.0, 1),
                "success_rate": round(ok / cnt * 100) if cnt else 0,
            },
            "days": days,
            "daily": daily,
            "by_status": _group("status"),
            "by_provider": _group("provider"),
            "by_source": _group("source"),
            "by_language": _group("language"),
        }
    finally:
        conn.close()


def get_download_period_recap(username=None, start_iso=None, end_iso=None):
    """Aggregate one period of *completed* downloads, for the home Wrapped card.

    Returns ``{"count", "size_mb", "top_sources", "biggest", "top_titles"}``.

    Aggregated in SQLite rather than by reading rows into Python: the history
    is expected to reach six figures (see get_download_history_summary), and a
    recap that loads a year of rows to count them is the exact mistake the
    statistics rework had to undo.

    Timestamps are the table's own TEXT format ("YYYY-MM-DD HH:MM:SS", UTC);
    *start_iso* is inclusive, *end_iso* exclusive. Passing None for both gives
    the all-time figures.

    Note what this does NOT count, because the number surprises people: only
    rows with ``status = 'completed'`` (a cancelled or failed download is not
    a download), and only those whose ``size_mb`` was recorded -- the queue
    writes NULL when the source never reported a size, so those episodes count
    towards `count` but contribute nothing to the volume.
    """
    conn = get_db()
    try:
        where = ["status = 'completed'",
                 "COALESCE(finished_at, created_at) >= ?",
                 "COALESCE(finished_at, created_at) < ?"]
        params = [start_iso or "0000", end_iso or "9999"]
        if username:
            where.append("username = ?")
            params.append(username)
        where_sql = " WHERE " + " AND ".join(where)

        tot = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_mb), 0) AS size_mb "
            "FROM download_history" + where_sql, params).fetchone()
        sources = conn.execute(
            "SELECT provider AS name, COUNT(*) AS cnt FROM download_history" +
            where_sql + " AND provider IS NOT NULL AND provider != '' "
            "GROUP BY provider ORDER BY cnt DESC LIMIT 3", params).fetchall()
        titles = conn.execute(
            "SELECT title AS name, COUNT(*) AS cnt FROM download_history" +
            where_sql + " AND title != '' "
            "GROUP BY title COLLATE NOCASE ORDER BY cnt DESC LIMIT 3",
            params).fetchall()
        big = conn.execute(
            "SELECT title, size_mb FROM download_history" + where_sql +
            " AND size_mb IS NOT NULL ORDER BY size_mb DESC LIMIT 1",
            params).fetchone()

        return {
            "count": tot["cnt"] or 0,
            "size_mb": round(float(tot["size_mb"] or 0), 1),
            "top_sources": [{"name": r["name"], "count": r["cnt"]} for r in sources],
            "top_titles": [{"name": r["name"], "count": r["cnt"]} for r in titles],
            "biggest": ({"title": big["title"], "size_mb": round(float(big["size_mb"]), 1)}
                        if big else None),
        }
    except sqlite3.Error:
        # Table not created yet on a very fresh install.
        return {"count": 0, "size_mb": 0.0, "top_sources": [], "top_titles": [],
                "biggest": None}
    finally:
        conn.close()


def get_download_history_facets(username=None):
    """Distinct providers and languages present in the history.

    Feeds the filter dropdowns, so they only ever offer values that actually
    occur -- an empty option list is a better hint than a filter that always
    returns nothing.
    """
    conn = get_db()
    try:
        where_sql, params = _history_where(username)
        providers = [
            r["k"] for r in conn.execute(
                "SELECT DISTINCT COALESCE(provider, '') AS k FROM download_history" +
                where_sql + " ORDER BY k COLLATE NOCASE", params
            ).fetchall() if r["k"]
        ]
        languages = [
            r["k"] for r in conn.execute(
                "SELECT DISTINCT COALESCE(language, '') AS k FROM download_history" +
                where_sql + " ORDER BY k COLLATE NOCASE", params
            ).fetchall() if r["k"]
        ]
        return {"providers": providers, "languages": languages}
    finally:
        conn.close()


def get_download_history_entry(entry_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM download_history WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_download_history_entry(entry_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM download_history WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


def delete_download_history_entries(ids, username=None):
    """Delete multiple history rows by id. If *username* is given, only that
    user's rows are removed. Returns the number of rows deleted."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    conn = get_db()
    try:
        removed = 0
        for chunk, placeholders in _sql_chunks(ids):
            sql = f"DELETE FROM download_history WHERE id IN ({placeholders})"
            params = list(chunk)
            if username:
                sql += " AND username = ?"
                params.append(username)
            cur = conn.execute(sql, params)
            removed += cur.rowcount or 0
        conn.commit()
        return removed
    finally:
        conn.close()


def clear_download_history(username=None, search=None, status=None, source=None, since=None,
                           provider=None, language=None):
    """Delete history rows, optionally limited to the given filters. Returns the
    number of rows deleted."""
    conn = get_db()
    try:
        where_sql, params = _history_where(username, search, status, source, since,
                                           provider, language)
        cur = conn.execute("DELETE FROM download_history" + where_sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def prune_download_history(days):
    """Delete history rows older than *days* days. days<=0 disables pruning.
    Returns the number of rows deleted."""
    try:
        days = int(days)
    except (ValueError, TypeError):
        return 0
    if days <= 0:
        return 0
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM download_history "
            "WHERE COALESCE(finished_at, created_at) < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
