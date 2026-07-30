"""Dev Info feed poller — periodically fetches changelog/status posts from a
fixed, unauthenticated Dev Info server and caches them locally.

Mirrors the shape of uptime_monitor.py: a module-level threading.Event used
to wake the loop early, a _loop() that sleeps via event.wait(timeout=interval)
(interruptible), and a _start_devinfos_poller() that spawns the daemon thread
once. Unlike uptime_monitor.py, this feature is always-on and unconfigurable:
there is no enabled/disabled flag and no admin-settable URL.
"""

import threading
import time

from ..logger import get_logger
from ..telemetry.classify import is_server_unreachable as _is_server_unreachable
from .db import replace_devinfo_posts

logger = get_logger(__name__)

# Fixed Dev Info server endpoint. Intentionally NOT admin-configurable —
# do not add a settings UI or DB-backed override for this.
DEVINFOS_SERVER_URL = "https://mediaforge.softarchiv.com"
DEVINFOS_POLL_INTERVAL_SECONDS = 300

# Default short timeout for the outbound fetch — the remote admin app is a
# separate, possibly-unreachable service; a hung request must never block
# this thread for long.
_FETCH_TIMEOUT = 8

_devinfos_monitor_started = False
_devinfos_monitor_lock = threading.Lock()
_devinfos_wake = threading.Event()  # set to wake the poller early
_devinfos_last_fetch_ts = 0.0  # updated on every fetch attempt, used to rate-limit
                                # the on-page-visit "refresh now" trigger below


def _devinfos_fetch_and_store():
    """Fetch the remote Dev Info feed once and replace the local cache.

    Any failure (unreachable server, bad JSON, timeout, ...) is caught and
    logged — the previously cached posts are simply left in place so a
    flaky/offline remote admin app never crashes the poller or blanks out
    the sidebar badge / Dev Infos page.

    Used by: _start_devinfos_poller()'s loop below.
    """
    global _devinfos_last_fetch_ts
    if not DEVINFOS_SERVER_URL:
        return
    _devinfos_last_fetch_ts = time.time()
    try:
        # Reuse the project's shared, DNS-patched HTTP session (GLOBAL_SESSION)
        # for consistency with every other outbound request in this project
        # (niquests-based, routes through the configured DoH resolver) rather
        # than a bare urllib/requests call.
        from ..config import GLOBAL_SESSION as _GS

        base = DEVINFOS_SERVER_URL.rstrip("/")
        resp = _GS.get(f"{base}/api/posts", timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        raw_posts = data.get("posts") or []
        posts = []
        for rp in raw_posts:
            if not isinstance(rp, dict):
                continue
            posts.append({
                "id": rp.get("id"),
                "title": rp.get("title"),
                "body": rp.get("body"),
                "type": rp.get("type"),
                "author": rp.get("author"),
                "remote_created_at": rp.get("created_at"),
            })
        replace_devinfo_posts(posts)
    except Exception as exc:
        # One line, not a traceback: the Dev Info server being unreachable is a
        # routine event (no internet, DNS filtered, server down) and the cached
        # posts stay in place. A full stack at WARNING level buried the real
        # errors around it in 60 lines of noise -- same lesson as the ffmpeg
        # banner drowning out transcoding failures. The stack is still there
        # when debug logging is on.
        # The devInfo server is entirely optional infrastructure (a changelog
        # feed plus the opt-in telemetry endpoint). It being unreachable -- down
        # for maintenance, no internet, DNS filtered, behind a captive portal --
        # is a routine, harmless condition that the user can neither fix nor act
        # on, and this poller retries every 5 minutes anyway. Printing a warning
        # each round turned an offline weekend into hundreds of lines of console
        # noise for a feature nothing depends on, so an unreachable server is
        # DEBUG-only. Anything else (malformed JSON, a DB write failing, a bug
        # in this function) is a real problem and stays visible at WARNING.
        if _is_server_unreachable(exc):
            logger.debug("[DevInfos] server unreachable (%s) — keeping cached posts",
                         type(exc).__name__)
        else:
            logger.warning("[DevInfos] fetch failed (%s: %s) — keeping cached posts",
                           type(exc).__name__, str(exc)[:200])
        logger.debug("[DevInfos] fetch failure detail", exc_info=True)


def _start_devinfos_poller():
    """Start the background Dev Info poller loop once. Always runs,
    unconditionally, for the lifetime of the app — same as the update
    checker. Idle-waits (without fetching) while DEVINFOS_SERVER_URL is
    unset.

    Used by: web/app.py (called during app startup).
    """
    global _devinfos_monitor_started
    with _devinfos_monitor_lock:
        if _devinfos_monitor_started:
            return
        _devinfos_monitor_started = True

    def _loop():
        while True:
            if not DEVINFOS_SERVER_URL:
                _devinfos_wake.wait(timeout=10)
                _devinfos_wake.clear()
                continue
            try:
                _devinfos_fetch_and_store()
            except Exception as exc:
                # Same reasoning as in _devinfos_fetch_and_store(): a server that
                # simply is not answering must never reach the console.
                if _is_server_unreachable(exc):
                    logger.debug("[DevInfos] poller round skipped (server unreachable)")
                else:
                    logger.warning("[DevInfos] poller round failed", exc_info=True)
            # Sleep until the next round.
            _devinfos_wake.wait(timeout=DEVINFOS_POLL_INTERVAL_SECONDS)
            _devinfos_wake.clear()

    threading.Thread(target=_loop, daemon=True, name="devinfos-poller").start()


def request_immediate_refresh(min_interval=15):
    """Ask the poller to fetch right now instead of waiting for the next
    scheduled round -- used when a user opens the Dev Infos page, so newly
    published posts show up without waiting up to
    DEVINFOS_POLL_INTERVAL_SECONDS.

    Rate-limited to at most one extra fetch per ``min_interval`` seconds so
    repeated page visits/clicks cannot hammer the remote server. Returns
    True if a refresh was actually requested, False if skipped because the
    cache is already recent enough.

    Used by: web/routes/devinfos.py's devinfos_page().
    """
    if not DEVINFOS_SERVER_URL:
        return False
    if time.time() - _devinfos_last_fetch_ts < min_interval:
        return False
    _devinfos_wake.set()
    return True
