"""Resolve catalogue titles to TMDB/IMDb ids, slowly and in the background.

Why this exists
---------------
"Do I already have this?" is answered on the Catalogue page the same way the
start page answers it: by normalising the title and comparing it against the
download folders, or against the Plex/Jellyfin index. That works most of the
time and fails in exactly the cases that matter -- remakes, two shows with the
same name, a library that spells a title differently from the source site.

An id turns that guess into a fact. The library index (mediascan) already
carries TMDB and IMDb ids for everything in it; the catalogue side did not,
which is what this fills in.

Why it runs in the background
-----------------------------
There are ~13k catalogue entries and each one is a TMDB lookup, so this cannot
happen when a page opens. It resolves a handful at a time (see
:data:`LOOKUP_WORKERS`), writes each batch in one transaction, and picks up
where it left off after a restart because the progress lives in the database.
A full catalogue takes well under an hour.

What it deliberately does NOT do is use the whole request budget. The same
rate limit serves the modal a user is looking at right now, and a backfill
that makes the app feel slow is a backfill nobody wants running.

Why a wrong id is worse than no id
----------------------------------
TMDB's /search/multi answers almost every query with *something*. Storing that
answer unconditionally would mean confidently matching "Demon's Ascension"
against whatever came back first -- an exact match to the wrong show, which is
worse than the fuzzy title matching it replaces, and invisible when it happens.
So every lookup goes through ``lookup_media(require_confident=True)``: a result
whose title does not actually correspond to the one asked for is thrown away,
and the entry is recorded as *checked with no id* so the fuzzy path keeps
handling it.

Nothing here is reported as an application error. A title TMDB has never heard
of is a normal outcome, and an unreachable TMDB is an outage somewhere else.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from ..logger import get_logger

logger = get_logger(__name__)

# How many titles are looked up at once. The bottleneck is the round trip to
# TMDB, not TMDB's own limit -- one at a time meant a title every two to three
# seconds and a full catalogue taking most of a day. Four in parallel is a few
# per second, and a 13k-entry catalogue lands in well under an hour.
#
# Four and not forty: `lookup_media` spends the process-wide budget of 40 req/s
# that the UI shares, and one entry is several requests (search, details,
# providers, videos). Four workers use maybe a third of that, which leaves the
# modal a user is actually looking at answering immediately -- and a backfill
# that makes the app feel slow is a backfill nobody wants running.
LOOKUP_WORKERS = 4

# Breather between two lookups WITHIN one worker. Small; it exists so a
# catalogue that is entirely TMDB-cache hits does not turn into a tight loop.
LOOKUP_DELAY = 0.15

# Rows per batch: resolved in parallel, then written in ONE transaction. Small
# enough that the progress on the page moves every few seconds and that a
# catalogue refresh landing mid-pass is noticed quickly.
BATCH = 24

# When there is nothing left to resolve, look again after this long -- a
# catalogue refresh adds new titles while the app is running.
IDLE_SLEEP = 15 * 60

# How often to re-check for a TMDB key when there is none. Much shorter than
# IDLE_SLEEP: this is the state a fresh install sits in.
UNCONFIGURED_SLEEP = 60

# A row that was checked and came back empty is retried this much later. TMDB
# does gain entries for obscure titles, but not often enough to be worth
# asking again every pass.
RECHECK_AFTER = 30 * 24 * 3600

_thread = None
_stop = threading.Event()
# Interrupts an idle wait without stopping the worker. Set by wake(), which is
# what makes "Update now" and a finished catalogue refresh take effect at once
# instead of whenever the next IDLE_SLEEP happens to expire.
_kick = threading.Event()


def _idle(seconds) -> bool:
    """Wait up to *seconds*. Returns True when the worker should stop.

    An interruptible sleep rather than a plain one: with a fifteen-minute idle
    period, a user who presses Update and then watches nothing happen for a
    quarter of an hour has every reason to think the feature is broken.
    """
    _kick.wait(timeout=seconds)
    _kick.clear()
    return _stop.is_set()


def wake() -> None:
    """Ask the worker to look for work right now.

    Called when a catalogue refresh has just stored new entries, and from the
    Catalogue page's refresh button. Safe to call when the worker is busy or
    not running -- it only clears an idle wait.
    """
    _kick.set()


def _imdb_from(info) -> str:
    """IMDb id out of a TMDB result, wherever this endpoint happened to put it.

    Movies carry ``imdb_id`` on the details object; shows only have it under
    ``external_ids``, and only when that was appended to the request. Both are
    optional -- the TMDB id alone is already enough to match against the
    library index.
    """
    details = (info or {}).get("raw_details") or {}
    direct = details.get("imdb_id")
    if direct:
        return str(direct)
    external = details.get("external_ids") or {}
    return str(external.get("imdb_id") or "")


def _lookup(title, api_key=None, country=None, ui_lang=None) -> dict:
    """One TMDB lookup. ``{"error": True}`` when TMDB itself was unreachable,
    which is a different outcome from "no match" and must not be recorded as
    one -- see the note about not stamping the row below."""
    from .tmdb_cache import lookup_media

    try:
        info = lookup_media(title, require_confident=True, api_key=api_key,
                            country=country, ui_lang=ui_lang)
    except Exception as exc:
        logger.debug("[CatalogueIds] %s failed: %s: %s", title, type(exc).__name__, exc)
        return {"tmdb_id": "", "imdb_id": "", "error": True}
    if not info:
        return {"tmdb_id": "", "imdb_id": ""}
    return {"tmdb_id": str(info.get("tmdb_id") or ""), "imdb_id": _imdb_from(info)}


def resolve_entry(source_id, url, title, api_key=None) -> dict:
    """Resolve one entry and store it. Returns ``{tmdb_id, imdb_id}``.

    The single-entry path, used by the lazy resolution a details modal
    triggers. Always writes, including when nothing was found: the empty write
    is what stamps ``ids_checked_at`` and stops the entry being looked up again
    on every pass. For a catalogue with a long tail of obscure titles that is
    most of the work saved.

    A TMDB OUTAGE is the exception -- it writes nothing, because leaving the
    row unchecked is exactly what makes the next pass try again.
    """
    from .db import set_catalogue_ids

    result = _lookup(title, api_key=api_key)
    if result.get("error"):
        return result
    set_catalogue_ids(source_id, url,
                      tmdb_id=result["tmdb_id"], imdb_id=result["imdb_id"])
    return result


def _beat(state, detail="", error="", handled=None):
    try:
        from .worker_registry import beat
        extra = {"entries": handled} if handled is not None else None
        beat("catalogue_ids", state=state, detail=detail, error=error, extra=extra)
    except Exception:
        pass


def _run():
    from .catalogue_store import set_ids_running
    from .db import (catalogue_entries_without_ids, catalogue_id_progress,
                     get_setting, set_catalogue_ids_bulk)
    from .tmdb_cache import is_tmdb_configured
    from .worker_registry import STATE_IDLE, STATE_WORKING

    while not _stop.is_set():
        # No key, nothing to do. Checked every cycle rather than once at start:
        # a user configuring TMDB should not have to restart the app for this.
        if not is_tmdb_configured():
            set_ids_running(False)
            _beat(STATE_IDLE, detail="TMDB not configured")
            # Short poll, not IDLE_SLEEP: this is the state a fresh install
            # sits in, and someone who has just pasted their key in Settings
            # should see the backfill start, not wait a quarter of an hour
            # wondering whether it is broken.
            if _idle(UNCONFIGURED_SLEEP):
                break
            continue

        api_key = (get_setting("cineinfo_tmdb_api_key", "") or "").strip()
        todo = catalogue_entries_without_ids(BATCH, retry_after=RECHECK_AFTER)
        if not todo:
            set_ids_running(False)
            progress = catalogue_id_progress()
            _beat(STATE_IDLE,
                  detail="%d/%d resolved" % (progress["resolved"], progress["total"]),
                  handled=progress["checked"])
            if _idle(IDLE_SLEEP):
                break
            continue

        # Visible on the Catalogue page for as long as this runs -- a job that
        # takes an hour and says nothing is a job the user assumes is broken.
        set_ids_running(True)

        # Resolved once per batch rather than once per title: lookup_media()
        # reads both from the settings table when they are not passed, and
        # that is a database round-trip per title for a value that does not
        # change.
        country = get_setting("cineinfo_country", "DE")

        errors = 0
        results = []

        def _one(row):
            if _stop.is_set():
                return None
            result = _lookup(row["title"], api_key=api_key, country=country,
                             ui_lang="de")
            # Spread the requests out a little; see LOOKUP_DELAY.
            _stop.wait(LOOKUP_DELAY)
            return (row, result)

        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS,
                                thread_name_prefix="catalogue-ids") as pool:
            for outcome in pool.map(_one, todo):
                if outcome is None:
                    continue
                row, result = outcome
                if result.get("error"):
                    errors += 1
                    continue      # unchecked on purpose: retried next pass
                results.append((row["source_id"], row["url"],
                                result["tmdb_id"], result["imdb_id"]))

        # One transaction for the whole batch instead of a commit per title.
        if results:
            set_catalogue_ids_bulk(results)

        if errors >= LOOKUP_WORKERS:
            # TMDB is unhappy, not just this title. Back off rather than
            # burning through the remaining twelve thousand entries failing.
            logger.info("[CatalogueIds] backing off after %d failures", errors)

        progress = catalogue_id_progress()
        _beat(STATE_WORKING,
              detail="%d/%d checked" % (progress["checked"], progress["total"]),
              handled=progress["checked"])
        if errors >= LOOKUP_WORKERS and _idle(IDLE_SLEEP):
            break

    set_ids_running(False)
    _beat(STATE_IDLE, detail="stopped")


def start() -> bool:
    """Start the backfill thread. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        # Already running: at least make it look for work now, which is what
        # a caller asking to "start" it actually wants.
        wake()
        return False
    _stop.clear()
    _kick.clear()
    _thread = threading.Thread(target=_run, daemon=True, name="catalogue-ids")
    _thread.start()
    logger.info("[CatalogueIds] backfill worker started")
    return True


def stop() -> None:
    _stop.set()
    _kick.set()      # so an idle wait returns immediately instead of at timeout
