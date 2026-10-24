"""TMDB/CineInfo and provider-availability result caches.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import threading
from collections import OrderedDict

from ...logger import get_logger

from ._core import _sql_chunks, get_db

logger = get_logger(__name__)


def init_tmdb_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tmdb_cache (
                cache_key  TEXT    PRIMARY KEY,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL
            )
            """
        )
        # Eviction below and the hourly one in app.py filter on cached_at,
        # which is not the primary key -- without this index both are a full
        # table scan, at startup and once an hour.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tmdb_cache_cached_at "
            "ON tmdb_cache(cached_at)"
        )
        # Remove expired entries so the table does not grow unboundedly
        conn.execute(
            "DELETE FROM tmdb_cache WHERE cached_at < strftime('%s', 'now') - 86400"
        )
        conn.commit()
    finally:
        conn.close()


def get_tmdb_cache(cache_key: str, ttl: float = 86400.0) -> "dict | None":
    """Return cached TMDB data if it exists and is within TTL, else None."""
    import time as _time
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM tmdb_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row and (_time.time() - row["cached_at"]) < ttl:
            import json as _json
            return _json.loads(row["data_json"])
        return None
    finally:
        conn.close()


def get_tmdb_cache_bulk(cache_keys: list, ttl: float = 86400.0) -> dict:
    """Return dict mapping cache_key -> parsed JSON data for keys within TTL."""
    if not cache_keys:
        return {}
    import time as _time
    import json as _json
    conn = get_db()
    try:
        out = {}
        now = _time.time()
        for chunk, placeholders in _sql_chunks(cache_keys):
            rows = conn.execute(
                f"SELECT cache_key, data_json, cached_at FROM tmdb_cache WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                if (now - row["cached_at"]) < ttl:
                    try:
                        out[row["cache_key"]] = _json.loads(row["data_json"])
                    except Exception:
                        pass
        return out
    finally:
        conn.close()


# get_tmdb_cache_bulk_light(): cache_key -> (cached_at, projection). A row is
# only parsed again when its cached_at changed, i.e. when it was rewritten.
_TMDB_LIGHT_MEMO: "OrderedDict[str, tuple]" = OrderedDict()
_TMDB_LIGHT_MAX = 20000
_TMDB_LIGHT_LOCK = threading.Lock()
TMDB_LIGHT_KEYS = ("found", "tmdb_id", "media_type", "title", "title_confident",
                   "genres", "providers", "fsk", "vote_average")


def tmdb_light_projection(data) -> dict:
    """The small part of a cached TMDB lookup most readers need: the card
    fields plus imdb_id and poster_path, which otherwise sit inside the
    10-50 KB raw_details blob."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    out = {k: data[k] for k in TMDB_LIGHT_KEYS if k in data}
    raw = data.get("raw_details") if isinstance(data.get("raw_details"), dict) else {}
    imdb_id = (data.get("imdb_id") or raw.get("imdb_id")
               or (raw.get("external_ids") or {}).get("imdb_id"))
    if imdb_id:
        out["imdb_id"] = imdb_id
    if raw.get("poster_path"):
        out["poster_path"] = raw.get("poster_path")
    return out


def get_tmdb_cache_bulk_light(cache_keys: list, ttl: float = 86400.0) -> dict:
    """get_tmdb_cache_bulk(), reduced to tmdb_light_projection().

    The full rows are 10-50 KB of JSON each (credits, alternative titles,
    recommendations, ...). Browse rows, the age gate, the calendar watcher and
    the queue read them by the hundred -- the calendar every 10 seconds -- to
    look at a few fields. Here only the timestamps are read in bulk, and a
    row's JSON is parsed again only when the row was rewritten.

    READ-ONLY: the returned dicts are shared between callers. Copy before
    changing one.
    """
    if not cache_keys:
        return {}
    import json as _json
    import time as _time
    now = _time.time()
    out = {}
    stale = []
    conn = get_db()
    try:
        for chunk, placeholders in _sql_chunks(list(dict.fromkeys(cache_keys))):
            rows = conn.execute(
                f"SELECT cache_key, cached_at FROM tmdb_cache WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            with _TMDB_LIGHT_LOCK:
                for row in rows:
                    if (now - row["cached_at"]) >= ttl:
                        continue
                    hit = _TMDB_LIGHT_MEMO.get(row["cache_key"])
                    if hit is not None and hit[0] == row["cached_at"]:
                        _TMDB_LIGHT_MEMO.move_to_end(row["cache_key"])
                        out[row["cache_key"]] = hit[1]
                    else:
                        stale.append(row["cache_key"])
        for chunk, placeholders in _sql_chunks(stale):
            rows = conn.execute(
                f"SELECT cache_key, data_json, cached_at FROM tmdb_cache "
                f"WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                if (now - row["cached_at"]) >= ttl:
                    continue
                try:
                    light = tmdb_light_projection(_json.loads(row["data_json"]))
                except Exception:
                    continue
                out[row["cache_key"]] = light
                with _TMDB_LIGHT_LOCK:
                    _TMDB_LIGHT_MEMO[row["cache_key"]] = (row["cached_at"], light)
                    _TMDB_LIGHT_MEMO.move_to_end(row["cache_key"])
                    while len(_TMDB_LIGHT_MEMO) > _TMDB_LIGHT_MAX:
                        _TMDB_LIGHT_MEMO.popitem(last=False)
        return out
    finally:
        conn.close()


def set_tmdb_cache(cache_key: str, data: dict) -> None:
    """Persist a TMDB result. Upserts so repeated calls refresh the TTL."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO tmdb_cache (cache_key, data_json, cached_at)
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


def clear_tmdb_cache() -> None:
    """Wipe all cached TMDB entries (e.g. after API-key change)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM tmdb_cache")
        conn.commit()
    finally:
        conn.close()


def evict_tmdb_cache(ttl: float = 86400.0) -> int:
    """Delete entries older than *ttl* seconds. Returns number of rows removed."""
    import time as _time
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM tmdb_cache WHERE cached_at < ?",
            (_time.time() - ttl,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============================================================
# Generic provider-availability cache (persistent, 24 h TTL)
# ============================================================
# Same shape/behaviour as the TMDB cache above, but namespaced so several
# independent providers (Crunchyroll, Fernsehserien.de, ...) can share one
# table without key collisions. Used so pill lookups survive a restart
# instead of living only in a process-memory dict.

def init_provider_cache_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_cache (
                namespace  TEXT    NOT NULL,
                cache_key  TEXT    NOT NULL,
                data_json  TEXT    NOT NULL,
                cached_at  REAL    NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            )
            """
        )
        # Same reason as the TMDB cache: eviction filters on cached_at, which
        # is not part of the primary key.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_cache_cached_at "
            "ON provider_cache(cached_at)"
        )
        # Remove expired entries so the table does not grow unboundedly
        conn.execute(
            "DELETE FROM provider_cache WHERE cached_at < strftime('%s', 'now') - 86400"
        )
        conn.commit()
    finally:
        conn.close()


