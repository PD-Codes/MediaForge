"""One-off rewrite of stored MegaKino URLs onto the site's new URL scheme.

Why this exists
---------------
MegaKino moved off its JSON API onto DataLife Engine, and the URL scheme moved
with it::

    old   https://megakino.to/watch/<slug>/<24-hex-id>[?episode=N]
    new   https://megakino16.com/<category>/<id>-<slug>.html[?episode=N]

The old pages are gone -- the API answers 404 and there is no redirect. Every
MegaKino row a user already has (favourites, download queue, download history,
auto-sync jobs) therefore points at a page that no longer exists, and would
fail with a network error rather than anything a user could act on.

The two halves of the old URL are not enough to build the new one: the numeric
post id is different from the 24-hex object id, and the category segment
(``/films/``, ``/serials/``, ``/crime/``, ...) is not derivable from anything
we hold. What IS usable is the slug -- it is the title, lowercased and
hyphenated -- so each row is re-found through the site's own search and its URL
replaced with the match.

Design notes
------------
* **Never in a request, never at startup.** This needs the network, one search
  per distinct title. Doing it inside the schema-migration engine
  (``web/dbmigrate.py``) would put an unbounded network wait in front of the
  app starting, which is exactly what that engine must not do -- it runs
  before anything is serving. So it is a background pass, started after the app
  is up, the same shape as ``library_aliases.start_alias_resolver()``.
* **Idempotent and resumable.** Rows are matched by URL shape
  (``config.MEGAKINO_LEGACY_PATTERN``), so a row already rewritten is simply
  not selected again. A run that dies halfway leaves the rest for the next
  start; a run that finishes sets ``megakino_url_migration_done`` so a healthy
  installation pays one cheap SQL query per start and nothing else.
* **A title it cannot re-find is left alone.** Not deleted, not blanked: an
  unresolvable row keeps its old URL and the log names it, so the user can fix
  it by hand. Silently dropping someone's favourites to tidy up a migration
  would be much worse than leaving a dead link.
* **Never touches a non-MegaKino row.** The pattern requires both the old
  ``/watch/<slug>/<24-hex>`` shape and a host carrying the site's name.
* **Never holds the database while it waits for the network.** Old URLs are
  read first, looked up with no transaction open, and written afterwards in
  short transactions. A favourite or auto-sync job whose new URL the user has
  already re-added is the stale duplicate and is dropped instead of rewritten.
"""

import re
import threading
import time

from ..config import MEGAKINO_LEGACY_PATTERN
from ..logger import get_logger
from .db import get_db, get_setting, set_setting

logger = get_logger(__name__)

DONE_SETTING = "megakino_url_migration_done"

# (table, url column) pairs holding provider URLs. download_queue.episodes is a
# JSON list of episode URLs and is handled separately below.
_URL_COLUMNS = (
    ("favourites", "series_url"),
    ("download_queue", "series_url"),
    ("download_queue", "current_url"),
    ("download_history", "series_url"),
    ("download_history", "episode_url"),
    ("autosync_jobs", "series_url"),
)

# Pause between searches. This is somebody else's server and the pass is not
# urgent; a burst of lookups on startup is the one thing that could get an
# installation rate-limited right when the user wants to use it.
_SEARCH_DELAY = 1.5
# A run touches at most this many distinct titles, then stops and leaves the
# rest for the next start -- so a huge library cannot turn into a half-hour of
# background traffic in one go.
_MAX_TITLES_PER_RUN = 60

_started = threading.Lock()
_has_started = False

# /watch/<slug>/<24-hex-id> -- the slug is the only part carrying the title.
_SLUG_RE = re.compile(r"/watch/([^/?#]+)/[a-f0-9]{24}", re.IGNORECASE)
_EPISODE_RE = re.compile(r"[?&]episode=(\d+)", re.IGNORECASE)


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _column_exists(conn, table, column):
    return column in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def slug_to_title(url):
    """``/watch/die-schoene-und-das-biest/abc…`` -> ``die schoene und das biest``."""
    m = _SLUG_RE.search(url or "")
    if not m:
        return ""
    return m.group(1).replace("-", " ").strip()


