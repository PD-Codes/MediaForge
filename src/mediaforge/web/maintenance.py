"""Quiet hours: time-of-day limits on what the workers may do.

Naming note: the feature is called "quiet hours" everywhere a user can see it.
The module, the ``maintenance_windows`` table, the ``/api/ops/maintenance``
routes and the ``maintenance_window_*`` audit actions keep the old name on
purpose -- renaming them buys nothing a user would notice and costs a schema
migration, a breaking API change and a gap in the audit trail, which is a bad
trade for a label. "Maintenance" was wrong as a *name* because nothing here
maintains anything; it throttles.

The motivating case is the most common MediaForge deployment there is -- the
app running on the same machine its owner works on. Four parallel downloads
plus an ffmpeg encode is fine at 3 a.m. and unbearable at 10 a.m., and the only
control available today is a global "max parallel downloads" that is either too
low at night or too high during the day.

A quiet period says: on these weekdays, between these two clock times, allow at
most N downloads and optionally forbid encoding, upscaling and library scans
entirely. Periods are *restrictions*; outside any period the normal settings
apply unchanged, so an install with no quiet hours behaves exactly as before.

Overlapping periods resolve to the strictest combination rather than to
"whichever was found first". Two periods that each forbid encoding must not
combine into one that permits it, and an ordering-dependent answer here would
be the kind of bug nobody reproduces.

Times are stored as minutes since midnight, in the server's local timezone.
A period whose end is before its start wraps over midnight -- "22:00 to 06:00"
is one period, not two, because that is how a person describes a night.
"""

from __future__ import annotations

import datetime as _dt
import json

from ..logger import get_logger

logger = get_logger(__name__)

# Cache the parsed window list briefly. is_allowed() is called on every claim
# attempt in three workers; re-reading the table each time would put a SQLite
# read on the hot path of the download loop for data that changes maybe twice
# a year.
_cache: dict = {"at": 0.0, "rows": None}
_CACHE_TTL = 30.0


def _now_parts(when: _dt.datetime | None = None) -> tuple[int, int]:
    when = when or _dt.datetime.now()
    return when.weekday(), when.hour * 60 + when.minute


def _covers(row: dict, weekday: int, minute: int) -> bool:
    if not row.get("enabled"):
        return False
    if not (int(row.get("days_mask", 127)) >> weekday) & 1:
        return False
    start, end = int(row["start_minute"]), int(row["end_minute"])
    if start == end:
        return False          # zero-length window covers nothing
    if start < end:
        return start <= minute < end
    # Wraps midnight.
    return minute >= start or minute < end


def list_windows() -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM maintenance_windows ORDER BY start_minute, name").fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _rows() -> list[dict]:
    import time as _t
    now = _t.time()
    if _cache["rows"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["rows"]
    rows = list_windows()
    _cache.update({"at": now, "rows": rows})
    return rows


def invalidate_cache() -> None:
    _cache.update({"at": 0.0, "rows": None})


def active_windows(when: _dt.datetime | None = None) -> list[dict]:
    weekday, minute = _now_parts(when)
    return [r for r in _rows() if _covers(r, weekday, minute)]


def current_limits(when: _dt.datetime | None = None) -> dict:
    """The strictest combination of every window active right now.

    ``max_downloads`` is ``None`` when no window applies, meaning "use the
    configured setting" -- not "unlimited". Callers must treat None as
    "no override", or a machine with no windows would suddenly ignore the
    parallel-download limit entirely.
    """
    active = active_windows(when)
    if not active:
        return {"active": False, "windows": [], "max_downloads": None,
                "allow_encoding": True, "allow_upscale": True, "allow_scan": True}
    return {
        "active": True,
        "windows": [w["name"] for w in active],
        "max_downloads": min(int(w["max_downloads"]) for w in active),
        "allow_encoding": all(bool(w["allow_encoding"]) for w in active),
        "allow_upscale": all(bool(w["allow_upscale"]) for w in active),
        "allow_scan": all(bool(w["allow_scan"]) for w in active),
    }


def is_allowed(activity: str, when: _dt.datetime | None = None) -> bool:
    """True when ``activity`` ("encoding" | "upscale" | "scan") may run now."""
    limits = current_limits(when)
    if not limits["active"]:
        return True
    return bool(limits.get("allow_%s" % activity, True))


def max_downloads(configured: int, when: _dt.datetime | None = None) -> int:
    """Clamp the configured parallel-download limit to the active window."""
    limits = current_limits(when)
    override = limits.get("max_downloads")
    if override is None:
        return configured
    return max(0, min(int(configured), int(override)))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _clean(payload: dict) -> tuple[dict | None, str | None]:
    try:
        name = str(payload.get("name") or "").strip()
        if not name:
            return None, "name_required"
        start = int(payload.get("start_minute", 0))
        end = int(payload.get("end_minute", 0))
        if not (0 <= start <= 1440 and 0 <= end <= 1440):
            return None, "invalid_time"
        mask = int(payload.get("days_mask", 127))
        if not (0 <= mask <= 127):
            return None, "invalid_days"
        return {
            "name": name[:80],
            "enabled": 1 if payload.get("enabled", True) else 0,
            "days_mask": mask,
            "start_minute": start,
            "end_minute": end,
            # 0 is meaningful: "pause downloads entirely during this window".
            "max_downloads": max(0, min(int(payload.get("max_downloads", 1)), 32)),
            "allow_encoding": 1 if payload.get("allow_encoding", False) else 0,
            "allow_upscale": 1 if payload.get("allow_upscale", False) else 0,
            "allow_scan": 1 if payload.get("allow_scan", True) else 0,
        }, None
    except (TypeError, ValueError):
        return None, "invalid_payload"


def create_window(payload: dict) -> tuple[int | None, str | None]:
    data, err = _clean(payload)
    if err:
        return None, err
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO maintenance_windows "
            "(name, enabled, days_mask, start_minute, end_minute, max_downloads,"
            " allow_encoding, allow_upscale, allow_scan) VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(data[k] for k in ("name", "enabled", "days_mask", "start_minute",
                                    "end_minute", "max_downloads", "allow_encoding",
                                    "allow_upscale", "allow_scan")))
        conn.commit()
        invalidate_cache()
        return cur.lastrowid, None
    finally:
        conn.close()


def update_window(window_id: int, payload: dict) -> tuple[bool, str | None]:
    data, err = _clean(payload)
    if err:
        return False, err
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE maintenance_windows SET name=?, enabled=?, days_mask=?,"
            " start_minute=?, end_minute=?, max_downloads=?, allow_encoding=?,"
            " allow_upscale=?, allow_scan=? WHERE id=?",
            tuple(data[k] for k in ("name", "enabled", "days_mask", "start_minute",
                                    "end_minute", "max_downloads", "allow_encoding",
                                    "allow_upscale", "allow_scan")) + (window_id,))
        conn.commit()
        invalidate_cache()
        return (cur.rowcount or 0) > 0, None
    finally:
        conn.close()


def delete_window(window_id: int) -> bool:
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
        conn.commit()
        invalidate_cache()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def export_windows() -> str:
    return json.dumps(list_windows(), indent=2)
