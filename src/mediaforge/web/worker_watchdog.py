"""Restart a continuous worker that stopped reporting.

The Operations view used to have a "stale" badge: a worker that claimed to be
working but had not sent a heartbeat for 15 minutes got flagged, and that was
the end of it. Somebody had to notice the badge and restart MediaForge. Worse,
the flag was mostly wrong -- the upscale worker beat only when a job started
and when it ended, so every episode that took longer than 15 minutes (which is
most of them) lit it up while working perfectly.

Both halves are fixed here and in worker_registry.py:

* the workers now beat on a ticker while they work, so silence means silence
  (see ``heartbeat_ticker`` below), and
* silence past the worker's own window is acted on instead of merely displayed.

What "restart" can and cannot mean
----------------------------------
A stuck Python thread cannot be killed from the outside. There is no
Thread.kill(), and there is no safe way to fake one -- the thread holds locks
and open file handles. So recovery is two mechanisms, applied in order:

1. **The thread is gone.** The worker crashed out of its loop, or was never
   restarted after a previous failure, while its module still believes it is
   running. This is fully recoverable: clear the module's "already started"
   flag and call its ensure function again. Most real stalls seen in practice
   are this one, because the loops catch exceptions per iteration -- a thread
   that does die, died hard.

2. **The thread is alive but wedged**, typically blocked in a network read or
   waiting on a captcha that will never be solved. A restart-requested event is
   set; the worker loops check it at their own safe points and unwind. If the
   worker is blocked in a syscall it will not see the event until that call
   returns, which is exactly why the event is *also* what the countdown in the
   UI is counting down to: the operator sees that the attempt happened.

Either way the attempt is recorded in the audit log, because "the upscaler
restarts itself every 30 minutes" is a fact about the system that should not
live only in a log file nobody reads.

Deliberately not watched: every scheduled worker. A worker that sleeps for an
hour between rounds is not stalled, it is waiting, and the previous code's
inability to tell those apart is the bug this whole module exists to close.
"""

from __future__ import annotations

import importlib
import threading
import time

from ..logger import get_logger
from . import worker_registry as _wr

logger = get_logger(__name__)

# How often the watchdog looks. Well below the smallest stall window (900s), so
# a restart happens within a minute of the deadline rather than a quarter of an
# hour after it -- and cheap enough that the cost is one snapshot() per minute.
CHECK_INTERVAL = 60

# Heartbeat ticker period for a worker that is mid-job. Small relative to every
# stall window (the tightest is 900s, so a healthy long job sits at 15 beats of
# headroom) and large enough that the write rate stays negligible: with all
# three continuous workers busy that is one UPSERT every 20 seconds, on a table
# with one row per worker. Chosen with SQLite on slow storage (NAS, SD card) in
# mind -- diagnostics must not cost measurable I/O.
TICKER_INTERVAL = 60

# worker name -> how to bring it back:
#   module  import path of the module owning the worker
#   ensure  idempotent "start it if it isn't running" function
#   flag    module-level bool the ensure function guards itself with, which
#           has to be cleared before ensure will do anything
#   thread  the name the worker's thread is started with
_RECOVERY: dict[str, dict] = {
    "queue": {
        "module": "mediaforge.web.queue_worker",
        "ensure": "_ensure_queue_worker",
        "flag": "_queue_worker_started",
        "thread": "queue-worker",
    },
    "encoding": {
        "module": "mediaforge.web.encoding_worker",
        "ensure": "_ensure_encoding_worker",
        "flag": "_encoding_worker_started",
        "thread": "encoding-worker",
    },
    "upscale": {
        "module": "mediaforge.web.upscale_worker",
        "ensure": "_ensure_upscale_worker",
        "flag": "_upscale_worker_started",
        "thread": "upscale-worker",
    },
}

# Set by the watchdog, cleared by the worker once it has unwound. A worker loop
# checks restart_requested() at the top of each iteration and around any long
# wait it controls.
_restart_events: dict[str, threading.Event] = {}
_events_lock = threading.Lock()

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()


