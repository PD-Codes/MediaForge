"""Web server thread count and the budget for long-lived streams.

waitress serves every request from a fixed pool of worker threads, and a
Server-Sent-Events response occupies one of them for as long as the browser
keeps it open. Nothing limited how many of those there could be: the SyncPlay
stream is reachable by guests without an account, so a watch party -- or
anybody opening streams with a guest token -- could take every thread and
freeze the whole UI for everyone.

The streams therefore get a budget: the pool size minus a reserve that always
stays free for page loads, API polls and static files.
"""

from __future__ import annotations

import os
import threading

_DEFAULT_THREADS = 32
# Threads that never go to a stream, whatever the pool size.
_RESERVED_THREADS = 8

_slots = None
_slots_lock = threading.Lock()


def web_threads() -> int:
    """Size of the waitress thread pool (MEDIAFORGE_WEB_THREADS, default 32)."""
    try:
        value = int(os.environ.get("MEDIAFORGE_WEB_THREADS", str(_DEFAULT_THREADS)))
    except ValueError:
        value = _DEFAULT_THREADS
    return max(4, min(value, 256))


def stream_budget() -> int:
    """How many streams may be open at once."""
    return max(2, web_threads() - _RESERVED_THREADS)


def _semaphore():
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(stream_budget())
        return _slots


def try_acquire_stream() -> bool:
    """Take one stream slot without waiting. False when the budget is used up."""
    return _semaphore().acquire(blocking=False)


def release_stream() -> None:
    """Give a slot back. Call exactly once per successful try_acquire_stream()."""
    try:
        _semaphore().release()
    except ValueError:
        # Released more often than acquired -- a bug elsewhere, but never a
        # reason to break the stream that is shutting down.
        pass
