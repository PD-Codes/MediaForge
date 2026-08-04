"""Background telemetry sender.

A single bounded queue + one daemon worker thread per process, matching
TELEMETRY_IMPLEMENTATION_PLAN.md §3.5 exactly: batches of up to ~20 events (or
whatever has accumulated once the queue drains), POSTed via GLOBAL_SESSION
with a 3s timeout, silently dropped on any failure. No retry, no backoff, no
offline queue persisted to disk -- a lost batch during a flaky/offline moment
is an accepted trade-off in v1, in exchange for never blocking or slowing
down the app the queue/worker/autosync threads actually care about.
"""

import json
import platform
import queue
import threading

from ..config import GLOBAL_SESSION, VERSION
from ..logger import get_logger
from . import device_auth, settings
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

    @staticmethod
    def _post_ingest(body, _retried=False):
        """POST one already-serialised batch, signed if we have a secret.

        The one retry this module allows itself, and it is not a retry of a
        failed send: the server answers 401 with a body naming
        ``/telemetry/register`` when an install it knows to be enrolled sends
        an unsigned or stale-keyed request. That happens whenever our stored
        secret and the server's record of it disagree -- a restored database
        backup, a wiped server row, a rotation that only half landed -- and
        the only fix is to re-register. Exactly once, and only for that
        specific 401: retrying a rate limit or a 5xx here would turn an
        overloaded server into an overloaded server being hammered.
        """
        headers = {
            "X-Project-Key": TELEMETRY_PROJECT_KEY,
            "Content-Type": "application/json",
        }
        headers.update(device_auth.sign_headers("POST", TELEMETRY_INGEST_URL, body))
        resp = GLOBAL_SESSION.post(
            TELEMETRY_INGEST_URL, data=body, headers=headers, timeout=_POST_TIMEOUT,
        )
        if not _retried and getattr(resp, "status_code", None) == 401:
            if device_auth.needs_registration(resp) and device_auth.rotate():
                return TelemetryClient._post_ingest(body, _retried=True)
        return resp

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
                # Declarative snapshot of "what this install currently has toggled
                # on" -- sent with every batch (not just when one of these keys
                # happens to produce a real event) so the server's Install.enabled_keys
                # always reflects the actual current Settings-page state instead of
                # drifting to "whatever data_key last happened to fire an event"
                # (see telemetry_ingest.py's use of this field on the server side --
                # a raised stage with no matching event yet, e.g. downloads.titles
                # enabled but no download attempted, would otherwise look like it
                # was never actually enabled).
                "enabled_keys": sorted(settings.get_enabled_keys()),
                "events": batch,
            }
            # Serialise once and post those exact bytes (data=, not json=):
            # the signature covers sha256 of the raw body, so re-serialising
            # after signing would produce a signature over a different
            # document -- key order and whitespace are enough to break it.
            body = json.dumps(payload).encode("utf-8")
            # Enrollment is attempted here rather than at startup because this
            # is the first point where the app is definitely about to send
            # something: _flush() only ever runs on events that got past
            # submit()'s telemetry_active() gate, so a user with telemetry off
            # never registers a device with the server at all.
            device_auth.ensure_enrolled()
            resp = self._post_ingest(body)
            # A rejected batch (401 from a project-key mismatch, 429 from the
            # rate limiter, 5xx from a half-broken deployment) is not an
            # exception -- without this line it looked exactly like a successful
            # send, which is how a server-side config problem once went
            # unnoticed. Still DEBUG: the user can neither see nor fix the
            # devInfo server, and telemetry failing is by design harmless.
            status = getattr(resp, "status_code", None)
            if status is not None and status >= 400:
                logger.debug("[Telemetry] ingest rejected %d event(s) with HTTP %s (dropped)",
                             len(batch), status)
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