# ---------------------------------------------------------------------------
# The worker side
# ---------------------------------------------------------------------------

def _event_for(worker: str) -> threading.Event:
    with _events_lock:
        event = _restart_events.get(worker)
        if event is None:
            event = _restart_events[worker] = threading.Event()
        return event


def restart_requested(worker: str) -> bool:
    """For a worker loop: has the watchdog asked me to unwind and come back?"""
    return _event_for(worker).is_set()


def clear_restart(worker: str) -> None:
    """For a worker loop: acknowledge the request, after actually unwinding."""
    _event_for(worker).clear()


def worker_exiting(worker: str) -> None:
    """For a worker loop: "my thread is about to end, so I am not running".

    Must be called before a loop returns, and this is not optional. Each
    worker module guards its own start with a module-level ``_started`` flag,
    and that flag is what makes ``_ensure_*_worker()`` idempotent -- a thread
    that ends without clearing it leaves the module permanently convinced the
    worker is running, so nothing will ever start it again.

    Getting this wrong turns the watchdog into the opposite of what it is for:
    it asks a stalled worker to unwind, the worker obliges, and the worker is
    then dead until the process restarts. Clearing the flag here means the
    next watchdog pass sees a dead thread and starts a fresh one.
    """
    recovery = _RECOVERY.get(worker)
    if not recovery:
        return
    clear_restart(worker)
    try:
        module = importlib.import_module(recovery["module"])
        setattr(module, recovery["flag"], False)
    except Exception:
        logger.exception("[Watchdog] Could not clear the started flag for %s", worker)


class heartbeat_ticker:
    """Keep a worker's heartbeat fresh for as long as a job runs.

    Used as a context manager around the actual work::

        with heartbeat_ticker("upscale", detail=f"{title} S{season}E{episode}"):
            run_the_long_thing()

    Without this a long job looks identical to a wedged one, which is precisely
    the false "overdue" the Operations view used to show for a two-hour upscale:
    the worker beat at job start, then went quiet for two hours because it was
    busy, and the stall check could not tell that apart from a hang.

    The ticker thread is a daemon and does nothing but call
    ``worker_registry.working()``, which already swallows its own errors -- so
    the worst a broken heartbeat can do here is nothing at all.
    """

    def __init__(self, worker: str, detail: str = "", interval: int = TICKER_INTERVAL,
                 extra: dict | None = None):
        self.worker = worker
        self.detail = detail
        self.interval = max(5, int(interval))
        self.extra = extra
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Begin ticking. Idempotent, so a retry path cannot start two."""
        if self._thread is not None:
            return self
        _wr.working(self.worker, self.detail, extra=self.extra)

        def _tick():
            while not self._stop.wait(self.interval):
                # Checked again after the wait returns: stop() can be called in
                # the window between wait() timing out and the beat below. A
                # tick that lands after the job reported done() would flip the
                # worker back to "working" and have the Operations view show a
                # stall countdown for a worker that is idle.
                if self._stop.is_set():
                    return
                _wr.working(self.worker, self.detail)

        self._thread = threading.Thread(
            target=_tick, daemon=True, name=f"heartbeat-{self.worker}")
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop ticking. Safe to call more than once, and safe to call on a
        ticker that was never started -- worker loops have several exit paths
        and none of them should have to know which."""
        self._stop.set()
        # Not joined: the ticker is a daemon whose only action is a heartbeat,
        # and the caller is usually finishing a job it wants to report on
        # immediately. Waiting up to `interval` seconds here would add that
        # delay to every single job.

    def update(self, detail: str) -> None:
        """Change what the next tick reports (progress, current file, ...)."""
        self.detail = detail

    # Context-manager form, for a worker whose job body is a single block.
    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


# ---------------------------------------------------------------------------
# The watchdog side
# ---------------------------------------------------------------------------

