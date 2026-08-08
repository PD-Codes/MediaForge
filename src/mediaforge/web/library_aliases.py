"""Alternative names for the folders in the library.

Why this exists
---------------
"Is this title already downloaded?" was answered by comparing the provider's
spelling of a title against the folder names on disk. That comparison can be
made punctuation-insensitive and bidirectional (see
``models/common/common.py``'s ``titles_match``), and after that it still cannot
answer the case that actually matters: the folder is named after whichever
provider downloaded it FIRST, and a different provider may call the same show
something else entirely. "Horizon in the Middle of Nowhere" and
"Kyoukaisen-jou no Horizon" share not one character, and no amount of string
cleverness will connect them.

So the connection is looked up instead of guessed. TMDB knows a show's
localized title, its original-language title and its list of alternative
titles; resolving a folder once gives every name it may legitimately arrive
under, and those names are stored next to the folder (``library_aliases``,
see ``db/library.py``).

Design notes
------------
* **Persisted, not cached.** ``tmdb_cache`` has a 24 h TTL and is keyed by the
  asking title -- fine for "what is the rating of this card", useless here.
  Which names a folder answers to is a fact about the folder; it is written
  once and pruned when the folder disappears.

* **Resolved in the background, never in a request.** A first run on a large
  library is one TMDB lookup per folder. Doing that inside
  ``/api/downloaded-folders`` would turn the home page into a two-minute wait
  the first time. The route serves whatever is already resolved and this module
  catches up behind it; until it does, the ordinary string match is what
  answers, exactly as before.

* **Cheap when there is nothing to do.** A folder is looked up once. A pass
  over a fully resolved library is one SQL query and no network at all.

* **No API key, no work.** CineInfo/TMDB is optional; without a key this module
  does nothing and reports nothing, and the string match remains the whole
  answer.
"""

from __future__ import annotations

import threading
import time

from ..logger import get_logger
from .db import (
    get_library_aliases,
    get_setting,
    prune_library_aliases,
    set_library_aliases,
)

logger = get_logger(__name__)

# How often the background pass runs. Slow on purpose: everything it does is
# permanent, so there is nothing to keep fresh -- the interval only decides how
# quickly a newly downloaded folder gains its aliases.
RESOLVE_INTERVAL = 900.0

# How many folders one pass resolves. A first run on a large library is
# deliberately spread over several passes rather than firing hundreds of TMDB
# lookups back to back: the shared rate limiter would let it through, and the
# same limiter is what the pages a user is actually looking at depend on.
RESOLVE_BATCH = 25

# A folder TMDB had nothing for is re-checked after this long -- a show that was
# not in TMDB last month may be now, and a scrape-mangled folder name may have
# been renamed since. Without it a miss would be permanent.
RETRY_MISS_AFTER = 30 * 86400.0

_stop = threading.Event()
_started = False
_start_lock = threading.Lock()


def _downloaded_folder_names() -> list:
    """Every folder name in every scan root, deduplicated.

    Deliberately the same walk /api/downloaded-folders does, imported from
    there rather than reimplemented -- two answers to "what is on disk" is how
    the alias table would end up describing folders the badge check never sees.
    """
    from .routes.browse import downloaded_folder_names
    return downloaded_folder_names()


