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

# The three states the Operations view knows. Everything a worker reports is
# mapped onto one of them before it leaves this module, so the UI never has to
# know about intermediate vocabulary.
#
#   idle    -- registered, not doing anything right now ("Inaktiv")
#   working -- actively processing ("Arbeitet")
#   error   -- last attempt failed ("Fehler")
#
# There is deliberately no "unknown" any more. It was never information an
# operator could act on: it only ever meant "this worker has no heartbeat row",
# which for five of the eleven workers below was permanent, because they simply
# never called beat(). They do now (see the call sites listed per worker in
# WORKERS), so a worker with no row is one that has not started yet -- which is
# what idle already says.
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_ERROR = "error"

# Legacy state names that may still sit in worker_heartbeats from before the
# rename, plus the ones a module's own worker might use. Mapped on read so an
# upgrade does not need a data migration.
_STATE_ALIASES = {
    "running": STATE_WORKING,
    "busy": STATE_WORKING,
    "ok": STATE_IDLE,
    "unknown": STATE_IDLE,
    "": STATE_IDLE,
}

# What kind of schedule a worker is on -- this decides which fields the
# Operations view shows for it, so the UI has no per-worker special cases.
#
#   continuous -- a loop that reacts to work arriving. "Next run" is
#                 meaningless for these; what an operator wants is how long
#                 until the stall watchdog steps in.
#   scheduled  -- runs on an interval. Last run / next run are the useful
#                 facts; a stall countdown is not.
KIND_CONTINUOUS = "continuous"
KIND_SCHEDULED = "scheduled"

# Field ids the UI can render. Kept as data rather than as template branches so
# adding a worker (or a field to one) is a one-line change here.
F_STATUS = "status"
F_STALL = "stall"            # countdown to the watchdog restart
F_LAST_RUN = "last_run"
F_NEXT_RUN = "next_run"
F_FOUND = "found"            # number of files a scan turned up
F_ONLINE = "online"          # uptime monitor: online/offline split
F_ENTRIES = "entries"        # number of stored records
F_NEXT_UPDATE = "next_update"

# Every worker the app can run:
#   label     i18n key for its display name
#   external  candidate for the out-of-process worker host
#   kind      KIND_CONTINUOUS / KIND_SCHEDULED (see above)
#   fields    what the Operations view shows, in order
#   stall     seconds without a heartbeat before the watchdog restarts it,
#             or None for "not watched". Only meaningful for continuous
#             workers -- a scheduled worker that sleeps for an hour is not
#             stalled, it is waiting.
#   link      relative URL of the page that configures this worker, or absent.
#             The card is a dead end otherwise: "Auto-Sync: next run never"
#             is only actionable if you can get to the schedules from there.
#             Relative and server-side on purpose -- the client must not build
#             URLs, and an absolute one here would be an open-redirect waiting
#             to happen once modules can register workers.
#
# Anything not listed here still works -- beat() accepts any name -- but only
# listed workers get a row in the Operations view when they have never run.
WORKERS: dict[str, dict] = {
    "queue": {
        "label": "worker_queue", "external": True, "kind": KIND_CONTINUOUS,
        "fields": (F_STATUS, F_STALL), "stall": 900,
    },
    "encoding": {
        "label": "worker_encoding", "external": True, "kind": KIND_CONTINUOUS,
        "fields": (F_STATUS, F_STALL), "stall": 900,
        "link": "/encoding",
    },
    "upscale": {
        "label": "worker_upscale", "external": True, "kind": KIND_CONTINUOUS,
        # Upscaling a single episode can legitimately run for hours, so the
        # watchdog window is wide. What keeps it honest is that the worker
        # beats on a ticker while it works (see upscale_worker.py), not only
        # at job start/end -- the old code beat only at the edges, which is
        # exactly why a healthy 2h job showed up as overdue.
        "fields": (F_STATUS, F_STALL), "stall": 1800,
    },
    "autosync": {
        "label": "worker_autosync", "external": True, "kind": KIND_SCHEDULED,
        # No next_run on purpose. There is no such thing as "the" next
        # auto-sync run: every job carries its own interval, its own weekly
        # slot and its own retry backoff, and the earliest of forty-two of
        # them is a number that answers a question nobody asked. The card
        # links to /autosync, which shows the schedule per job -- that is the
        # honest answer.
        "fields": (F_LAST_RUN, F_STATUS), "stall": None,
        "link": "/autosync",
    },
    "library_scan": {
        "label": "worker_libscan", "external": True, "kind": KIND_SCHEDULED,
        "fields": (F_LAST_RUN, F_NEXT_RUN, F_STATUS, F_FOUND), "stall": None,
        "link": "/library",
    },
    "uptime": {
        "label": "worker_uptime", "external": False, "kind": KIND_CONTINUOUS,
        # next_run is shown even though this is a continuous worker: it sleeps
        # between probe rounds, so "idle" plus "5 up / 0 down" reads like a
        # contradiction unless the card also says when it looks again.
        "fields": (F_STATUS, F_ONLINE, F_NEXT_RUN), "stall": None,
        "link": "/uptime",
    },
    "mediascan": {
        "label": "worker_mediascan", "external": True, "kind": KIND_SCHEDULED,
        "fields": (F_LAST_RUN, F_NEXT_RUN, F_STATUS), "stall": None,
        "link": "/integrations",
    },
    "tmdb_keywords": {
        "label": "worker_keywords", "external": True, "kind": KIND_SCHEDULED,
        "fields": (F_LAST_RUN, F_NEXT_RUN, F_STATUS), "stall": None,
    },
    "cache_evict": {
        "label": "worker_cacheevict", "external": False, "kind": KIND_SCHEDULED,
        "fields": (F_LAST_RUN, F_NEXT_RUN, F_STATUS), "stall": None,
    },
    "devinfos": {
        "label": "worker_devinfos", "external": False, "kind": KIND_SCHEDULED,
        "fields": (F_NEXT_UPDATE, F_ENTRIES, F_STATUS), "stall": None,
        "link": "/devinfos",
    },
    "audit_prune": {
        "label": "worker_auditprune", "external": False, "kind": KIND_SCHEDULED,
        "fields": (F_LAST_RUN, F_NEXT_RUN, F_ENTRIES, F_STATUS), "stall": None,
    },
}

