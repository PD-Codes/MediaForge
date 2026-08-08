"""DB-backed catalogue lists, served stale while they revalidate.

``mediaforge/catalogue.py`` knows how to FETCH and PARSE a source site's A-Z
list. It deliberately imports nothing from ``mediaforge.web`` -- it is core,
the web layer imports it and never the other way round. Persistence is a web
concern, so it lives here.

What changed, and why it matters more than it sounds:

Before, the lists lived in a process-local dict with a 12-hour TTL. Opening
the Catalogue page after a restart meant waiting on two multi-megabyte
downloads from sites behind DDoS-Guard, in the request, with a spinner. If
one of them was slow the page was slow; if one of them was down the page said
"unavailable" even though a perfectly good copy had existed ten minutes ago.

Now the page is answered from SQLite immediately -- always, including the
first open after a restart -- and anything stale is refreshed in the
background. A refresh that fails changes nothing the user can see except the
"last updated" line: the previous snapshot is still a real catalogue, and a
list of series titles does not rot in a day.

The one case that still blocks is a source with NO stored rows at all (fresh
install, newly installed module). There is nothing to serve there, so the
first call waits -- but only that one, and only once.

Progress is reported through :func:`status`, because a background job the
user cannot see is a background job the user assumes is broken.
"""

import threading
import time

from ..logger import get_logger

logger = get_logger(__name__)

# How old a stored catalogue may get before a refresh is triggered. Long on
# purpose: a site adds a handful of titles a week, the payload is megabytes,
# and refetching it more often than this is the single most expensive thing
# this app does to a source site. Once a day is plenty for an A-Z list.
CATALOGUE_TTL = 24 * 3600

# How often the background loop asks "is anything due?". This is a couple of
# cheap DB reads, not a fetch -- it is CATALOGUE_TTL above that decides how
# often a source is actually contacted, and this only controls how promptly a
# list that has come due is picked up. Hourly means a source is refetched
# about once a day, close to the hour it was last fetched.
CHECK_INTERVAL = 3600

# A failed refresh is not retried at TTL speed -- a site that is down stays
# down for a while, and hammering it every page open is both rude and useless.
FAILED_RETRY = 30 * 60

# The first call for a source with nothing stored has to wait. Everything
# after that is background, so this only bounds the very first page open.
COLD_FETCH_TIMEOUT = 90

_lock = threading.Lock()
_loop_stop = threading.Event()
_refreshing: dict = {}     # source_id -> {"since": float, "phase": str}
_last_attempt: dict = {}   # source_id -> float, including failed ones
_cold_waiters: dict = {}   # source_id -> threading.Event


def _sources():
    from ..catalogue import all_catalogues
    return all_catalogues()


# ─────────────────────────────────────────────────────────────────────────────
#  Refresh
# ─────────────────────────────────────────────────────────────────────────────
def _set_phase(source_id, phase):
    with _lock:
        if source_id in _refreshing:
            _refreshing[source_id]["phase"] = phase


def _do_refresh(source_id, entry):
    """Fetch one source and store it. Never raises."""
    from .db import mark_catalogue_failed, save_catalogue

    started = time.time()
    try:
        _set_phase(source_id, "fetching")
        entries = entry["fetch"]() or []
        if not entries:
            # An empty list is a parse failure, not an empty site: both
            # built-ins have thousands of titles, and a challenge page or a
            # redesign is exactly what produces zero matches. Storing it would
            # replace a good catalogue with nothing.
            raise ValueError("source returned no entries")
        _set_phase(source_id, "saving")
        count = save_catalogue(source_id, entries)
        logger.info("[Catalogue] %s: stored %d entries in %.1fs",
                    source_id, count, time.time() - started)
        return True
    except Exception as exc:
        # Not an error event, and deliberately not raised: the stored copy is
        # still being served, so this is a stale list, not a broken page.
        logger.warning("[Catalogue] %s refresh failed: %s: %s",
                       source_id, type(exc).__name__, exc)
        try:
            mark_catalogue_failed(source_id, "%s: %s" % (type(exc).__name__, exc))
        except Exception:
            pass
        return False
    finally:
        with _lock:
            _refreshing.pop(source_id, None)
            _last_attempt[source_id] = time.time()
            waiter = _cold_waiters.pop(source_id, None)
        if waiter is not None:
            waiter.set()
        _beat()


