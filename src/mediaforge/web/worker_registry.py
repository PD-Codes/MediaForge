"""One place that knows what every background worker is doing.

Before this, the answer to "is the encoder stuck?" was spread across six
tables and three log prefixes: the queue worker's state lived in
``download_queue``, the scanner's in ``library_cache`` timestamps, auto-sync's
in ``autosync_jobs.last_run``, and the rest only in the log. Each of those is
the right place for that worker's *data* -- none of them answers the operator
question, which is always the same shape: did it run, is it running now, when
does it run next, and what did it last fail with.

This module is that answer. Workers call :func:`beat` as they work; the
Operations view reads :func:`snapshot`. Nothing here is authoritative for the
work itself, only for its liveness, which is why a lost heartbeat row is
harmless -- it degrades to "unknown", never to "job lost".

It is also the seam for moving workers out of the web process. A worker that
reports through a table instead of an in-process variable does not care which
process it lives in, so :mod:`mediaforge.web.worker_host` can run the exact
same worker functions in a separate process without any of them changing.
"""

from __future__ import annotations

import datetime as _dt
import os
import socket
import threading

from ..logger import get_logger

logger = get_logger(__name__)

# Every worker the app can run, with the i18n key for its display name and
# whether it is a candidate for the out-of-process worker host. Anything not
# listed here still works -- beat() accepts any name -- but only listed
# workers get a row in the Operations view when they have never run.
WORKERS: dict[str, dict] = {
    "queue":          {"label": "worker_queue",     "external": True},
    "encoding":       {"label": "worker_encoding",  "external": True},
    "upscale":        {"label": "worker_upscale",   "external": True},
    "autosync":       {"label": "worker_autosync",  "external": True},
    "library_scan":   {"label": "worker_libscan",   "external": True},
    "uptime":         {"label": "worker_uptime",    "external": False},
    "mediascan":      {"label": "worker_mediascan", "external": True},
    "tmdb_keywords":  {"label": "worker_keywords",  "external": True},
    "cache_evict":    {"label": "worker_cacheevict", "external": False},
    "devinfos":       {"label": "worker_devinfos",  "external": False},
    "audit_prune":    {"label": "worker_auditprune", "external": False},
}

# A worker is "stale" once this many seconds have passed without a heartbeat.
# Generous on purpose: several of these sleep for an hour between rounds, and
# they beat on wake, not on a timer, so a short threshold would flag healthy
# idle workers as dead.
STALE_AFTER = 900

_local = threading.local()

# Workers whose heartbeat has already failed once. See beat()'s except clause:
# the first failure is a warning, the rest are debug.
_warned: set[str] = set()


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _mode() -> str:
    return os.environ.get("MEDIAFORGE_WORKER_MODE", "inprocess")