# Fallback stall window for a continuous worker without an explicit one, and
# for a module's own worker that reports through beat() without being listed
# in WORKERS.
DEFAULT_STALL_AFTER = 900

# Kept as an alias so anything still importing the old name keeps working.
STALE_AFTER = DEFAULT_STALL_AFTER

_local = threading.local()

# Workers whose heartbeat has already failed once. See beat()'s except clause:
# the first failure is a warning, the rest are debug.
_warned: set[str] = set()


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# Topic the Operations view listens on. One topic for all workers rather
# than one per worker: a browser showing the page wants every card, and one
# EventSource is cheaper than eleven.
EVENT_TOPIC = "workers"


# Last published (state, detail, error) per worker. See _publish(): heartbeats
# are frequent and mostly say the same thing, and each published message costs
# every connected Operations view a full snapshot rebuild.
_last_published: dict[str, tuple] = {}
_publish_lock = threading.Lock()


def _publish(worker: str, state: str, detail: str, error) -> None:
    """Tell connected Operations views that *worker* changed. Never raises.

    Only fires when something an operator can see actually changed. This is not
    a micro-optimization: an idle worker beats every 3-4 seconds (the loops
    poll an empty queue and report "still here"), so publishing every heartbeat
    meant roughly one message per second with nothing to show for it -- and
    each one made every connected browser's stream rebuild a full snapshot.
    That would have been strictly worse than the 10-second poll it replaced.

    Sends a hint, not the state: the client re-reads snapshot() through the
    stream's own payload builder, so there is one authoritative shape for the
    data and a dropped message costs nothing but latency.
    """
    try:
        signature = (state, str(detail or "")[:300], str(error or "")[:200])
        with _publish_lock:
            if _last_published.get(worker) == signature:
                return
            _last_published[worker] = signature

        from .events import publish

        publish(EVENT_TOPIC, {"worker": worker})
    except Exception:
        logger.debug("[Workers] Could not publish change for %s", worker, exc_info=True)


def _normalize_state(state) -> str:
    """Map whatever a caller passed onto one of the three UI states."""
    s = str(state or "").strip().lower()
    s = _STATE_ALIASES.get(s, s)
    return s if s in (STATE_IDLE, STATE_WORKING, STATE_ERROR) else STATE_IDLE


