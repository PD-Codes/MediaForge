"""Telemetry hook wiring -- four independent capture paths, because a real
crash in this app can reach the user in four different ways:

  1. sys.excepthook       -- an unhandled exception on the MAIN thread.
  2. threading.excepthook -- an unhandled exception on any OTHER thread
                              (Python 3.8+; sys.excepthook never fires here).
                              This app starts ~30 background daemon threads
                              (queue_worker, autosync_worker, upscale_worker,
                              calendar/uptime/mediascan loops, ...) -- before
                              this hook existed, a crash in any one of them
                              was invisible to telemetry no matter how badly
                              it failed, since neither sys.excepthook nor the
                              Flask error handler below ever sees it.
  3. the Flask error handler -- an exception that reaches Flask's request
                              dispatch without being caught by the view.
  4. the logging handler  -- BY FAR the most common case in practice: code
                              that already catches its own exception and
                              reports it via logger.error(...)/.exception(...)
                              (a provider's scrape failing, an ffmpeg
                              subprocess erroring out, a network timeout) and
                              deliberately does NOT re-raise, so it would
                              never reach any of the three hooks above. Since
                              logger.py's get_logger() hands back one single
                              shared "mediaforge" logger instance no matter
                              which module calls it, attaching one handler to
                              that one instance sees every ERROR-level log
                              call anywhere in the codebase, with no changes
                              needed at any individual call site.

init_telemetry(app) is the single entry point called once from
web/app.py's create_app() -- see that call site for why it's placed where
it is (right next to the other always-on background workers).
"""

import functools
import logging
import sys
import threading

from ..logger import get_logger
from . import events
from .client import get_client

logger = get_logger(__name__)

_excepthook_installed = False
_excepthook_lock = threading.Lock()
_thread_excepthook_installed = False
_thread_excepthook_lock = threading.Lock()
_log_handler_installed = False
_log_handler_lock = threading.Lock()


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


def install_thread_excepthook():
    """Wrap threading.excepthook (Python 3.8+) so an unhandled exception on
    ANY background thread is reported, the same way install_excepthook()
    covers the main thread. See the module docstring's capture path #2 --
    without this, none of this app's daemon worker threads were ever able to
    report a crash unless they happened to route it through logger.error()
    (see install_log_handler() below) or re-raise all the way out.

    Safe to call more than once; only the first call actually wraps the hook.
    """
    global _thread_excepthook_installed
    with _thread_excepthook_lock:
        if _thread_excepthook_installed:
            return
        _thread_excepthook_installed = True

        previous_hook = threading.excepthook

        def _telemetry_thread_excepthook(args):
            # args is a threading.ExceptHookArgs namedtuple:
            # (exc_type, exc_value, exc_traceback, thread)
            _report_exception(args.exc_type, args.exc_value, args.exc_traceback)
            previous_hook(args)

        threading.excepthook = _telemetry_thread_excepthook
        logger.debug("[Telemetry] threading.excepthook installed")


class _TelemetryLogHandler(logging.Handler):
    """Reports every ERROR-level-or-above record on the shared "mediaforge"
    logger as a crash_reports event -- see the module docstring's capture
    path #4, the one that actually matters most in practice: nearly every
    real failure in this codebase (a provider's scrape failing, ffmpeg
    erroring out, a timed-out request) is already caught and logged via
    logger.error(...)/.exception(...) and deliberately not re-raised, so it
    never reaches sys.excepthook/threading.excepthook/the Flask handler.

    If the log call happened from inside the except block that caught the
    error (the overwhelming majority of logger.error(f"...: {e}") call sites
    in this codebase), sys.exc_info() is still populated at the time this
    handler runs -- logging is synchronous on the same thread/call stack --
    so a full, real traceback is available even when the call site never
    passed exc_info=True explicitly. Only when there is truly no exception
    object anywhere (a bare logger.error("something looks wrong") with no
    except block at all) does this fall back to a location-only report.
    """

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        try:
            exc_info = record.exc_info or sys.exc_info()
            if exc_info and exc_info[0] is not None:
                event = events.build_crash_event(*exc_info)
            else:
                event = events.build_log_error_event(record)
            if event:
                get_client().submit(event)
        except Exception:
            pass  # a bug in telemetry must never take down logging itself


def install_log_handler():
    """Attach _TelemetryLogHandler to the one shared "mediaforge" logger
    instance (logger.py's get_logger() -- singleton regardless of which
    module calls it, propagate=False so this is the only place records from
    anywhere in the app pass through). Safe to call more than once; only the
    first call actually attaches the handler.
    """
    global _log_handler_installed
    with _log_handler_lock:
        if _log_handler_installed:
            return
        _log_handler_installed = True
        get_logger(__name__).addHandler(_TelemetryLogHandler())
        logger.debug("[Telemetry] log handler installed")


def register_error_handler(app):
    """Register a Flask app.errorhandler(Exception) that reports the crash
    and then re-raises, handing back to Flask's own normal exception
    handling (500 page / interactive debugger in debug mode) -- this must
    never swallow the error, only observe it (TELEMETRY_IMPLEMENTATION_PLAN.md
    §3.6: "meldet den Fehler und gibt danach den normalen 500-Handler
    zurück -- kein Verschlucken von Fehlern").

    Used by: init_telemetry(app), called once from create_app().
    """
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)
    def _telemetry_flask_error_handler(e):
        # Exception's MRO covers HTTPException too (404, 403, 405, ...) -- those are
        # Flask/Werkzeug doing their normal job (a browser requesting /favicon.ico on an
        # app that doesn't serve one, a bad method on a route, ...), not application
        # crashes. Reporting them would flood crash_reports with routing noise, and
        # re-raising one here breaks Flask's own default HTTPException handling and
        # turns a harmless 404 into a logged 500 (this is the standard Flask "generic
        # exception handler" pitfall -- see Flask's docs on errorhandler(Exception)).
        # Returning the exception itself lets Flask render its normal error response.
        if isinstance(e, HTTPException):
            return e
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
    """Install all four capture paths (see module docstring), register the
    Flask error handler, and start the background TelemetryClient worker
    thread. Called once from create_app() -- see web/app.py, placed next to
    the other always-on background workers (devinfos poller, update checker,
    ...) started there.
    """
    install_excepthook()
    install_thread_excepthook()
    install_log_handler()
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
