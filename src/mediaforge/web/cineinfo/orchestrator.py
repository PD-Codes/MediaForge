"""CineInfo query orchestrator.

Runs a single :class:`CineInfoSource` over a list of items and returns
``{key: payload}``, automatically choosing between the two fetch forms and
funnelling both through the same cache + rate-limit + in-flight-dedup layer:

  * ``supports_bulk = False`` -> loop :meth:`fetch_one` over the cache-misses,
    bounded by a thread pool and a per-source token-bucket rate limiter.
  * ``supports_bulk = True``  -> call :meth:`fetch_many` once per chunk of up to
    ``source.max_bulk`` cache-misses ("all at once").

The cache is always consulted first, so only misses ever reach the network in
either form. Every source id gets its own rate-limiter bucket, so sources cannot
starve one another.
"""
from __future__ import annotations

import concurrent.futures as _cf
import threading
import time

from ..db import get_provider_cache, set_provider_cache
from ...logger import get_logger
from .source import CineInfoSource, QueryContext

logger = get_logger(__name__)

# Bounded concurrency for the per-item loop form. Kept small on purpose: a
# module source hitting a third-party API should not open dozens of sockets.
_MAX_WORKERS = 4
# Hard cap on how long one query() may block, so a slow/hanging source can
# never freeze the request thread indefinitely.
_QUERY_TIMEOUT = 30.0


class _RateLimiter:
    """Thread-safe token-bucket, same shape as tmdb_cache._TmdbRateLimiter."""

    def __init__(self, rate: float):
        self._rate = max(0.1, float(rate))
        self._tokens = self._rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                wait = 0.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
        if wait > 0:
            time.sleep(wait)


# One limiter + one in-flight registry per source id, created lazily.
_limiters: dict = {}
_limiters_lock = threading.Lock()
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _limiter_for(source: CineInfoSource) -> _RateLimiter:
    want = max(0.1, float(getattr(source, "rate", 5.0)))
    with _limiters_lock:
        rl = _limiters.get(source.id)
        if rl is None or rl._rate != want:
            rl = _RateLimiter(want)
            _limiters[source.id] = rl
        return rl


def _cache_key(item_key: str, ctx: QueryContext) -> str:
    # Country + language are part of the identity: the same title yields
    # different providers/certifications per region and per UI language.
    return f"{item_key}|||{ctx.country}|||{ctx.ui_lang}"


def _chunks(seq, size):
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def query(source: CineInfoSource, items: list[dict], ctx: QueryContext) -> dict:
    """Return ``{item["key"]: payload}`` for ``items``, cache-first, auto form."""
    use_cache = bool(getattr(source, "cache_ttl", 0) and source.cache_ttl > 0)
    results: dict = {}
    misses: list[dict] = []

    # 1) Cache first -- only misses ever touch the network.
    for it in items:
        key = it.get("key")
        if key is None:
            continue
        if use_cache:
            cached = get_provider_cache(source.cache_ns, _cache_key(key, ctx),
                                        ttl=source.cache_ttl)
            if cached is not None:
                results[key] = cached.get("payload", {})
                continue
        misses.append(it)

    if not misses:
        return results

    # 2) Fetch the misses in the form the source supports.
    try:
        if source.supports_bulk:
            fetched = _run_bulk(source, misses, ctx, use_cache)
        else:
            fetched = _run_loop(source, misses, ctx, use_cache)
    except Exception:
        logger.exception("[CineInfo] source %r query failed", source.id)
        fetched = {}

    # 3) Merge (loop already wrote the cache for its own results; bulk writes
    #    here as it has the full chunk in hand).
    for it in misses:
        key = it.get("key")
        payload = fetched.get(key)
        if payload is None:
            continue
        results[key] = payload
    return results


def _run_bulk(source, misses, ctx, use_cache) -> dict:
    """All-at-once form: one fetch_many() per chunk of <= max_bulk items."""
    out: dict = {}
    for chunk in _chunks(misses, getattr(source, "max_bulk", 20)):
        _limiter_for(source).acquire()  # one token per upstream request
        try:
            part = source.fetch_many(chunk, ctx) or {}
        except Exception:
            logger.exception("[CineInfo] source %r fetch_many failed", source.id)
            part = {}
        for k, v in part.items():
            if v is None:
                continue
            out[k] = v
            if use_cache:
                _store(source, k, ctx, v)
    return out


def _run_loop(source, misses, ctx, use_cache) -> dict:
    """One-by-one form: bounded thread pool, per-source rate limit + dedup."""
    out: dict = {}
    with _cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS,
                                thread_name_prefix=f"cineinfo-{source.id}") as pool:
        fut_map = {pool.submit(_guarded_fetch_one, source, it, ctx, use_cache): it.get("key")
                   for it in misses}
        try:
            for fut in _cf.as_completed(fut_map, timeout=_QUERY_TIMEOUT):
                key = fut_map[fut]
                try:
                    val = fut.result()
                except Exception:
                    val = None
                if val is not None:
                    out[key] = val
        except _cf.TimeoutError:
            logger.warning("[CineInfo] source %r loop timed out after %.0fs",
                           source.id, _QUERY_TIMEOUT)
    return out


def _guarded_fetch_one(source, item, ctx, use_cache):
    """Rate-limited fetch_one with process-wide in-flight de-duplication.

    The leader fetches and writes the cache BEFORE releasing waiters, so a
    concurrent request for the same key returns the cached value instead of
    firing a second upstream request.
    """
    key = item.get("key")
    dedup_key = (source.id, _cache_key(key, ctx))

    with _inflight_lock:
        ev = _inflight.get(dedup_key)
        leader = ev is None
        if leader:
            ev = threading.Event()
            _inflight[dedup_key] = ev

    if not leader:
        ev.wait(timeout=_QUERY_TIMEOUT)
        if use_cache:
            cached = get_provider_cache(source.cache_ns, _cache_key(key, ctx),
                                        ttl=source.cache_ttl)
            if cached is not None:
                return cached.get("payload", {})
        return None

    try:
        _limiter_for(source).acquire()
        payload = source.fetch_one(item, ctx)
        if payload is not None and use_cache:
            _store(source, key, ctx, payload)
        return payload
    finally:
        with _inflight_lock:
            _inflight.pop(dedup_key, None)
        ev.set()


def _store(source, item_key, ctx, payload) -> None:
    try:
        set_provider_cache(source.cache_ns, _cache_key(item_key, ctx), {"payload": payload})
    except Exception:
        logger.debug("[CineInfo] cache store failed for %r", item_key, exc_info=True)
