"""Persistent full-catalogue store.

The Catalogue page's A-Z lists used to live in a process-local dict in
``mediaforge/catalogue.py``, which meant three things: every restart threw
away ~13k rows and the next page open waited on two multi-megabyte downloads
from sites behind DDoS-Guard; nothing else in the app could read the lists;
and there was nowhere to put the TMDB/IMDb ids that turn "does this title
match a folder name" into an exact answer.

So the lists live here instead. Two tables:

* ``catalogue_entries`` -- one row per title per source, keyed by
  ``(source_id, url)`` because a title both sites carry is deliberately two
  entries, not one (different pages, different languages, different episode
  counts).
* ``catalogue_meta`` -- when each source was last fetched, how many rows it
  produced, and whether the last attempt worked. Separate from the entries so
  a FAILED refresh can be recorded without touching the rows that are still
  perfectly usable -- which is the whole point of serving stale data while
  revalidating.

``tmdb_id`` / ``imdb_id`` / ``ids_checked_at`` are created empty. They are
filled in later, lazily and by a throttled backfill; ``ids_checked_at`` exists
so a title TMDB genuinely has nothing for is not looked up again on every
pass.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up.
"""

import time as _time

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


def init_catalogue_cache_db() -> None:
    """Create the catalogue tables. Safe to call repeatedly."""
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS catalogue_entries (
                source_id      TEXT NOT NULL,
                url            TEXT NOT NULL,
                title          TEXT NOT NULL,
                alt            TEXT NOT NULL DEFAULT '',
                tmdb_id        TEXT,
                imdb_id        TEXT,
                ids_checked_at REAL,
                PRIMARY KEY (source_id, url)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalogue_entries_source "
            "ON catalogue_entries(source_id)"
        )
        # For the id backfill: "give me rows that have never been looked up".
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalogue_entries_ids "
            "ON catalogue_entries(ids_checked_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS catalogue_meta (
                source_id  TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                count      INTEGER NOT NULL DEFAULT 0,
                status     TEXT NOT NULL DEFAULT 'ok',
                last_error TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_catalogue(source_id: str, entries: list) -> int:
    """Replace one source's entries with *entries*, in one transaction.

    Replace rather than upsert-and-prune: a catalogue is a snapshot of a page,
    a title that vanished from it is gone, and doing it in halves would leave
    the table holding two different snapshots at once.

    The ids are deliberately NOT wiped along with the rows. A url that is
    still in the list keeps whatever was resolved for it -- re-resolving
    thousands of titles because a site added five is exactly the cost this
    table exists to avoid.
    """
    now = _time.time()
    rows = []
    for entry in entries or []:
        url = str(entry.get("url") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not url or not title:
            continue
        rows.append({
            "source_id": source_id,
            "url": url,
            "title": title,
            "alt": str(entry.get("alt") or ""),
        })

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Carry the resolved ids of urls that survive this refresh across the
        # delete. A temp table rather than a Python round-trip: 13k rows.
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _cat_keep "
            "(url TEXT PRIMARY KEY, tmdb_id TEXT, imdb_id TEXT, ids_checked_at REAL)"
        )
        conn.execute("DELETE FROM _cat_keep")
        conn.execute(
            "INSERT INTO _cat_keep (url, tmdb_id, imdb_id, ids_checked_at) "
            "SELECT url, tmdb_id, imdb_id, ids_checked_at FROM catalogue_entries "
            "WHERE source_id = ? AND ids_checked_at IS NOT NULL",
            (source_id,),
        )
        conn.execute("DELETE FROM catalogue_entries WHERE source_id = ?", (source_id,))
        conn.executemany(
            "INSERT INTO catalogue_entries (source_id, url, title, alt) "
            "VALUES (:source_id, :url, :title, :alt)",
            rows,
        )
        conn.execute(
            "UPDATE catalogue_entries SET "
            "  tmdb_id        = (SELECT k.tmdb_id        FROM _cat_keep k WHERE k.url = catalogue_entries.url), "
            "  imdb_id        = (SELECT k.imdb_id        FROM _cat_keep k WHERE k.url = catalogue_entries.url), "
            "  ids_checked_at = (SELECT k.ids_checked_at FROM _cat_keep k WHERE k.url = catalogue_entries.url) "
            "WHERE source_id = ? AND url IN (SELECT url FROM _cat_keep)",
            (source_id,),
        )
        conn.execute("DELETE FROM _cat_keep")
        conn.execute(
            """
            INSERT INTO catalogue_meta (source_id, fetched_at, count, status, last_error)
            VALUES (?, ?, ?, 'ok', NULL)
            ON CONFLICT(source_id) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                count      = excluded.count,
                status     = 'ok',
                last_error = NULL
            """,
            (source_id, now, len(rows)),
        )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_catalogue_failed(source_id: str, error: str) -> None:
    """Record that a refresh failed, WITHOUT touching the stored entries.

    The rows from the last good fetch stay exactly as they are and keep being
    served. What changes is only what the page is told: "this list is from
    yesterday and today's refresh did not work", which is a different message
    from "the catalogue is unavailable" and calls for a different reaction.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT fetched_at, count FROM catalogue_meta WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO catalogue_meta (source_id, fetched_at, count, status, last_error)
            VALUES (?, ?, ?, 'failed', ?)
            ON CONFLICT(source_id) DO UPDATE SET
                status     = 'failed',
                last_error = excluded.last_error
            """,
            (source_id,
             row["fetched_at"] if row else 0.0,
             row["count"] if row else 0,
             str(error or "")[:300]),
        )
        conn.commit()
    finally:
        conn.close()