def beat(worker: str, *, state: str = STATE_IDLE, detail: str = "",
         last_run: str | None = None, next_run: str | None = None,
         error: str | None = None, extra: dict | None = None) -> None:
    """Record that ``worker`` is alive and what it is doing. Never raises.

    ``error`` is sticky: passing ``None`` leaves the previous error in place
    so a failure stays visible after the worker recovers and goes back to
    idle. Pass an empty string to clear it explicitly.

    ``extra`` is the same shape of deal for the per-worker facts the Operations
    view shows (files found, sites online, rows stored -- see the ``fields``
    entry in WORKERS): keys passed are merged over what is stored, keys not
    passed keep their previous value. A worker that reports its file count once
    per scan therefore does not blank it out on every intermediate heartbeat.
    """
    try:
        import json as _json
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
            # Merge, don't replace: read the stored extras first so a heartbeat
            # that only reports "still working" cannot wipe the file count the
            # last completed scan wrote. Passing extra=None skips the read
            # entirely, which is the common case (every ticker beat).
            merged_extra = None
            if extra:
                stored = {}
                try:
                    row = conn.execute(
                        "SELECT extra FROM worker_heartbeats WHERE worker = ?",
                        (worker,)).fetchone()
                    if row and row["extra"]:
                        stored = _json.loads(row["extra"]) or {}
                        if not isinstance(stored, dict):
                            stored = {}
                except Exception:
                    stored = {}
                stored.update({str(k): v for k, v in extra.items()})
                merged_extra = _json.dumps(stored)[:2000]

            conn.execute("""
                INSERT INTO worker_heartbeats
                    (worker, pid, host, state, detail, last_beat, last_run,
                     next_run, last_error, error_at, extra)
                VALUES (:worker, :pid, :host, :state, :detail, :now,
                        :last_run, :next_run, COALESCE(:err, ''),
                        CASE WHEN :err IS NOT NULL AND :err != '' THEN :now END,
                        COALESCE(:extra, '{}'))
                ON CONFLICT(worker) DO UPDATE SET
                    pid = :pid,
                    host = :host,
                    state = :state,
                    detail = :detail,
                    last_beat = :now,
                    last_run = COALESCE(:last_run, worker_heartbeats.last_run),
                    next_run = COALESCE(:next_run, worker_heartbeats.next_run),
                    last_error = COALESCE(:err, worker_heartbeats.last_error),
                    error_at = CASE WHEN :err IS NOT NULL AND :err != ''
                                    THEN :now ELSE worker_heartbeats.error_at END,
                    extra = COALESCE(:extra, worker_heartbeats.extra)
            """, {
                "worker": worker,
                "pid": os.getpid(),
                "host": socket.gethostname()[:64],
                "state": _normalize_state(state),
                "detail": str(detail)[:300],
                "now": _now(),
                "last_run": last_run,
                "next_run": next_run,
                "err": None if error is None else str(error)[:500],
                "extra": merged_extra,
            })
            conn.commit()
        finally:
            conn.close()
        # Push the change to any connected Operations view. Best-effort,
        # non-blocking, and deduplicated -- see _publish(). A browser that is
        # not listening costs nothing, and a failing publish must not break a
        # heartbeat.
        #
        # `extra` intentionally does not take part in the change signature: a
        # counter ticking up is not something an operator is watching in real
        # time, and it arrives with the next state change anyway.
        _publish(worker, _normalize_state(state), detail, error)
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


def fail(worker: str, error: str, detail: str = "", extra: dict | None = None) -> None:
    beat(worker, state=STATE_ERROR, detail=detail, error=str(error),
         last_run=_now(), extra=extra)


def done(worker: str, detail: str = "", next_run: str | None = None,
         extra: dict | None = None) -> None:
    beat(worker, state=STATE_IDLE, detail=detail, last_run=_now(),
         next_run=next_run, error="", extra=extra)


def working(worker: str, detail: str = "", extra: dict | None = None) -> None:
    beat(worker, state=STATE_WORKING, detail=detail, extra=extra)


def idle(worker: str, detail: str = "", next_run: str | None = None,
         extra: dict | None = None) -> None:
    """Report "alive, nothing to do" without claiming a run just finished.

    The difference to done(): done() stamps last_run, because it means "that
    round is over". A continuous worker looping on an empty queue has not
    finished a run -- it never started one -- so it uses this instead. Without
    it, an idle queue worker would keep advancing "last run" every few seconds.
    """
    beat(worker, state=STATE_IDLE, detail=detail, next_run=next_run, extra=extra)


def stall_after(worker: str) -> int | None:
    """Seconds without a heartbeat before *worker* counts as stalled, or None
    if it is not watched (every scheduled worker -- sleeping is not stalling)."""
    meta = WORKERS.get(worker)
    if meta is None:
        return DEFAULT_STALL_AFTER
    if meta["kind"] != KIND_CONTINUOUS:
        return None
    return meta.get("stall") or DEFAULT_STALL_AFTER