def resolve_pending(limit=RESOLVE_BATCH) -> int:
    """Resolve aliases for up to *limit* folders that do not have them yet.

    Returns how many folders were resolved. Zero means either "nothing left to
    do" or "no TMDB key", and both are ordinary.
    """
    api_key = (get_setting("cineinfo_tmdb_api_key", "") or "").strip()
    if not api_key:
        return 0

    try:
        folders = _downloaded_folder_names()
    except Exception:
        logger.debug("[Aliases] Could not list library folders", exc_info=True)
        return 0
    if not folders:
        return 0

    known = get_library_aliases()
    # Drop rows for folders that are gone, so a deleted folder stops claiming
    # its names. Done here rather than on a schedule of its own: this is the
    # only place that already knows both sides.
    try:
        removed = prune_library_aliases(folders)
        if removed:
            logger.info("[Aliases] Pruned %d row(s) for folders no longer on disk", removed)
    except Exception:
        logger.debug("[Aliases] Prune failed", exc_info=True)

    now = time.time()
    todo = []
    for name in folders:
        row = known.get(str(name).strip().lower())
        if row is None:
            todo.append(name)
        elif not row["aliases"] and (now - (row["resolved_at"] or 0)) > RETRY_MISS_AFTER:
            # Previously a miss, and long enough ago to be worth asking again.
            todo.append(name)
        if len(todo) >= limit:
            break
    if not todo:
        return 0

    country = get_setting("cineinfo_country", "DE") or "DE"
    from .tmdb_cache import _tmdb_lookup_cached, all_titles

    done = 0
    for name in todo:
        if _stop.is_set():
            break
        try:
            # ui_lang is fixed to "de" here rather than taken from a session:
            # this runs on a background thread with no request, and the alias
            # SET is what matters, not which of its members is "the" title.
            # alternative_titles is language-independent anyway.
            data = _tmdb_lookup_cached(name, None, api_key, country, "de") or {}
            if not data.get("found"):
                set_library_aliases(name, [], "", "")
                done += 1
                continue
            # Only a confident match may contribute aliases. A fuzzy hit would
            # hand this folder the names of a DIFFERENT show, and that turns
            # into a wrong "already downloaded" -- which silently stops a
            # download the user asked for, the one failure mode worse than the
            # bug being fixed here.
            if not data.get("title_confident"):
                logger.debug("[Aliases] %r: TMDB match not confident, no aliases stored", name)
                set_library_aliases(name, [], "", "")
                done += 1
                continue
            titles = data.get("titles") or all_titles(data.get("raw_details"))
            set_library_aliases(name, titles,
                                str(data.get("tmdb_id") or ""),
                                str(data.get("media_type") or ""))
            done += 1
        except Exception:
            logger.debug("[Aliases] Lookup failed for %r", name, exc_info=True)
    if done:
        logger.info("[Aliases] Resolved alternative names for %d folder(s)", done)
    return done


def alias_index() -> dict:
    """``{loose_key: folder}`` for every alias of every resolved folder.

    The key is ``models.common.common.loose_title_key`` of the alias, so a
    caller can test a provider's title with one dict lookup instead of walking
    the table. The value is the folder the alias belongs to, which is what the
    download dialog needs in order to say WHERE the title is stored.

    The folder's own name is included as well, so this index alone answers the
    exact-match case too and a caller does not have to combine two mechanisms.
    """
    from ..models.common.common import loose_title_key

    out = {}
    for folder, row in get_library_aliases().items():
        for name in [folder] + list(row.get("aliases") or []):
            key = loose_title_key(name)
            # First writer wins: two folders legitimately sharing an alternative
            # title is possible (a remake), and quietly reassigning the key on
            # every pass would make the answer depend on dict order.
            if key and key not in out:
                out[key] = folder
    return out


def folder_holds_title(folder_name, title, index=None) -> bool:
    """Does *folder_name* hold *title*, aliases included?

    The drop-in replacement for a bare ``titles_match(folder.name, title)`` in
    a folder-scanning loop: the string comparison is tried first (it needs no
    table and works without a TMDB key), and the alias index answers the case
    string comparison cannot -- a provider that calls the show by a different
    name entirely.

    Pass *index* when scanning many folders; building it per folder is a query
    per folder.
    """
    from ..models.common.common import loose_title_key, titles_match

    if titles_match(folder_name, title):
        return True
    key = loose_title_key(title)
    if not key:
        return False
    idx = index if index is not None else alias_index()
    return idx.get(key, "") == str(folder_name or "").strip().lower()


def folder_for_title(title, index=None):
    """The folder holding *title* by alias, or "" if none does.

    *index* lets a caller that checks many titles build :func:`alias_index`
    once instead of per title.
    """
    from ..models.common.common import loose_title_key

    key = loose_title_key(title)
    if not key:
        return ""
    idx = index if index is not None else alias_index()
    return idx.get(key, "")


def start_alias_resolver() -> None:
    """Start the background resolver. Idempotent.

    Same shape as web/worker_watchdog.py's start(): named daemon thread, a stop
    Event so the wait is interruptible, an idempotence flag under a lock, and
    per-pass exception isolation.
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        _stop.clear()

    def _loop():
        # One short delay before the first pass so this never competes with
        # startup: the library scan and the first page load both want the disk
        # and the network more than this does.
        if _stop.wait(60.0):
            return
        while True:
            try:
                resolve_pending()
            except Exception:
                logger.exception("[Aliases] Resolver pass failed")
            if _stop.wait(RESOLVE_INTERVAL):
                return

    threading.Thread(target=_loop, daemon=True, name="library-alias-resolver").start()
    logger.info("[Aliases] Alias resolver started (every %.0fs, %d folder(s) per pass)",
                RESOLVE_INTERVAL, RESOLVE_BATCH)


def stop_alias_resolver() -> None:
    """Stop the resolver. Used by tests; the thread is a daemon otherwise."""
    global _started
    _stop.set()
    with _start_lock:
        _started = False