def start_refresh(source_id, entry=None) -> bool:
    """Kick off a background refresh unless one is already running.

    Returns whether this call started one. Single-flight per source: the
    Catalogue page is the kind of page people reload, and two tabs must not
    mean two 2.5 MB downloads from the same site.
    """
    entry = entry or _sources().get(source_id)
    if entry is None:
        return False
    with _lock:
        if source_id in _refreshing:
            return False
        _refreshing[source_id] = {"since": time.time(), "phase": "queued"}
        _cold_waiters.setdefault(source_id, threading.Event())
    _beat()
    threading.Thread(target=_do_refresh, args=(source_id, entry), daemon=True,
                     name="catalogue-refresh-%s" % source_id).start()
    return True


def _is_stale(source_id, meta) -> bool:
    info = meta.get(source_id)
    if not info or not info.get("count"):
        return True
    age = time.time() - (info.get("fetched_at") or 0)
    if info.get("status") == "failed":
        # Back off after a failure rather than retrying at TTL speed.
        with _lock:
            last = _last_attempt.get(source_id, 0)
        return (time.time() - last) > FAILED_RETRY
    return age > CATALOGUE_TTL


# ─────────────────────────────────────────────────────────────────────────────
#  Reads
# ─────────────────────────────────────────────────────────────────────────────
def get_entries(source_id, force=False, wait_when_empty=True):
    """``(entries, meta)`` for one source. ``entries`` is None only when there
    is nothing stored AND the fetch that had to run failed.

    This is the whole point of the module: it returns in microseconds whenever
    anything is stored, whatever its age, and lets the refresh happen behind
    the answer.
    """
    from .db import catalogue_meta, load_catalogue

    entry = _sources().get(source_id)
    if entry is None:
        return None, {}

    meta = catalogue_meta(source_id)
    stored = load_catalogue(source_id)

    if force or _is_stale(source_id, meta):
        start_refresh(source_id, entry)

    if stored:
        return stored, catalogue_meta(source_id).get(source_id, {})

    # Nothing stored at all. Only here does a caller wait, and only until the
    # in-flight fetch finishes -- the whole reason the rest of this module
    # exists is to make sure that is a once-per-source event.
    if not wait_when_empty:
        return [], meta.get(source_id, {})
    with _lock:
        waiter = _cold_waiters.get(source_id)
    if waiter is not None:
        waiter.wait(timeout=COLD_FETCH_TIMEOUT)
    stored = load_catalogue(source_id)
    return (stored or None), catalogue_meta(source_id).get(source_id, {})


def all_entries() -> dict:
    """``{source_id: [entry]}`` for every source that has something stored.

    Used by the bulk endpoint to validate submitted urls. Reads only -- it
    never triggers a fetch, because "is this url in a catalogue" must not be
    able to turn one POST into two multi-megabyte downloads.
    """
    from .db import load_catalogue

    out = {}
    for source_id in _sources():
        rows = load_catalogue(source_id)
        if rows:
            out[source_id] = rows
    return out


