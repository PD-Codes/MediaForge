"""Persistent browse-list cache.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import re
import threading

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


def init_browse_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS browse_cache (
                cache_key  TEXT    PRIMARY KEY,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browse_cache_cached_at "
            "ON browse_cache(cached_at)"
        )
        conn.commit()
    finally:
        conn.close()


def evict_browse_cache(ttl: float = 604800.0) -> int:
    """Delete browse entries older than *ttl* (default 7 days).

    This table had no eviction at all -- unlike tmdb_cache and provider_cache,
    which both prune at startup and hourly. Every prefetch cycle wrote new
    keys, so it only ever grew. A week is deliberately generous: the entries
    are still useful as stale-while-revalidate fallbacks long after their TTL.
    """
    import time as _time
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM browse_cache WHERE cached_at < ?",
            (_time.time() - ttl,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_browse_cache_stale(cache_key: str) -> "tuple | None":
    """Return (data_list, cached_at) regardless of TTL — for stale-while-revalidate."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM browse_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row:
            import json as _json
            return (_json.loads(row["data_json"]), row["cached_at"])
        return None
    finally:
        conn.close()


def set_browse_cache(cache_key: str, data: list) -> None:
    """Persist browse results. Upserts to refresh the timestamp."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO browse_cache (cache_key, data_json, cached_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                cached_at = excluded.cached_at
            """,
            (cache_key, _json.dumps(data, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  MediaScan cache
# ─────────────────────────────────────────────────────────────────────────────

def init_mediascan_db() -> None:
    """Create the mediascan_cache table if it does not exist yet."""
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mediascan_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id    TEXT,
                imdb_id    TEXT,
                tvdb_id    TEXT,
                title      TEXT,
                media_type TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mediascan_tmdb ON mediascan_cache (tmdb_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mediascan_imdb ON mediascan_cache (imdb_id)"
        )
        conn.commit()
    finally:
        conn.close()


def replace_mediascan_cache(entries: list) -> None:
    """
    Atomically replace the entire mediascan_cache with *entries*.
    Each entry is a dict with keys: tmdb_id, imdb_id, tvdb_id, title, media_type.
    """
    import time as _time
    now = _time.time()
    conn = get_db()
    try:
        conn.execute("DELETE FROM mediascan_cache")
        conn.executemany(
            """
            INSERT INTO mediascan_cache (tmdb_id, imdb_id, tvdb_id, title, media_type, updated_at)
            VALUES (:tmdb_id, :imdb_id, :tvdb_id, :title, :media_type, :updated_at)
            """,
            [
                {
                    "tmdb_id":    str(e.get("tmdb_id") or "").strip() or None,
                    "imdb_id":    str(e.get("imdb_id") or "").strip() or None,
                    "tvdb_id":    str(e.get("tvdb_id") or "").strip() or None,
                    "title":      str(e.get("title") or "").strip() or None,
                    "media_type": str(e.get("media_type") or "").strip() or None,
                    "updated_at": now,
                }
                for e in entries
            ],
        )
        conn.commit()
    finally:
        conn.close()


# get_mediascan_ids() is requested on every browse page (several times per
# classic home page) and normalised 10k titles with five regexes each time --
# ~200 ms per call -- for data that only changes when a MediaScan run
# finishes. Kept until the row count or the newest updated_at changes.
_MEDIASCAN_IDS_MEMO = {"key": None, "value": None}
_MEDIASCAN_IDS_LOCK = threading.Lock()
_MS_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")
_MS_SEASON_RE = re.compile(r"\s*:?\s*season\s+\d+\s*$")
_MS_STAFFEL_RE = re.compile(r"\s*:?\s*staffel\s+\d+\s*$")
_MS_PART_RE = re.compile(r"\s*:?\s*part\s+\d+\s*$")
_MS_PUNCT_RE = re.compile(r"[^\w\s]")


def _mediascan_norm_title(t):
    """lowercase, strip year/season suffixes and punctuation, collapse whitespace"""
    t = (t or "").lower()
    t = _MS_YEAR_RE.sub("", t)        # (2013)
    t = _MS_SEASON_RE.sub("", t)
    t = _MS_STAFFEL_RE.sub("", t)
    t = _MS_PART_RE.sub("", t)
    t = _MS_PUNCT_RE.sub("", t)       # strip punctuation
    return " ".join(t.split())


def get_mediascan_ids() -> dict:
    """Return sets of tmdb_ids, imdb_ids and normalised titles from the mediascan cache."""
    conn = get_db()
    try:
        head = conn.execute(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS ts FROM mediascan_cache"
        ).fetchone()
        key = (head["n"], head["ts"]) if head else None
        with _MEDIASCAN_IDS_LOCK:
            if key is not None and _MEDIASCAN_IDS_MEMO["key"] == key:
                cached = _MEDIASCAN_IDS_MEMO["value"]
                # Fresh lists per call, so a caller can never change the memo.
                return {k: list(v) for k, v in cached.items()}
        rows = conn.execute(
            "SELECT tmdb_id, imdb_id, title FROM mediascan_cache"
        ).fetchall()
        tmdb_ids = {r["tmdb_id"] for r in rows if r["tmdb_id"]}
        imdb_ids = {r["imdb_id"] for r in rows if r["imdb_id"]}
        # Normalise titles for fuzzy front-end matching:
        # lowercase, strip year/season suffixes, collapse whitespace
        titles = {_mediascan_norm_title(r["title"]) for r in rows if r["title"]}
        titles.discard("")  # remove empty strings
        value = {"tmdb_ids": list(tmdb_ids), "imdb_ids": list(imdb_ids), "titles": list(titles)}
        with _MEDIASCAN_IDS_LOCK:
            _MEDIASCAN_IDS_MEMO["key"] = key
            _MEDIASCAN_IDS_MEMO["value"] = value
        return {k: list(v) for k, v in value.items()}
    finally:
        conn.close()


def get_mediascan_ids_by_type(media_type: str) -> set:
    """Same tmdb_id set get_mediascan_ids() returns, but scoped to one
    media_type ('movie' or 'tv') via mediascan_cache's own media_type
    column -- get_mediascan_ids() intentionally merges both into one flat
    set for its existing callers (front-end "is this downloaded" checks,
    where the type is already known from context), but a caller that needs
    to look up TMDB detail by id (movie ids and tv ids are separate TMDB
    namespaces, an id can coincidentally exist in both) needs the type-
    scoped version to avoid querying the wrong endpoint for every id --
    see web/thirdparties/mediacalendar/service.py's _resolve_library()."""
    # MediaScan stores series as 'show' (Jellyfin "Series" and the Plex
    # section type both map to it), while callers ask in TMDB's vocabulary
    # ('tv'). Both spellings are accepted either way.
    series_types = ("show", "tv")
    types = series_types if media_type in series_types else (media_type,)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tmdb_id FROM mediascan_cache WHERE media_type IN (%s)"
            " AND tmdb_id IS NOT NULL AND tmdb_id != ''" % ",".join("?" for _ in types),
            types,
        ).fetchall()
        return {r["tmdb_id"] for r in rows}
    finally:
        conn.close()


def get_mediascan_count() -> int:
    """Return the number of entries in the mediascan cache."""
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM mediascan_cache").fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def get_mediascan_last_updated() -> "float | None":
    """Return the most recent updated_at timestamp, or None if cache is empty."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS ts FROM mediascan_cache"
        ).fetchone()
        return row["ts"] if row and row["ts"] else None
    finally:
        conn.close()


def clear_mediascan_cache() -> None:
    """Wipe all mediascan entries."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM mediascan_cache")
        conn.commit()
    finally:
        conn.close()


def get_mediascan_series() -> list:
    """Return all series from mediascan_cache as a list of dicts with tmdb_id, title, imdb_id."""
    conn = get_db()
    try:
        # 'show' is what MediaScan writes; filtering on 'tv' alone matched
        # nothing, so the calendar's "media library" overlay was always empty.
        rows = conn.execute(
            "SELECT tmdb_id, imdb_id, title FROM mediascan_cache WHERE media_type IN ('show', 'tv')"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
