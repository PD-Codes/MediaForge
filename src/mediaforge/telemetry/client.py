"""Background telemetry sender.

A single bounded queue + one daemon worker thread per process, matching
TELEMETRY_IMPLEMENTATION_PLAN.md §3.5 exactly: batches of up to ~20 events (or
whatever has accumulated once the queue drains), POSTed via GLOBAL_SESSION
with a 3s timeout, silently dropped on any failure. No retry, no backoff, no
offline queue persisted to disk -- a lost batch during a flaky/offline moment
is an accepted trade-off in v1, in exchange for never blocking or slowing
down the app the queue/worker/autosync threads actually care about.
"""

import platform
import queue
import threading

from ..config import GLOBAL_SESSION, VERSION
from ..logger import get_logger
from . import settings
from .registry import TELEMETRY_INGEST_URL, TELEMETRY_PROJECT_KEY

logger = get_logger(__name__)

_QUEUE_MAXSIZE = 200  # caps memory use during an offline stretch -- see module docstring
_BATCH_MAX = 20
_GET_TIMEOUT = 5  # seconds -- also acts as the "flush roughly every 5s" cadence
_POST_TIMEOUT = 3  # seconds -- must never let a hung devInfo server stall the app


class TelemetryClient:
    """Owns the submit queue and its background worker thread.

    Use the module-level get_client() singleton rather than instantiating
    this directly, so the whole process shares one queue/thread.
    """

    def __init__(self):
        self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._thread = None
        self._start_lock = threading.Lock()

    def start(self):
        """Start the background worker thread once. Safe to call repeatedly
        (e.g. once from create_app() and again defensively elsewhere) --
        only the first call actually spawns the thread."""
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._worker, daemon=True, name="telemetry-client"
            )
            self._thread.start()
            logger.debug("[Telemetry] client worker thread started")

    def submit(self, event):
        """Enqueue a single already-built event dict ({"data_key", "occurred_at",
        "payload"}). Silently ignored if telemetry isn't active at all (defense
        in depth -- the event builders in events.py already gate on this
        before building anything) or if the queue is currently full (an
        offline stretch should never turn into unbounded memory growth or a
        blocked caller)."""
        if not event:
            return
        if not settings.telemetry_active():
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.debug("[Telemetry] queue full — dropping event %r", event.get("data_key"))

    def _worker(self):
        batch = []
        while True:
            try:
                batch.append(self._queue.get(timeout=_GET_TIMEOUT))
            except queue.Empty:
                pass
            if batch and (len(batch) >= _BATCH_MAX or self._queue.empty()):
                self._flush(batch)
                batch = []

    def _flush(self, batch):
        if not batch:
            return
        try:
            payload = {
                "install_id": settings.get_install_id(),
                "app_version": VERSION or "unknown",
                "os": platform.system(),
                "python_version": platform.python_version(),
                "arch": platform.machine(),
                "events": batch,
            }
            GLOBAL_SESSION.post(
                TELEMETRY_INGEST_URL,
                json=payload,
                headers={"X-Project-Key": TELEMETRY_PROJECT_KEY},
                timeout=_POST_TIMEOUT,
            )
        except Exception as e:
            # No retry, no backoff -- see module docstring. Debug-level only
            # since a flaky/offline devInfo server is an expected, harmless
            # condition, not something an operator needs to see in normal logs.
            logger.debug("[Telemetry] flush of %d event(s) failed (dropped): %s", len(batch), e)


_client = None
_client_lock = threading.Lock()


def get_client() -> TelemetryClient:
    """Process-wide TelemetryClient singleton."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TelemetryClient()
    return _client


def submit(event):
    """Convenience wrapper: get_client().submit(event). Used throughout
    events.py's callers instead of importing the client class directly."""
    get_client().submit(event)


def submit_all(events):
    """submit() a list of events (some builders in events.py return a list,
    e.g. build_download_event/build_watch_event, since more than one
    data_key can apply to the same underlying occurrence). Silently no-ops
    for an empty/None list."""
    for event in (events or []):
        submit(event)