def status() -> dict:
    """What the Catalogue page shows about its own freshness and background work.

    Three things, because they answer three different questions: is a list
    being refetched right now, how old is each stored list, and how far along
    is the id resolution that runs across all of them.
    """
    from .db import catalogue_id_progress, catalogue_meta

    meta = catalogue_meta()
    with _lock:
        running = {sid: dict(info) for sid, info in _refreshing.items()}

    sources = []
    for source_id, entry in _sources().items():
        info = meta.get(source_id) or {}
        job = running.get(source_id)
        sources.append({
            "id": source_id,
            "label": entry.get("label", source_id),
            "count": info.get("count", 0),
            "fetched_at": info.get("fetched_at") or 0,
            "failed": info.get("status") == "failed",
            "last_error": info.get("last_error", ""),
            "refreshing": bool(job),
            "phase": (job or {}).get("phase", ""),
        })

    ids = catalogue_id_progress()
    ids["running"] = _ids_running()
    return {
        "sources": sources,
        "refreshing": sorted(running),
        "ids": ids,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Id resolution progress
# ─────────────────────────────────────────────────────────────────────────────
# The backfill worker itself is the next piece of work. Its progress surface
# lives here already so the page has one place to read from and does not need
# changing when the worker lands -- and so "nothing is running" is a real
# answer today rather than a missing key.
_ids_state = {"running": False, "detail": ""}


def _ids_running() -> bool:
    return bool(_ids_state.get("running"))


def set_ids_running(running: bool, detail: str = "") -> None:
    """Called by the id backfill to make itself visible on the page."""
    _ids_state["running"] = bool(running)
    _ids_state["detail"] = str(detail or "")


# ─────────────────────────────────────────────────────────────────────────────
#  Operations view
# ─────────────────────────────────────────────────────────────────────────────
def _beat():
    """Report to the Operations view. Never lets reporting break the work."""
    try:
        from .worker_registry import STATE_IDLE, STATE_WORKING, beat
        from .db import catalogue_entry_count

        with _lock:
            running = sorted(_refreshing)
        if running:
            beat("catalogue_sync", state=STATE_WORKING,
                 detail="refreshing " + ", ".join(running), error="")
        else:
            beat("catalogue_sync", state=STATE_IDLE, detail="",
                 error="", extra={"entries": catalogue_entry_count()})
    except Exception:
        pass


def refresh_stale(force=False) -> int:
    """Start a refresh for every source that needs one. Returns how many.

    Called at startup and periodically. Startup matters: it is what turns the
    first page open of the day into an instant one instead of the slow one.
    """
    from .db import catalogue_meta
    from .source_policy import source_enabled

    meta = catalogue_meta()
    started = 0
    for source_id, entry in _sources().items():
        try:
            if not source_enabled(source_id):
                continue
        except Exception:
            pass
        if force or _is_stale(source_id, meta):
            if start_refresh(source_id, entry):
                started += 1
    return started


def _refresh_loop():
    """Ask once an hour whether anything has passed :data:`CATALOGUE_TTL`.

    A timer rather than only refreshing when the page is opened: with
    stale-while-revalidate the page never waits either way, but somebody who
    opens the Catalogue once a week should not be the one who discovers the
    list is a week old. It also means the refetch happens at some quiet
    moment rather than while they are using the page.
    """
    while not _loop_stop.wait(CHECK_INTERVAL):
        try:
            started = refresh_stale()
            if started:
                logger.debug("[Catalogue] periodic check started %d refresh(es)", started)
        except Exception as exc:
            logger.debug("[Catalogue] periodic check failed: %s", exc)


def _install_unregister_hook():
    """Drop a module's stored catalogue when the module goes away.

    Registered as a hook rather than called from ``mediaforge/catalogue.py``
    directly, because that module must not import anything from
    ``mediaforge.web`` -- see its docstring.
    """
    from ..catalogue import UNREGISTER_HOOKS

    def _drop(source_id):
        try:
            from .db import drop_catalogue
            drop_catalogue(source_id)
        except Exception as exc:
            logger.debug("[Catalogue] dropping stored catalogue failed: %s", exc)

    if _drop not in UNREGISTER_HOOKS:
        UNREGISTER_HOOKS.append(_drop)


def init(refresh_on_start=True) -> None:
    """Wire the store up at app startup."""
    from .db import init_catalogue_cache_db

    init_catalogue_cache_db()
    _install_unregister_hook()
    _beat()
    if refresh_on_start:
        # Deferred: startup should not wait on the network, and a list that is
        # a day old is not worse for another twelve seconds.
        threading.Timer(12.0, lambda: refresh_stale()).start()
        _loop_stop.clear()
        threading.Thread(target=_refresh_loop, daemon=True,
                         name="catalogue-refresh-loop").start()