def load_catalogue(source_id: str) -> list:
    """Every stored entry for *source_id*, already sorted by title.

    Sorted in SQL so the page does not sort 13k rows on every load, and by
    ``title COLLATE NOCASE`` so the order matches the case-insensitive one the
    client used to produce itself.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT title, url, alt, tmdb_id, imdb_id FROM catalogue_entries "
            "WHERE source_id = ? ORDER BY title COLLATE NOCASE, url",
            (source_id,),
        ).fetchall()
        return [
            {"title": r["title"], "url": r["url"], "alt": r["alt"] or "",
             "tmdb_id": r["tmdb_id"] or "", "imdb_id": r["imdb_id"] or ""}
            for r in rows
        ]
    finally:
        conn.close()


def catalogue_meta(source_id: "str | None" = None) -> dict:
    """``{source_id: {fetched_at, count, status, last_error}}``.

    One query for every source: the page asks for this before it asks for any
    list, and doing it per source turned a single page open into one query per
    installed provider.
    """
    conn = get_db()
    try:
        if source_id:
            rows = conn.execute(
                "SELECT * FROM catalogue_meta WHERE source_id = ?", (source_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM catalogue_meta").fetchall()
        return {
            r["source_id"]: {
                "fetched_at": r["fetched_at"],
                "count": r["count"],
                "status": r["status"],
                "last_error": r["last_error"] or "",
            }
            for r in rows
        }
    finally:
        conn.close()


def drop_catalogue(source_id: str) -> None:
    """Forget one source entirely -- used when a module is uninstalled."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM catalogue_entries WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM catalogue_meta WHERE source_id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()


def evict_catalogue_cache(known_source_ids) -> int:
    """Delete rows belonging to sources that no longer exist.

    A module that is uninstalled goes through :func:`drop_catalogue`, but a
    module removed while the app was not running never gets the chance -- and
    an orphaned catalogue is 10k rows nothing will ever read again. Called
    from the periodic cache eviction, same as the other cache tables.
    """
    known = {str(s) for s in (known_source_ids or [])}
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_id FROM catalogue_meta"
        ).fetchall()
        orphans = [r["source_id"] for r in rows if r["source_id"] not in known]
        if not orphans:
            return 0
        marks = ",".join("?" for _ in orphans)
        conn.execute(
            "DELETE FROM catalogue_entries WHERE source_id IN (%s)" % marks, orphans)
        conn.execute(
            "DELETE FROM catalogue_meta WHERE source_id IN (%s)" % marks, orphans)
        conn.commit()
        logger.info("[Catalogue] evicted orphaned catalogues: %s", ", ".join(orphans))
        return len(orphans)
    finally:
        conn.close()


