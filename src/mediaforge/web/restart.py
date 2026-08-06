"""Restart MediaForge's own process, in place.

Why this exists at all: a module *upgrade* cannot be applied to a running process.
Flask can add a blueprint to a live app but never replace one, and Python will not
re-import a package it has already imported — so an "upgrade" applied live would
leave the old code running behind new routes, which is worse than not upgrading.
The module store therefore stages every install into ``thirdparties/_pending/`` and
``apply_pending_changes()`` applies it at the next start, *before* anything imports
the folder. That design is correct, and it hands the user a bill: "restart
required". This module is how that bill gets paid with a button instead of a
terminal.

What a restart here means, precisely: the process is *replaced*, not rebuilt.
Nothing survives it — no imported module, no background thread, no blueprint, no
stale `sys.modules` entry. That is the entire point. Anything short of replacing
the process (rebuilding the Flask app, purging sys.modules, re-importing) leaves
debris behind, and the debris is exactly the thing that would run the old code.

Three environments, one mechanism:

- **Linux / Docker**: ``os.execv`` replaces the process image. The PID survives, so
  a container whose PID 1 is MediaForge stays up and its children (Xvfb, D-Bus, see
  entrypoint.sh) keep running; the environment (DISPLAY, TZ) is inherited.
- **Windows**: ``os.execv`` exists but is emulated — the parent dies immediately and
  the console handle goes with it, which mangles the terminal MediaForge was started
  from. So Windows gets a real child process (``subprocess.Popen``) and then exits.
- **Anything else / execv fails**: exit non-zero and let the supervisor
  (``restart: unless-stopped``, systemd, the Windows service wrapper) restart us.
  Logged loudly, because if there is no supervisor, this is a stop, not a restart.

The listening socket is closed before the replacement starts, and Python's sockets
are non-inheritable by default (PEP 446), so the new process can bind the port
immediately — no "address already in use" race with the process we just left.
"""

import os
import subprocess
import sys
import threading
import time

from ..logger import get_logger

logger = get_logger(__name__)

# Set by app.py's run() once it owns a server it can shut down. Until then a restart
# is not something we can honestly offer, so restart_supported() says so rather than
# leaving a button that does nothing.
_restart_hook = None

# One restart per lifetime, obviously. Two clicks must not race two re-execs.
_restarting = threading.Event()


def register_restart_handler(fn) -> None:
    """Called by run() with a callable that shuts the server down and re-execs."""
    global _restart_hook
    _restart_hook = fn


def restart_supported() -> bool:
    """Whether this process can restart itself.

    False in debug mode (Werkzeug's reloader owns the process there and re-execs on
    its own terms) and false if run() never installed a handler — for instance when
    MediaForge is embedded in someone else's WSGI server, where killing the process
    would take their app with it. The UI asks before it offers the button.
    """
    return _restart_hook is not None


def restart_pending() -> bool:
    """True once a restart has been asked for and is on its way."""
    return _restarting.is_set()


def request_restart(delay: float = 1.0) -> dict:
    """Ask the process to restart itself shortly.

    Returns immediately: the actual restart runs on a timer thread, so the HTTP
    response that triggered it is flushed *before* the socket disappears. A restart
    that kills the connection it was requested over looks, from the browser, exactly
    like a crash.
    """
    if not restart_supported():
        return {"ok": False, "error": "this process cannot restart itself"}
    if _restarting.is_set():
        return {"ok": True, "already": True}

    _restarting.set()
    logger.info("[Restart] Restart requested — replacing the process in %.1fs", delay)

    # Audited, and audited *here* rather than after the exec: once the process
    # is replaced there is nothing left to write the entry, so a restart used
    # to appear in the log only as an unexplained gap.
    try:
        from .audit_hooks import record_lifecycle

        record_lifecycle("restart_requested", delay=delay)
        # Flush synchronously: the writer thread does not survive the exec, and
        # a queued entry that never reaches disk is the same as no entry.
        from .audit import flush as _audit_flush

        _audit_flush(2.0)
    except Exception:
        logger.debug("[Restart] Audit hook failed", exc_info=True)

    def _run():
        time.sleep(delay)
        try:
            _restart_hook()
        except Exception:
            # The hook is expected to end the process. If it comes back, it failed, and
            # limping on with a half-shut-down server would be the worst of both worlds.
            logger.exception("[Restart] Restart failed — exiting and leaving it to the "
                             "supervisor (Docker restart policy / systemd / service).")
            os._exit(1)

    threading.Thread(target=_run, name="mediaforge-restart", daemon=True).start()
    return {"ok": True, "already": False}


