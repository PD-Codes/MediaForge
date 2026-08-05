"""Aggregate statistics over the queue, history and library.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import datetime as _dt
from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


def get_sync_stats():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM autosync_jobs").fetchone()[
            "cnt"
        ]
        enabled = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE enabled = 1"
        ).fetchone()["cnt"]
        disabled = total - enabled
        last_check = conn.execute(
            "SELECT MAX(last_check) AS lc FROM autosync_jobs"
        ).fetchone()["lc"]
        last_new = conn.execute(
            "SELECT MAX(last_new_found) AS ln FROM autosync_jobs"
        ).fetchone()["ln"]
        total_eps = conn.execute(
            "SELECT COALESCE(SUM(episodes_found), 0) AS s FROM autosync_jobs"
        ).fetchone()["s"]
        jobs = conn.execute(
            "SELECT id, title, series_url, language, provider, enabled, "
            "last_check, last_new_found, episodes_found, added_by, created_at "
            "FROM autosync_jobs ORDER BY id"
        ).fetchall()
        return {
            "total_jobs": total,
            "enabled": enabled,
            "disabled": disabled,
            "last_check": last_check,
            "last_new_found": last_new,
            "total_episodes_found": total_eps,
            "jobs": [dict(r) for r in jobs],
        }
    finally:
        conn.close()


def get_queue_stats(visible_only=False):
    """Counts over download_queue.

    ``visible_only=True`` counts only what get_queue() would return, i.e. what
    the user can actually SEE in the queue.

    That distinction is not cosmetic. Removing a finished, failed or cancelled
    entry does not delete the row -- it sets ``hidden = 1``, so the download
    still counts towards the statistics (see remove_from_queue() and
    clear_completed()). Every counter here ignored that flag, so a badge built
    on ``by_status["failed"]`` kept counting downloads the user had already
    cleared away, and no amount of clearing ever brought it down while the
    list next to it was empty.

    The default stays ``False`` on purpose: /api/stats and /api/v1/status want
    the historical numbers, which is the entire reason the rows are kept.
    """
    where = " WHERE hidden = 0" if visible_only else ""
    conn = get_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue" + where
        ).fetchone()["cnt"]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM download_queue" + where
            + " GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["cnt"]
        running = conn.execute(
            "SELECT id, title, current_episode, total_episodes, language, provider, source "
            "FROM download_queue WHERE status = 'running'"
            + (" AND hidden = 0" if visible_only else "") + " LIMIT 1"
        ).fetchone()
        if running:
            r = dict(running)
            cur = r.get("current_episode") or 0
            tot = r.get("total_episodes") or 0
            r["progress_percent"] = round(cur / tot * 100) if tot > 0 else 0
        else:
            r = None
        return {
            "total": total,
            "by_status": by_status,
            "currently_running": r,
        }
    finally:
        conn.close()


def get_general_stats():
    conn = get_db()
    try:
        total_downloads = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial', 'failed')"
        ).fetchone()["cnt"]
        completed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'completed'"
        ).fetchone()["cnt"]
        failed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'failed'"
        ).fetchone()["cnt"]
        total_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial')"
        ).fetchone()["s"]
        last_24h = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') "
            "AND completed_at >= datetime('now', '-1 day')"
        ).fetchone()["cnt"]
        # Average duration (completed items with both timestamps)
        avg_dur = conn.execute(
            "SELECT AVG("
            "  (julianday(completed_at) - julianday(created_at)) * 86400"
            ") AS avg_s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND completed_at IS NOT NULL"
        ).fetchone()["avg_s"]
        # Most downloaded titles
        top_titles = conn.execute(
            "SELECT title, COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') GROUP BY title "
            "ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        # Episodes per language
        by_language = conn.execute(
            "SELECT language, COUNT(*) AS cnt, "
            "COALESCE(SUM(total_episodes), 0) AS eps "
            "FROM download_queue WHERE status IN ('completed', 'partial') "
            "GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        # Source breakdown (heuristic by URL)
        anime_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%aniworld.to%'"
        ).fetchone()["cnt"]
        anime_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%aniworld.to%'"
        ).fetchone()["s"]
        series_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%s.to%'"
        ).fetchone()["cnt"]
        series_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%s.to%'"
        ).fetchone()["s"]
        movie_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%filmpalast.to%'"
        ).fetchone()["cnt"]
        movie_episodes = conn.execute(
            "SELECT COALESCE(SUM(total_episodes), 0) AS s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND series_url LIKE '%filmpalast.to%'"
        ).fetchone()["s"]
        # Weekday activity (0=Sunday, 1=Monday, ...)
        weekday_rows = conn.execute(
            "SELECT strftime('%w', completed_at) as weekday, COUNT(*) as cnt "
            "FROM download_queue WHERE status IN ('completed', 'partial') AND completed_at IS NOT NULL "
            "GROUP BY weekday ORDER BY weekday"
        ).fetchall()
        weekday_activity = {r["weekday"]: r["cnt"] for r in weekday_rows}

        # Speed stats
        avg_speed = conn.execute(
            "SELECT AVG(average_speed_mbps) as avg_s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND average_speed_mbps IS NOT NULL"
        ).fetchone()["avg_s"]

        total_size = conn.execute(
            "SELECT SUM(total_size_mb) as s FROM download_queue "
            "WHERE status IN ('completed', 'partial') AND total_size_mb IS NOT NULL"
        ).fetchone()["s"]

        # Last 20 speeds for details modal
        last_speeds = conn.execute(
            "SELECT title, average_speed_mbps, total_size_mb, completed_at "
            "FROM download_queue WHERE status IN ('completed', 'partial') AND average_speed_mbps IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 20"
        ).fetchall()

        partial = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE status = 'partial'"
        ).fetchone()["cnt"]

        return {
            "total_downloads": total_downloads,
            "completed": completed,
            "failed": failed,
            "partial": partial,
            "total_episodes": total_episodes,
            "last_24h_completed": last_24h,
            "average_duration_seconds": round(avg_dur, 1) if avg_dur else None,
            "weekday_activity": weekday_activity,
            "average_speed_mbps": round(avg_speed, 3) if avg_speed else None,
            "total_size_mb": round(total_size, 1) if total_size else 0.0,
            "last_speeds": [
                {
                    "title": r["title"],
                    "speed": round(r["average_speed_mbps"], 3),
                    "size": round(r["total_size_mb"], 2),
                    "date": r["completed_at"]
                } for r in last_speeds
            ],
            "top_titles": [
                {"title": r["title"], "count": r["cnt"]} for r in top_titles
            ],
            "by_language": [
                {"language": r["language"], "downloads": r["cnt"], "episodes": r["eps"]}
                for r in by_language
            ],
            "anime_downloads": anime_count,
            "anime_episodes": anime_episodes,
            "series_downloads": series_count,
            "series_episodes": series_episodes,
            "movie_downloads": movie_count,
            "movie_files": movie_episodes,
        }
    finally:
        conn.close()


# --- Trend aggregates (Statistics page charts) ---------------------------

# Upper bound for the ?days= window of get_stats_trends(). Keeps a crafted
# query (e.g. days=9999999) from making SQLite walk an unbounded range and
# from returning a multi-megabyte JSON payload.
STATS_TRENDS_MAX_DAYS = 365
STATS_TRENDS_DEFAULT_DAYS = 30


def _stats_clamp_days(days):
    """Coerce a user-supplied ?days= value into 1..STATS_TRENDS_MAX_DAYS."""
    try:
        d = int(days)
    except (TypeError, ValueError):
        return STATS_TRENDS_DEFAULT_DAYS
    return max(1, min(STATS_TRENDS_MAX_DAYS, d))


def get_stats_trends(days=STATS_TRENDS_DEFAULT_DAYS):
    """Aggregate the download history into chart-ready series.

    Feeds the Statistics page charts (static/stats.js). Everything is read
    from download_history, which stores one row per finished episode with
    size/speed/duration -- unlike download_queue, which only keeps one row
    per queue entry and is overwritten as items are retried.

    All grouping happens in SQLite (indexed on finished_at) rather than in
    Python, so the payload size, not the history size, drives the cost.
    Days with no activity are filled with zeros so the resulting series is
    always exactly `days` long and the chart x-axis is continuous.

    Returns a dict with:
      days, from_date, to_date            -- the resolved window
      daily[]                             -- per-day downloads/episodes/size/failed/avg speed
      hourly[24], weekday[7]              -- activity distribution inside the window
      heatmap                             -- weekday x hour matrix (7 rows of 24)
      by_provider[], by_source[], by_language[]
      totals                              -- window totals incl. success rate
      speed_series[]                      -- most recent per-download speeds (oldest first)
    """
    days = _stats_clamp_days(days)
    since = f"-{days - 1} days"
    conn = get_db()
    try:
        # Guard: the table may not exist yet on a very old DB that has not run
        # init_download_history_db(). Treat that as "no data" rather than 500.
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='download_history'"
        ).fetchone()
        if not exists:
            return {
                "days": days, "daily": [], "hourly": [0] * 24, "weekday": [0] * 7,
                "heatmap": [[0] * 24 for _ in range(7)], "by_provider": [],
                "by_source": [], "by_language": [], "speed_series": [],
                "totals": {
                    "downloads": 0, "episodes": 0, "failed": 0, "size_mb": 0.0,
                    "avg_speed_mbps": None, "success_rate": 0, "hours_spent": 0.0,
                },
                "available": False,
            }

        # localtime() so "per day" matches the user's calendar, not UTC.
        rows = conn.execute(
            "SELECT date(finished_at, 'localtime') AS d, "
            "       COUNT(*) AS cnt, "
            "       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS ok_cnt, "
            "       SUM(CASE WHEN status <> 'completed' THEN 1 ELSE 0 END) AS bad_cnt, "
            "       COALESCE(SUM(size_mb), 0) AS size_mb, "
            "       AVG(avg_speed_mbps) AS spd, "
            "       COALESCE(SUM(duration_sec), 0) AS dur "
            "FROM download_history "
            "WHERE finished_at IS NOT NULL "
            "  AND date(finished_at, 'localtime') >= date('now', 'localtime', ?) "
            "GROUP BY d ORDER BY d",
            (since,),
        ).fetchall()
        by_day = {r["d"]: r for r in rows}

        today = _dt.datetime.now().date()
        daily = []
        for i in range(days - 1, -1, -1):
            d = today - _dt.timedelta(days=i)
            key = d.isoformat()
            r = by_day.get(key)
            daily.append({
                "date": key,
                "downloads": r["cnt"] if r else 0,
                "completed": (r["ok_cnt"] or 0) if r else 0,
                "failed": (r["bad_cnt"] or 0) if r else 0,
                "size_mb": round(r["size_mb"] or 0.0, 2) if r else 0.0,
                "avg_speed_mbps": round(r["spd"], 3) if r and r["spd"] else None,
                "duration_sec": round(r["dur"] or 0.0, 1) if r else 0.0,
            })

        # Hour-of-day and weekday distribution (SQLite %w: 0 = Sunday).
        hourly = [0] * 24
        weekday = [0] * 7
        heatmap = [[0] * 24 for _ in range(7)]
        for r in conn.execute(
            "SELECT CAST(strftime('%H', finished_at, 'localtime') AS INTEGER) AS h, "
            "       CAST(strftime('%w', finished_at, 'localtime') AS INTEGER) AS w, "
            "       COUNT(*) AS cnt "
            "FROM download_history "
            "WHERE finished_at IS NOT NULL "
            "  AND date(finished_at, 'localtime') >= date('now', 'localtime', ?) "
            "GROUP BY h, w",
            (since,),
        ).fetchall():
            h, w, cnt = r["h"], r["w"], r["cnt"]
            if h is None or w is None:
                continue
            hourly[h] += cnt
            weekday[w] += cnt
            heatmap[w][h] += cnt

        def _breakdown(column):
            return [
                {
                    "name": r["k"] or "",
                    "downloads": r["cnt"],
                    "size_mb": round(r["size_mb"] or 0.0, 2),
                    "avg_speed_mbps": round(r["spd"], 3) if r["spd"] else None,
                }
                for r in conn.execute(
                    f"SELECT COALESCE({column}, '') AS k, COUNT(*) AS cnt, "
                    "       COALESCE(SUM(size_mb), 0) AS size_mb, AVG(avg_speed_mbps) AS spd "
                    "FROM download_history "
                    "WHERE finished_at IS NOT NULL "
                    "  AND date(finished_at, 'localtime') >= date('now', 'localtime', ?) "
                    f"GROUP BY k ORDER BY cnt DESC LIMIT 12",
                    (since,),
                ).fetchall()
            ]

        # Column names are literals from this function only -- never user input.
        by_provider = _breakdown("provider")
        by_source = _breakdown("source")
        by_language = _breakdown("language")

        tot = conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS ok_cnt, "
            "       SUM(CASE WHEN status <> 'completed' THEN 1 ELSE 0 END) AS bad_cnt, "
            "       COALESCE(SUM(size_mb), 0) AS size_mb, "
            "       AVG(avg_speed_mbps) AS spd, "
            "       COALESCE(SUM(duration_sec), 0) AS dur, "
            "       MAX(size_mb) AS max_size, MAX(avg_speed_mbps) AS max_spd "
            "FROM download_history "
            "WHERE finished_at IS NOT NULL "
            "  AND date(finished_at, 'localtime') >= date('now', 'localtime', ?)",
            (since,),
        ).fetchone()
        cnt = tot["cnt"] or 0
        ok = tot["ok_cnt"] or 0

        top_titles = [
            {"title": r["title"], "downloads": r["cnt"], "size_mb": round(r["size_mb"] or 0.0, 2)}
            for r in conn.execute(
                "SELECT title, COUNT(*) AS cnt, COALESCE(SUM(size_mb), 0) AS size_mb "
                "FROM download_history "
                "WHERE finished_at IS NOT NULL "
                "  AND date(finished_at, 'localtime') >= date('now', 'localtime', ?) "
                "GROUP BY title ORDER BY cnt DESC, size_mb DESC LIMIT 10",
                (since,),
            ).fetchall()
        ]

        # Oldest-first so the sparkline reads left-to-right in time order.
        speed_series = [
            {
                "title": r["title"],
                "speed": round(r["avg_speed_mbps"], 3),
                "size_mb": round(r["size_mb"] or 0.0, 2),
                "finished_at": r["finished_at"],
            }
            for r in reversed(conn.execute(
                "SELECT title, avg_speed_mbps, size_mb, finished_at FROM download_history "
                "WHERE avg_speed_mbps IS NOT NULL AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 60"
            ).fetchall())
        ]

        return {
            "days": days,
            "from_date": daily[0]["date"] if daily else None,
            "to_date": daily[-1]["date"] if daily else None,
            "daily": daily,
            "hourly": hourly,
            "weekday": weekday,
            "heatmap": heatmap,
            "by_provider": by_provider,
            "by_source": by_source,
            "by_language": by_language,
            "top_titles": top_titles,
            "speed_series": speed_series,
            "totals": {
                "downloads": cnt,
                "completed": ok,
                "failed": tot["bad_cnt"] or 0,
                "size_mb": round(tot["size_mb"] or 0.0, 2),
                "avg_speed_mbps": round(tot["spd"], 3) if tot["spd"] else None,
                "max_speed_mbps": round(tot["max_spd"], 3) if tot["max_spd"] else None,
                "largest_mb": round(tot["max_size"] or 0.0, 2),
                "hours_spent": round((tot["dur"] or 0.0) / 3600.0, 2),
                "success_rate": round(ok / cnt * 100) if cnt else 0,
            },
            "available": True,
        }
    finally:
        conn.close()