def get_provider_cache(namespace: str, cache_key: str, ttl: float = 86400.0) -> "dict | None":
    """Return cached provider data if it exists and is within TTL, else None."""
    import time as _time
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, cached_at FROM provider_cache WHERE namespace = ? AND cache_key = ?",
            (namespace, cache_key),
        ).fetchone()
        if row and (_time.time() - row["cached_at"]) < ttl:
            import json as _json
            return _json.loads(row["data_json"])
        return None
    finally:
        conn.close()


def set_provider_cache(namespace: str, cache_key: str, data: dict) -> None:
    """Persist a provider-lookup result. Upserts so repeated calls refresh the TTL."""
    import json as _json
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO provider_cache (namespace, cache_key, data_json, cached_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, cache_key) DO UPDATE SET
                data_json = excluded.data_json,
                cached_at = excluded.cached_at
            """,
            (namespace, cache_key, _json.dumps(data, ensure_ascii=False), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_provider_cache(namespace: "str | None" = None) -> None:
    """Wipe cached entries for *namespace* (e.g. after credential changes), or all if None."""
    conn = get_db()
    try:
        if namespace is None:
            conn.execute("DELETE FROM provider_cache")
        else:
            conn.execute("DELETE FROM provider_cache WHERE namespace = ?", (namespace,))
        conn.commit()
    finally:
        conn.close()


def evict_provider_cache(ttl: float = 86400.0) -> int:
    """Delete entries older than *ttl* seconds across all namespaces."""
    import time as _time
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM provider_cache WHERE cached_at < ?",
            (_time.time() - ttl,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