# The distribution's top-level package, derived (not hard-coded) from where this module
# lives: mediaforge.web.restart → "mediaforge". Used as the last-resort re-exec form,
# ``python -m mediaforge``, which works from anywhere the package is importable — which is
# always, since we are running inside it.
_ROOT_PACKAGE = (__package__ or __name__).split(".")[0]


def reexec_argv() -> list:
    """The command line that starts this MediaForge again.

    FOUR ways MediaForge gets launched, and each rebuilds differently. Guessing one wrong is
    silent until someone hits restart — which is exactly what happened with the console-script
    case below.

    1. **Frozen build (PyInstaller):** ``sys.executable`` *is* MediaForge. Re-run it with the
       same arguments.

    2. **``python -m mediaforge``:** re-running ``sys.argv[0]`` would run ``__main__.py`` as a
       loose script with the wrong package context. ``__main__.__spec__.parent`` is the only
       reliable way back to the ``-m`` form (Werkzeug's reloader does the same).

    3. **``python path/to/run.py``:** a real script file. Re-run it under the interpreter.

    4. **Console script (``mediaforge``, the pip entry point):** and this is the one that was
       broken. ``sys.argv[0]`` is the wrapper's path *without* an extension —
       ``C:\\Python312\\Scripts\\mediaforge`` — and there is no such *file*: on Windows the
       real launcher is ``mediaforge.exe``. Prefixing it with ``sys.executable`` produced
       ``python.exe C:\\...\\Scripts\\mediaforge``, and python cannot open that as a script:
       *"can't open file 'mediaforge'"*. The fix does not try to guess the wrapper's extension
       at all — it re-runs the package it belongs to, ``python -m mediaforge``, which is
       exactly what the console script does anyway and works identically on Windows and Linux.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]

    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    if spec is not None and getattr(spec, "parent", ""):
        return [sys.executable, "-m", spec.parent, *sys.argv[1:]]

    # A real script file the interpreter can open: python run.py.
    argv0 = sys.argv[0] or ""
    if argv0.endswith((".py", ".pyw")) and os.path.isfile(argv0):
        return [sys.executable, argv0, *sys.argv[1:]]

    # Anything else is the console-script launcher (no .py, or a bare "mediaforge" / an
    # .exe stub). Do not reconstruct its path — re-enter the package by name. sys.argv[1:]
    # carries the real CLI arguments (web, --web-port, …), so this reproduces the launch.
    return [sys.executable, "-m", _ROOT_PACKAGE, *sys.argv[1:]]


def replace_process() -> None:
    """Replace this process with a fresh one. Does not return (unless it fails).

    Called by run()'s restart hook *after* the server socket is closed and in-flight
    work has been told to stop.
    """
    args = reexec_argv()
    logger.info("[Restart] Re-executing: %s", " ".join(args))

    # Flush our own output first: os.execv does not run atexit handlers, and a log line
    # that never made it out of the buffer is a log line that does not exist.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    if os.name == "nt":
        # Windows has no real exec: os.execv() terminates the parent at once, which on
        # Windows also tears down the console it was attached to. Spawn a genuine child
        # and step aside instead.
        try:
            subprocess.Popen(args, cwd=os.getcwd(), env=os.environ.copy(), close_fds=True)
        except Exception:
            logger.exception("[Restart] Could not spawn the replacement process")
            raise
        os._exit(0)

    # POSIX (incl. Docker): the process image is replaced. PID, open volumes and child
    # processes (Xvfb, D-Bus) all survive — which is why a container does not need a
    # restart policy for this to work.
    os.execv(args[0], args)