def snapshot() -> list[dict]:
    """Current state of every known worker.

    Every entry carries:
      state            idle | working | error (never "unknown" -- see the
                       STATE_* constants for why that state is gone)
      fields           which of the values below the Operations view shows
      stall_after      watchdog window in seconds, or None
      stall_deadline   epoch seconds at which the watchdog would step in, or
                       None. The UI counts down to this rather than being fed
                       a ticking number, so the countdown stays correct between
                       updates and needs no polling of its own.
      extra            the per-worker facts (found / online / offline /
                       entries / next_update), already decoded

    A worker with no heartbeat row has not started yet and reports idle. It is
    still listed: "there is no encoder" and "the encoder has not started" are
    different problems, and omitting the row hides both.
    """
    import json as _json

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

    def _decode_extra(raw):
        try:
            value = _json.loads(raw or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    out = []
    now = _dt.datetime.now()
    for name, meta in WORKERS.items():
        row = rows.pop(name, None)
        entry = {
            "worker": name,
            "label": meta["label"],
            "external_capable": meta["external"],
            "kind": meta["kind"],
            "fields": list(meta["fields"]),
            "link": meta.get("link") or "",
            "stall_after": stall_after(name),
            "stall_deadline": None,
            "state": STATE_IDLE,
            "detail": "",
            "pid": 0,
            "host": "",
            "last_beat": None,
            "last_run": None,
            "next_run": None,
            "last_error": "",
            "error_at": None,
            "extra": {},
            "age": None,
        }
        if row:
            row.pop("mode", None)  # dropped from the payload, see module header
            extra = _decode_extra(row.pop("extra", None))
            entry.update(row)
            entry["extra"] = extra
            entry["label"] = meta["label"]
            entry["external_capable"] = meta["external"]
            # The heartbeat row has no say in these -- entry.update(row) above
            # would otherwise let a stray column overwrite them.
            entry["link"] = meta.get("link") or ""
            entry["state"] = _normalize_state(row.get("state"))
            try:
                beat_at = _dt.datetime.fromisoformat(row["last_beat"])
                # Clamped at zero: an NTP correction or a DST change can put
                # last_beat in the future, and a negative age would push the
                # stall deadline out by that much -- the countdown in the UI
                # would show hours remaining for a worker that just beat.
                entry["age"] = max(0, int((now - beat_at).total_seconds()))
                if beat_at > now:
                    beat_at = now
                # A deadline only exists while the worker claims to be working.
                # An idle worker is not going to be restarted for being quiet --
                # that is what idle means.
                window = entry["stall_after"]
                if window and entry["state"] == STATE_WORKING:
                    entry["stall_deadline"] = int(
                        (beat_at + _dt.timedelta(seconds=window)).timestamp())
            except Exception:
                pass
        out.append(entry)

    # Anything that reported but is not in WORKERS -- a module's own worker,
    # most likely. Show it rather than hide it.
    for name, row in rows.items():
        row.pop("mode", None)
        extra = _decode_extra(row.pop("extra", None))
        row.update({
            "label": name,
            "external_capable": False,
            "kind": KIND_CONTINUOUS,
            "fields": [F_STATUS],
            # Never a link for an unlisted worker: the URL would come from a
            # module, and the card renders it into an href.
            "link": "",
            "stall_after": None,
            "stall_deadline": None,
            "state": _normalize_state(row.get("state")),
            "extra": extra,
        })
        out.append(row)

    return out


def is_stalled(entry: dict, now_ts: float | None = None) -> bool:
    """Has *entry* (a snapshot() row) blown past its stall deadline?

    Only a worker that says it is working can stall: an idle worker is not
    expected to beat, and a scheduled worker has no deadline at all. This is
    the whole reason a healthy two-hour upscale used to be flagged -- it was
    judged against a fixed 15-minute window that nothing reset while it worked.
    """
    import time as _t

    deadline = entry.get("stall_deadline")
    if not deadline or entry.get("state") != STATE_WORKING:
        return False
    return (now_ts if now_ts is not None else _t.time()) > deadline


def health(workers: list[dict] | None = None) -> dict:
    """Compact roll-up used by /healthz and the settings badge.

    Pass an existing snapshot() result to avoid reading it twice -- the SSE
    stream builds both halves of its payload on every push and has no reason
    to hit the database again.
    """
    import time as _t

    workers = snapshot() if workers is None else workers
    now_ts = _t.time()
    errors = [w for w in workers if w["state"] == STATE_ERROR or w["last_error"]]
    stalled = [w for w in workers if is_stalled(w, now_ts)]
    return {
        "workers": len(workers),
        # Both keys are kept: "running" is what /healthz consumers already
        # parse, "working" is the name the UI uses now.
        "running": sum(1 for w in workers if w["state"] == STATE_WORKING),
        "working": sum(1 for w in workers if w["state"] == STATE_WORKING),
        "errors": [w["worker"] for w in errors],
        "stale": [w["worker"] for w in stalled],
        "ok": not errors and not stalled,
    }