def _norm(text):
    """Squash a title down to what two spellings of it have in common."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _best_match(title, cards):
    """The card whose title matches *title*, or None.

    Deliberately strict. A wrong match here silently repoints somebody's
    favourite at a different film, which is worse than leaving the row for the
    user to fix: an exact normalised match, else a single unambiguous prefix
    match, else nothing.
    """
    want = _norm(title)
    if not want:
        return None
    exact = [c for c in cards if _norm(c.get("title")) == want]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return exact[0]
    partial = [c for c in cards
               if _norm(c.get("title")).startswith(want) or want.startswith(_norm(c.get("title")))]
    return partial[0] if len(partial) == 1 else None


def _resolve(title, cache):
    """New base URL for *title*, or "" -- memoised per run."""
    key = _norm(title)
    if key in cache:
        return cache[key]
    cache[key] = ""
    try:
        from ..models.megakino_to import scraper
        cards = scraper.search(title)
    except Exception as e:
        logger.debug("[MegaKino] Search failed for %r: %s", title, e)
        return ""
    match = _best_match(title, cards or [])
    if match and match.get("url"):
        cache[key] = match["url"]
    return cache[key]


def _rewrite(old_url, cache):
    """New URL for one stored URL, or "" when it cannot be re-found.

    The synthetic ``?episode=N`` is carried over untouched -- it is MediaForge's
    own convention (see models/megakino_to/__init__.py), not the site's, so it
    stays valid across the move.
    """
    title = slug_to_title(old_url)
    if not title:
        return ""
    base = _resolve(title, cache)
    if not base:
        return ""
    m = _EPISODE_RE.search(old_url or "")
    return f"{base}?episode={m.group(1)}" if m else base


def pending_count(conn=None):
    """How many stored URLs still use the old scheme."""
    own = conn is None
    conn = conn or get_db()
    try:
        total = 0
        for table, column in _URL_COLUMNS:
            if not _table_exists(conn, table) or not _column_exists(conn, table, column):
                continue
            rows = conn.execute(
                "SELECT %s FROM %s WHERE %s LIKE '%%/watch/%%'" % (column, table, column)
            ).fetchall()
            total += sum(1 for r in rows if MEGAKINO_LEGACY_PATTERN.match(r[0] or ""))
        return total
    finally:
        if own:
            conn.close()


def _collect():
    """Phase 1: every stored legacy URL, read in one go (no transaction kept).

    Returns ``(columns, queue_rows)``: ``{(table, column): [old_url, ...]}`` and
    ``[(row_id, raw_episodes_json, [episode_url, ...])]`` for queue rows whose
    episode list holds legacy URLs.
    """
    import json

    conn = get_db()
    try:
        columns = {}
        for table, column in _URL_COLUMNS:
            if not _table_exists(conn, table) or not _column_exists(conn, table, column):
                continue
            rows = conn.execute(
                "SELECT DISTINCT %s FROM %s WHERE %s LIKE '%%/watch/%%'" % (column, table, column)
            ).fetchall()
            olds = [r[0] for r in rows if MEGAKINO_LEGACY_PATTERN.match(r[0] or "")]
            if olds:
                columns[(table, column)] = olds
        queue_rows = []
        if _table_exists(conn, "download_queue"):
            for row in conn.execute(
                "SELECT id, episodes FROM download_queue WHERE episodes LIKE '%/watch/%'"
            ).fetchall():
                try:
                    episodes = json.loads(row[1] or "[]")
                except (TypeError, ValueError):
                    continue
                if not isinstance(episodes, list) or not episodes:
                    continue
                if any(MEGAKINO_LEGACY_PATTERN.match(str(e or "")) for e in episodes):
                    queue_rows.append((row[0], row[1], episodes))
        return columns, queue_rows
    finally:
        conn.close()


def _resolve_all(old_urls, cache, mapping, unresolved):
    """Phase 2: look the titles up -- network only, no database access.

    Pauses after every search that actually went out (a title shared by many
    rows is searched once). Stops searching new titles once this run's budget
    is used up; titles already looked up still resolve.
    """
    budget_left = True
    for old in old_urls:
        if old in mapping or old in unresolved:
            continue
        key = _norm(slug_to_title(old))
        if key not in cache and len(cache) >= _MAX_TITLES_PER_RUN:
            if budget_left:
                logger.info(
                    "[MegaKino] Reached this run's lookup budget — the rest "
                    "is picked up on the next start")
                budget_left = False
            continue
        searched_before = len(cache)
        new = _rewrite(old, cache)
        if len(cache) > searched_before:
            time.sleep(_SEARCH_DELAY)
        if new:
            mapping[old] = new
        else:
            unresolved.add(old)


def _apply_column(conn, table, column, old, new):
    """Rewrite *old* to *new* in one column. Returns the number of rows handled.

    favourites (UNIQUE(series_url, added_by)) and autosync_jobs (UNIQUE
    series_url) refuse the new URL when the user already re-added the title
    under it -- the obvious reaction to a dead MegaKino entry. That used to
    abort the whole run with an IntegrityError, roll back every rewrite of it
    and repeat all searches on the next start, forever. The old row is then
    the stale duplicate and goes, as the s.to -> serienstream.to migration of
    autosync_jobs already does (db/autosync.py).
    """
    import sqlite3

    sql = "UPDATE %s SET %s = ? WHERE %s = ?" % (table, column, column)
    try:
        return conn.execute(sql, (new, old)).rowcount
    except sqlite3.IntegrityError:
        pass
    handled = 0
    for (rowid,) in conn.execute(
        "SELECT rowid FROM %s WHERE %s = ?" % (table, column), (old,)
    ).fetchall():
        try:
            conn.execute("UPDATE %s SET %s = ? WHERE rowid = ?" % (table, column), (new, rowid))
        except sqlite3.IntegrityError:
            conn.execute("DELETE FROM %s WHERE rowid = ?" % table, (rowid,))
            logger.info("[MegaKino] Dropped a stale %s entry for %s — it already "
                        "exists under its new URL", table, new)
        handled += 1
    return handled


def run_once():
    """One migration pass. Returns (rewritten, unresolved).

    Three phases, so the database is never locked while the network is used:
    collect the old URLs (plain reads), resolve them (searches with a pause
    in between, no transaction open), then write the result in one short
    transaction per column. The old version updated row by row inside one
    transaction that stayed open across up to 60 searches and their pauses --
    minutes in which every other writer (queue, auto-sync, settings, watch
    progress) waited and then failed with "database is locked".
    """
    import json

    columns, queue_rows = _collect()
    cache = {}
    mapping = {}
    unresolved = set()

    _resolve_all([old for olds in columns.values() for old in olds],
                 cache, mapping, unresolved)
    _resolve_all([str(e or "") for _id, _raw, eps in queue_rows for e in eps
                  if MEGAKINO_LEGACY_PATTERN.match(str(e or ""))],
                 cache, mapping, unresolved)

    rewritten = 0
    conn = get_db()
    try:
        for (table, column), olds in columns.items():
            for old in olds:
                new = mapping.get(old)
                if new:
                    rewritten += _apply_column(conn, table, column, old, new)
            conn.commit()
        # download_queue.episodes is a JSON list the queue worker walks: a row
        # is only written back when EVERY legacy entry in it resolved (a
        # partial rewrite would be worse than none), and only if the list was
        # not changed meanwhile.
        for row_id, raw, episodes in queue_rows:
            updated = []
            for entry in episodes:
                old = str(entry or "")
                if not MEGAKINO_LEGACY_PATTERN.match(old):
                    updated.append(entry)
                    continue
                new = mapping.get(old)
                if not new:
                    updated = None
                    break
                updated.append(new)
            if updated is None:
                continue
            conn.execute("UPDATE download_queue SET episodes = ? WHERE id = ? AND episodes = ?",
                         (json.dumps(updated), row_id, raw))
        conn.commit()
    finally:
        conn.close()
    return rewritten, len(unresolved)


def _worker():
    try:
        rewritten, unresolved = run_once()
    except Exception:
        logger.exception("[MegaKino] URL migration failed — retrying on the next start")
        return
    if rewritten:
        logger.info("[MegaKino] Rewrote %d stored URL(s) onto the site's new scheme", rewritten)
    if unresolved:
        logger.warning(
            "[MegaKino] %d stored URL(s) could not be re-found and were left "
            "untouched — open them once and re-add them if you still want them",
            unresolved)
    remaining = pending_count()
    if remaining:
        logger.info("[MegaKino] %d URL(s) still to migrate — continuing on the next start",
                    remaining)
        return
    try:
        set_setting(DONE_SETTING, "1")
    except Exception:
        logger.debug("[MegaKino] Could not record the migration as done", exc_info=True)


def start_megakino_url_migration():
    """Start the background pass, unless there is nothing to do.

    Called from ``web/app.py``. Cheap on a healthy installation: one settings
    read, and one COUNT-shaped query only while the flag is unset.
    """
    global _has_started

    with _started:
        if _has_started:
            return
        _has_started = True
    try:
        if (get_setting(DONE_SETTING, "0") or "0") == "1":
            return
        if not pending_count():
            set_setting(DONE_SETTING, "1")
            return
    except Exception:
        logger.debug("[MegaKino] Could not check for old URLs", exc_info=True)
        return
    threading.Thread(target=_worker, name="megakino-url-migration", daemon=True).start()
