"""Telemetry hook wiring: sys.excepthook, the Flask error handler, and the
@telemetry_guarded decorator for background worker threads (which run
outside of both the main-thread excepthook path in some interpreter
configurations and entirely outside Flask's request handling).

init_telemetry(app) is the single entry point called once from
web/app.py's create_app() -- see that call site for why it's placed where
it is (right next to the other always-on background workers).
"""

import functools
import sys
import threading

from ..logger import get_logger
from . import events
from .client import get_client

logger = get_logger(__name__)

_excepthook_installed = False
_excepthook_lock = threading.Lock()


def _report_exception(exc_type, exc_value, tb):
    """Build (if enabled) and submit a crash_reports event. Wrapped in its
    own try/except so a bug in the telemetry path itself can never turn one
    crash report into a second, unrelated crash."""
    try:
        event = events.build_crash_event(exc_type, exc_value, tb)
        if event:
            get_client().submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit crash event", exc_info=True)


def install_excepthook():
    """Wrap sys.excepthook so unhandled exceptions in the main thread are
    reported before falling through to the previous hook (normally
    sys.__excepthook__, i.e. the default "print traceback to stderr"
    behaviour -- this never swallows or replaces that output).

    Safe to call more than once; only the first call actually wraps the hook.
    """
    global _excepthook_installed
    with _excepthook_lock:
        if _excepthook_installed:
            return
        _excepthook_installed = True

        previous_hook = sys.excepthook

        def _telemetry_excepthook(exc_type, exc_value, tb):
            _report_exception(exc_type, exc_value, tb)
            previous_hook(exc_type, exc_value, tb)

        sys.excepthook = _telemetry_excepthook
        logger.debug("[Telemetry] sys.excepthook installed")


def register_error_handler(app):
    """Register a Flask app.errorhandler(Exception) that reports the crash
    and then re-raises, handing back to Flask's own normal exception
    handling (500 page / interactive debugger in debug mode) -- this must
    never swallow the error, only observe it (TELEMETRY_IMPLEMENTATION_PLAN.md
    §3.6: "meldet den Fehler und gibt danach den normalen 500-Handler
    zurück -- kein Verschlucken von Fehlern").

    Used by: init_telemetry(app), called once from create_app().
    """

    @app.errorhandler(Exception)
    def _telemetry_flask_error_handler(e):
        exc_type, exc_value, tb = type(e), e, e.__traceback__
        _report_exception(exc_type, exc_value, tb)
        raise e


def telemetry_guarded(func):
    """Decorator for background worker-thread bodies (queue_worker,
    autosync_worker, upscale_worker, ...) that run entirely outside Flask
    request handling and therefore never reach the errorhandler above.
    Reports any exception the wrapped function raises, then re-raises it
    unchanged so the worker's own existing error handling/logging keeps
    working exactly as before -- this decorator only adds a report, it
    changes no control flow.
    """

    @functools.wraps(func)
    def _wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            _report_exception(type(e), e, e.__traceback__)
            raise

    return _wrapped


def init_telemetry(app):
    """Install the excepthook, register the Flask error handler, and start
    the background TelemetryClient worker thread. Called once from
    create_app() -- see web/app.py, placed next to the other always-on
    background workers (devinfos poller, update checker, ...) started
    there.
    """
    install_excepthook()
    register_error_handler(app)
    get_client().start()

    # A one-off system_info event on startup (if the user has enabled it) so
    # the devInfo server sees an install "check in" even on runs with no
    # crash at all -- not gated behind any particular route/request.
    try:
        event = events.build_system_info_event()
        if event:
            get_client().submit(event)
    except Exception:
        logger.debug("[Telemetry] startup system_info event failed", exc_info=True)

    logger.debug("[Telemetry] initialized")