def _thread_alive(name: str) -> bool:
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def _restart(worker: str, entry: dict, age: int) -> str:
    """Try to bring *worker* back. Returns what was done, for the log/audit."""
    recovery = _RECOVERY.get(worker)
    if not recovery:
        return "no recovery registered"

    try:
        module = importlib.import_module(recovery["module"])
    except Exception:
        logger.exception("[Watchdog] Could not import %s", recovery["module"])
        return "import failed"

    alive = _thread_alive(recovery["thread"])
    # Ask the worker to unwind either way: if it is alive it may act on this,
    # and if it is gone the flag is harmless and cleared on the next start.
    _event_for(worker).set()

    if alive:
        # Nothing further is safe. The thread holds locks and possibly a
        # half-written file; the request above is the whole intervention.
        logger.warning(
            "[Watchdog] %s has not reported for %ds but its thread is alive — "
            "restart requested, waiting for it to unwind", worker, age)
        _wr.beat(worker, state=_wr.STATE_ERROR,
                 detail="stalled — restart requested",
                 error=f"no heartbeat for {age}s")
        return "restart requested (thread alive)"

    # The thread is gone but the module still thinks it started it, so its
    # ensure function would return immediately. Clearing the flag is what makes
    # the restart actually happen.
    try:
        setattr(module, recovery["flag"], False)
        clear_restart(worker)
        getattr(module, recovery["ensure"])()
    except Exception as exc:
        logger.exception("[Watchdog] Restart of %s failed", worker)
        _wr.fail(worker, f"watchdog restart failed: {exc}")
        return "restart failed"

    logger.warning("[Watchdog] %s was dead (no heartbeat for %ds) — restarted", worker, age)
    _wr.beat(worker, state=_wr.STATE_IDLE, detail="restarted by watchdog", error="")
    return "restarted"


def _audit(worker: str, action: str, age: int) -> None:
    try:
        from .audit import audit

        audit("worker", "worker_stalled", severity="warning",
              target=worker, detail={"action": action, "silent_for_seconds": age})
    except Exception:
        logger.debug("[Watchdog] Could not write audit entry", exc_info=True)


def check_once() -> list[str]:
    """One pass. Returns the workers acted on -- also the unit-test entry point.

    Two independent checks, because they catch different failures:

    * **Liveness.** A worker whose thread is simply gone. Checked regardless of
      the state it last reported, which matters more than it sounds: a worker
      that crashed, or that unwound on request, last said "idle", and an
      idle worker never has a stall deadline. Judging only by the reported
      state would mean the one failure the watchdog can fully repair is the one
      it never notices.
    * **Stall.** A worker that says it is working but has stopped reporting.
    """
    acted = []
    now_ts = time.time()
    for entry in _wr.snapshot():
        worker = entry.get("worker")
        recovery = _RECOVERY.get(worker)
        if not recovery:
            continue

        if not _thread_alive(recovery["thread"]):
            # Nothing to ask and nothing to wait for: start a new thread.
            action = _restart(worker, entry, entry.get("age") or 0)
            _audit(worker, action, entry.get("age") or 0)
            acted.append(worker)
            continue

        if _wr.is_stalled(entry, now_ts):
            age = entry.get("age") or 0
            action = _restart(worker, entry, age)
            _audit(worker, action, age)
            acted.append(worker)
    return acted


def _loop() -> None:
    while not _stop.wait(CHECK_INTERVAL):
        try:
            check_once()
        except Exception:
            # A watchdog that dies of its own exception is worse than no
            # watchdog, because the UI keeps promising it is there.
            logger.exception("[Watchdog] Check failed")


def start() -> None:
    """Start the watchdog thread once. Idempotent.

    Only started where the workers actually run: in the web process by default,
    in the worker host when workers are external. Running it in both would mean
    two processes racing to restart the same worker.
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        # Cleared inside the lock: a stop() landing between the flag and the
        # clear would be swallowed, leaving a watchdog that believes it is
        # stopped and keeps running.
        _stop.clear()
    threading.Thread(target=_loop, daemon=True, name="worker-watchdog").start()
    logger.info("[Watchdog] Started (checking every %ds)", CHECK_INTERVAL)


def stop() -> None:
    global _started
    _stop.set()
    with _start_lock:
        _started = False
