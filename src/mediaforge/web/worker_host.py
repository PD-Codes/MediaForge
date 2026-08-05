"""Run the background workers in a process of their own.

Why
---
Every worker -- download queue, encoding, upscaling, Auto-Sync, TMDB keywords
-- runs as a daemon thread inside the Flask process. That is the simplest
thing that works, and for a single-user install it is the right answer. It
stops being the right answer as soon as the machine is shared:

* Python's GIL means a worker doing CPU-bound work in Python (scraping,
  parsing, hashing) competes with request handling. A scraper stuck in a
  blocking call makes the UI feel broken even though nothing is wrong with it.
* A worker that corrupts its own state, leaks memory or wedges a subprocess
  takes the web UI down with it -- including the page you would use to cancel
  the job that did it.
* There is no way to give the workers different resource limits, a different
  restart policy, or a different container.

This module lets those workers run somewhere else. It is **opt-in and
reversible**: with no configuration at all, nothing changes and the workers
keep running in the web process exactly as before.

How
---
Set ``MEDIAFORGE_WORKER_MODE=external`` on the web process and start a second
process with::

    python -m mediaforge.web.worker_host

Both must see the same ``MEDIAFORGE_CONFIG_DIR`` -- they coordinate purely
through the database, which they already did: ``claim_next_queued()`` and its
siblings are atomic ``UPDATE ... WHERE status='queued'`` statements written
precisely so two workers can never take the same job. That property is what
makes this a configuration change rather than a rewrite.

What this process deliberately does NOT do
------------------------------------------
It does not build a Flask app. The workers never needed one: ``get_db()``
falls back to a plain connection outside a request context, which is the path
they have always taken. Building an app here would start a second set of
schedulers, a second telemetry sender and a second library watcher.

It also does not run migrations. Exactly one process may own the schema, and
that is the web process -- see ``dbmigrate.run_pending()``. A worker host that
starts first waits for the schema instead of racing it.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

from ..logger import get_logger

logger = get_logger(__name__)

# Workers this process can own. The value is the "ensure" function that starts
# the worker's thread; every one of them is idempotent.
_WORKERS: dict[str, str] = {
    "queue":         "mediaforge.web.queue_worker:_ensure_queue_worker",
    "encoding":      "mediaforge.web.encoding_worker:_ensure_encoding_worker",
    "upscale":       "mediaforge.web.upscale_worker:_ensure_upscale_worker",
    "autosync":      "mediaforge.web.autosync_worker:_ensure_autosync_worker",
    "tmdb_keywords": "mediaforge.web.tmdb_keywords_sync:_ensure_tmdb_keywords_sync_worker",
}

DEFAULT_WORKERS = tuple(_WORKERS)

_stop = threading.Event()


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

def worker_mode() -> str:
    """``"inprocess"`` (default) or ``"external"``.

    Read from the environment rather than from a setting on purpose: it has to
    be answerable before the database is open, and it is a deployment
    decision, not a preference. A setting would also be editable from a UI the
    change cannot take effect in without a restart of two processes.
    """
    value = (os.environ.get("MEDIAFORGE_WORKER_MODE") or "inprocess").strip().lower()
    return "external" if value in ("external", "separate", "host") else "inprocess"


def workers_run_in_web_process() -> bool:
    return worker_mode() != "external"


def selected_workers() -> list[str]:
    """Which workers this host owns. ``MEDIAFORGE_WORKERS=queue,encoding``."""
    raw = (os.environ.get("MEDIAFORGE_WORKERS") or "").strip()
    if not raw:
        return list(DEFAULT_WORKERS)
    wanted, unknown = [], []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        if name in _WORKERS:
            wanted.append(name)
        else:
            unknown.append(name)
    if unknown:
        # Loud, not silent. A typo here means a worker nobody is running, and
        # the symptom is "downloads just sit there" with nothing in the log.
        logger.error("[WorkerHost] Unknown worker(s) in MEDIAFORGE_WORKERS: %s. Known: %s",
                     ", ".join(unknown), ", ".join(sorted(_WORKERS)))
    return wanted or list(DEFAULT_WORKERS)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _resolve(path: str):
    module_name, attr = path.split(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def _wait_for_schema(timeout: float = 120.0) -> bool:
    """Block until the web process has finished migrating.

    Only one process may own the schema. If this host starts first -- which it
    will, in any Compose file that does not spell out a dependency -- it must
    not run the workers against a half-built database. Waiting is the cheap
    correct answer; migrating here as well would mean two processes racing to
    ALTER the same tables.
    """
    from . import dbmigrate

    deadline = time.time() + timeout
    warned = False
    while time.time() < deadline:
        try:
            state = dbmigrate.status()
            if state["current"] >= state["latest"] and state["latest"] > 0:
                return True
            if not warned:
                logger.info("[WorkerHost] Waiting for the web process to migrate the "
                            "schema (at %s, needs %s)…", state["current"], state["latest"])
                warned = True
        except Exception as exc:
            if not warned:
                logger.info("[WorkerHost] Waiting for the database to appear (%s)…", exc)
                warned = True
        if _stop.wait(2.0):
            return False
    logger.warning("[WorkerHost] Schema did not become current within %.0fs -- starting "
                   "anyway. If the workers fail with \"no such table\", start the web "
                   "process first.", timeout)
    return False


def _prepare_environment() -> None:
    """Apply the same DB-derived environment the web process applies.

    The workers read a dozen settings through ``os.environ`` rather than
    through ``get_setting()`` (a legacy of the .env era, see
    settings_migration.py). Without this the host would run with defaults for
    all of them and quietly download to the wrong place.
    """
    from .settings_migration import _apply_captcha_env, _sync_db_settings_to_env

    _sync_db_settings_to_env()
    _apply_captcha_env()

    # Movie subfolder: three env vars, one setting, set the same way app.py
    # sets them. Kept in sync by hand there too; if that grows a helper this
    # should call it.
    from .db import get_setting
    subfolder = get_setting("movie_subfolder") or get_setting("filmpalast_movie_subfolder", "0")
    os.environ["MEDIAFORGE_MOVIE_SUBFOLDER"] = subfolder
    os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = subfolder
    os.environ["MEGAKINO_MOVIE_SUBFOLDER"] = subfolder

    # DNS routing. The workers are the only thing that actually resolves
    # provider hostnames, so if anything needs this applied it is this process.
    try:
        from .dns_patch import _DNS_PRESETS, _apply_dns_patch
        saved_mode = get_setting("dns_mode", "system")
        if saved_mode == "system":
            _apply_dns_patch(None, mode="system")
        else:
            server = _DNS_PRESETS.get(saved_mode) or get_setting("dns_server", "") or None
            _apply_dns_patch(server, mode=saved_mode)
    except Exception as exc:
        logger.warning("[WorkerHost] Could not apply the DNS setting: %s", exc)


def _init_tables() -> None:
    """Create the tables the selected workers touch.

    Every ``init_*_db()`` is ``CREATE TABLE IF NOT EXISTS`` plus idempotent
    ALTERs, so calling them here is safe even though the web process already
    did. It matters for the case where this host is started against a config
    directory the web process has not opened yet.
    """
    from .db import (init_app_settings_db, init_autosync_db, init_custom_paths_db,
                     init_download_history_db, init_encoding_queue_db,
                     init_language_groups_db, init_library_db, init_notification_db,
                     init_queue_db, init_upscale_queue_db)

    for init in (init_app_settings_db, init_queue_db, init_custom_paths_db,
                 init_language_groups_db, init_autosync_db, init_download_history_db,
                 init_library_db, init_notification_db, init_upscale_queue_db,
                 init_encoding_queue_db):
        try:
            init()
        except Exception as exc:
            logger.warning("[WorkerHost] %s failed: %s", init.__name__, exc)


def _handle_signals() -> None:
    def _shutdown(signum, _frame):
        logger.info("[WorkerHost] Signal %s received -- shutting down", signum)
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, AttributeError, OSError):
            # Not the main thread, or a platform without this signal.
            pass


def run(workers=None) -> int:
    """Start the selected workers and block until interrupted."""
    from . import audit as _audit
    from . import worker_registry as _wr

    os.environ.setdefault("MEDIAFORGE_WORKER_MODE", "external")
    names = list(workers or selected_workers())

    logger.info("[WorkerHost] Starting with worker(s): %s (pid %d)",
                ", ".join(names), os.getpid())

    _handle_signals()
    _audit.init_audit_db()
    _wait_for_schema()
    _init_tables()
    _prepare_environment()

    _audit.audit("system", "worker_host_started", target=",".join(names),
                 detail={"pid": os.getpid()}, severity="notice",
                 actor_name="worker-host")

    started = []
    for name in names:
        try:
            _resolve(_WORKERS[name])()
            started.append(name)
            _wr.beat(name, state="idle", detail="external host")
        except Exception as exc:
            logger.error("[WorkerHost] Could not start %r: %s", name, exc, exc_info=True)
            _wr.fail(name, "worker host could not start it: %s" % exc)

    if not started:
        logger.error("[WorkerHost] No worker started -- nothing to do, exiting")
        return 1

    logger.info("[WorkerHost] Running. Ctrl-C or SIGTERM to stop.")
    try:
        while not _stop.wait(5.0):
            pass
    except KeyboardInterrupt:
        pass

    logger.info("[WorkerHost] Stopped")
    _audit.audit("system", "worker_host_stopped", target=",".join(started),
                 severity="notice", actor_name="worker-host")
    _audit.flush(3.0)
    # The workers are daemon threads; nothing to join. A job that was mid-flight
    # stays "running" in the database until the next host start resets it, which
    # is what reset_running_*_items() at worker startup already does.
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        print("Options:")
        print("  --workers a,b   Run only these workers (default: %s)"
              % ",".join(DEFAULT_WORKERS))
        return 0

    workers = None
    if argv and argv[0] == "--workers" and len(argv) > 1:
        os.environ["MEDIAFORGE_WORKERS"] = argv[1]
        workers = selected_workers()

    return run(workers)


if __name__ == "__main__":
    raise SystemExit(main())