def beat(worker: str, *, state: str = "idle", detail: str = "",
         last_run: str | None = None, next_run: str | None = None,
         error: str | None = None) -> None:
    """Record that ``worker`` is alive and what it is doing. Never raises.

    ``error`` is sticky: passing ``None`` leaves the previous error in place
    so a failure stays visible after the worker recovers and goes back to
    idle. Pass an empty string to clear it explicitly.
    """
    try:
        from .db import get_db
        conn = get_db()
        try:
            # last_error is NOT NULL, so the INSERT branch can never receive
            # NULL -- the sticky-error semantics live entirely in the UPDATE
            # branch, where :err IS NULL means "leave whatever is there".
            #
            # Writing the sentinel straight into the inserted row was the first
            # version of this and it was wrong in the quietest possible way:
            # every heartbeat failed with "NOT NULL constraint failed", beat()
            # swallowed it as a debug line (a heartbeat must never take a
            # worker down), and the Operations view simply stayed empty.
            conn.execute("""
                INSERT INTO worker_heartbeats
                    (worker, pid, host, mode, state, detail, last_beat, last_run,
                     next_run, last_error, error_at)
                VALUES (:worker, :pid, :host, :mode, :state, :detail, :now,
                        :last_run, :next_run, COALESCE(:err, ''),
                        CASE WHEN :err IS NOT NULL AND :err != '' THEN :now END)
                ON CONFLICT(worker) DO UPDATE SET
                    pid = :pid,
                    host = :host,
                    mode = :mode,
                    state = :state,
                    detail = :detail,
                    last_beat = :now,
                    last_run = COALESCE(:last_run, worker_heartbeats.last_run),
                    next_run = COALESCE(:next_run, worker_heartbeats.next_run),
                    last_error = COALESCE(:err, worker_heartbeats.last_error),
                    error_at = CASE WHEN :err IS NOT NULL AND :err != ''
                                    THEN :now ELSE worker_heartbeats.error_at END
            """, {
                "worker": worker,
                "pid": os.getpid(),
                "host": socket.gethostname()[:64],
                "mode": _mode(),
                "state": state,
                "detail": str(detail)[:300],
                "now": _now(),
                "last_run": last_run,
                "next_run": next_run,
                "err": None if error is None else str(error)[:500],
            })
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # A heartbeat is diagnostics. It must never take a worker down with it
        # -- but it must not be invisible either. The first version logged this
        # at DEBUG, so a statement that failed on *every single* heartbeat
        # produced an empty Operations view and not one line anybody saw.
        # Warn once per worker, then fall back to debug so a persistent problem
        # does not drown the log.
        if worker not in _warned:
            _warned.add(worker)
            logger.warning("[Workers] Heartbeat for %s failed: %s", worker, exc)
        else:
            logger.debug("[Workers] Heartbeat for %s failed: %s", worker, exc)


def fail(worker: str, error: str, detail: str = "") -> None:
    beat(worker, state="error", detail=detail, error=str(error), last_run=_now())


def done(worker: str, detail: str = "", next_run: str | None = None) -> None:
    beat(worker, state="idle", detail=detail, last_run=_now(),
         next_run=next_run, error="")


def working(worker: str, detail: str = "") -> None:
    beat(worker, state="running", detail=detail)


def snapshot() -> list[dict]:
    """Current state of every known worker, newest information first.

    Workers that have never reported appear with ``state = "unknown"`` rather
    than being omitted: "the encoder has never started" and "there is no
    encoder" look identical in a list that only shows what it found, and they
    are very different problems.
    """
    rows: dict[str, dict] = {}
    try:
        from .db import get_db
        conn = get_db()
        try:
            for row in conn.execute("SELECT * FROM worker_heartbeats").fetchall():
                rows[row["worker"]] = dict(row)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[Workers] Could not read heartbeats: %s", exc)

    out = []
    now = _dt.datetime.now()
    for name, meta in WORKERS.items():
        row = rows.pop(name, None)
        entry = {
            "worker": name,
            "label": meta["label"],
            "external_capable": meta["external"],
            "state": "unknown",
            "detail": "",
            "mode": _mode(),
            "pid": 0,
            "host": "",
            "last_beat": None,
            "last_run": None,
            "next_run": None,
            "last_error": "",
            "error_at": None,
            "stale": False,
            "age": None,
        }
        if row:
            entry.update(row)
            entry["label"] = meta["label"]
            entry["external_capable"] = meta["external"]
            try:
                age = (now - _dt.datetime.fromisoformat(row["last_beat"])).total_seconds()
                entry["age"] = int(age)
                entry["stale"] = age > STALE_AFTER and row["state"] == "running"
            except Exception:
                pass
        out.append(entry)

    # Anything that reported but is not in WORKERS -- a module's own worker,
    # most likely. Show it rather than hide it.
    for name, row in rows.items():
        row.update({"label": name, "external_capable": False, "stale": False})
        out.append(row)

    return out


def health() -> dict:
    """Compact roll-up used by /healthz and the settings badge."""
    workers = snapshot()
    errors = [w for w in workers if w["state"] == "error" or w["last_error"]]
    stale = [w for w in workers if w["stale"]]
    return {
        "workers": len(workers),
        "running": sum(1 for w in workers if w["state"] == "running"),
        "errors": [w["worker"] for w in errors],
        "stale": [w["worker"] for w in stale],
        "ok": not errors and not stale,
    }