def catalogue_entry_count() -> int:
    """Total rows across every source -- for the Operations card."""
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM catalogue_entries").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  External ids
# ─────────────────────────────────────────────────────────────────────────────
# Not filled by anything yet. The columns and these two helpers exist now
# because adding them later would mean a migration on a table with ~13k rows
# per source, and because the shape of the backfill is what decides the shape
# of the schema: it needs "rows nobody has looked up yet", which is an index,
# not a scan.

def set_catalogue_ids(source_id: str, url: str,
                      tmdb_id: str = "", imdb_id: str = "") -> None:
    """Record what an id lookup found for one entry.

    A lookup that found NOTHING still stamps ``ids_checked_at``: without it a
    title TMDB has no record of would be retried on every single pass, which
    for a catalogue this size is most of the passes.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE catalogue_entries SET tmdb_id = ?, imdb_id = ?, ids_checked_at = ? "
            "WHERE source_id = ? AND url = ?",
            (str(tmdb_id or "") or None, str(imdb_id or "") or None,
             _time.time(), source_id, url),
        )
        conn.commit()
    finally:
        conn.close()


def set_catalogue_ids_bulk(rows) -> int:
    """Store many id lookups in ONE transaction.

    The backfill resolves titles in parallel, so without this it would be one
    connection-open-commit-close per title -- thirteen thousand of them, each
    contending with whatever the UI is doing. *rows* is an iterable of
    ``(source_id, url, tmdb_id, imdb_id)``; an empty id pair is a real result
    and still stamps ``ids_checked_at`` (see :func:`set_catalogue_ids`).
    """
    now = _time.time()
    payload = [
        {"source_id": source_id, "url": url,
         "tmdb_id": str(tmdb_id or "") or None,
         "imdb_id": str(imdb_id or "") or None,
         "checked": now}
        for source_id, url, tmdb_id, imdb_id in rows
    ]
    if not payload:
        return 0
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "UPDATE catalogue_entries SET tmdb_id = :tmdb_id, imdb_id = :imdb_id, "
            "ids_checked_at = :checked WHERE source_id = :source_id AND url = :url",
            payload,
        )
        conn.commit()
        return len(payload)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def catalogue_id_progress() -> dict:
    """``{total, resolved, checked}`` across every source.

    What the Catalogue page shows while the backfill is working. ``checked``
    counts every row that has been looked up, ``resolved`` only the ones that
    came back with an id -- reporting them as one number would make a
    catalogue full of obscure titles look stuck at 40% forever when it is in
    fact finished.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN ids_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS checked, "
            "       SUM(CASE WHEN tmdb_id IS NOT NULL AND tmdb_id != '' THEN 1 ELSE 0 END) AS resolved "
            "FROM catalogue_entries"
        ).fetchone()
        if not row:
            return {"total": 0, "checked": 0, "resolved": 0}
        return {"total": row["total"] or 0,
                "checked": row["checked"] or 0,
                "resolved": row["resolved"] or 0}
    finally:
        conn.close()


def catalogue_entries_without_ids(limit: int = 50, retry_after: float = 0.0) -> list:
    """Entries the id backfill has not resolved yet, oldest attempt first.

    *retry_after* (seconds) lets a caller re-examine rows whose lookup came
    back empty a long time ago -- TMDB does gain entries for obscure titles.
    Zero means "never retry a checked row", which is the safe default.
    """
    conn = get_db()
    try:
        if retry_after > 0:
            rows = conn.execute(
                "SELECT source_id, url, title FROM catalogue_entries "
                "WHERE ids_checked_at IS NULL "
                "   OR (tmdb_id IS NULL AND ids_checked_at < ?) "
                "ORDER BY ids_checked_at IS NOT NULL, ids_checked_at LIMIT ?",
                (_time.time() - retry_after, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_id, url, title FROM catalogue_entries "
                "WHERE ids_checked_at IS NULL LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
