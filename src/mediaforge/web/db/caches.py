"""TMDB/CineInfo and provider-availability result caches.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

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
