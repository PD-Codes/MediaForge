"""Registry of CineInfo data sources + the enrichment entry point.

A module adds a source from its ``register(app)``::

    from ...cineinfo.registry import register_cineinfo_source
    register_cineinfo_source(MySource())

The core CineInfo endpoints (``web/routes/search.py``) call :func:`enrich` to
layer every enabled source's data on top of the built-in TMDB result. With no
source registered, :func:`enrich` is a zero-cost pass-through -- the built-in
behaviour is completely unchanged.
"""
from __future__ import annotations

import threading

from ...logger import get_logger
from .source import CineInfoSource, QueryContext
from . import orchestrator

logger = get_logger(__name__)

_SOURCES: dict = {}
_lock = threading.Lock()

# Values treated as "empty" when deciding whether a source may fill a field.
# 0 / False are intentionally NOT here: they are valid values and must survive.
_EMPTY = (None, "", [], {})


def register_cineinfo_source(source: CineInfoSource) -> None:
    """Register (or replace) a CineInfo source by its stable id."""
    if not isinstance(source, CineInfoSource):
        raise TypeError("register_cineinfo_source expects a CineInfoSource instance")
    if not getattr(source, "id", None) or source.id == "abstract":
        raise ValueError("CineInfoSource needs a unique, non-default id")
    with _lock:
        _SOURCES[source.id] = source
    logger.info("[CineInfo] registered source %r (%s, bulk=%s)",
                source.id, source.label, source.supports_bulk)


def unregister_cineinfo_source(source_id: str) -> None:
    """Remove a source. Optional: sources are also filtered by is_enabled(), so a
    disabled/uninstalled module already stops contributing without this call."""
    with _lock:
        _SOURCES.pop(source_id, None)


def get_sources(enabled_only: bool = True) -> list:
    """Registered sources, ordered by id for stable, deterministic merging."""
    with _lock:
        sources = list(_SOURCES.values())
    if enabled_only:
        live = []
        for s in sources:
            try:
                if s.is_enabled():
                    live.append(s)
            except Exception:
                logger.debug("[CineInfo] is_enabled() raised for %r", s.id, exc_info=True)
        sources = live
    return sorted(sources, key=lambda s: s.id)


def enrich(items: list[dict], base_by_key: dict, ctx: QueryContext) -> dict:
    """Layer every enabled source onto ``base_by_key`` and return the merged map.

    items:       list of item dicts, each carrying a stable ``"key"`` plus lookup
                 fields (title / imdb_id / tmdb_id / ...).
    base_by_key: ``{key: base_payload}`` from the built-in TMDB lookup.

    Returns a NEW ``{key: merged_payload}``. Base fields always win; a source only
    fills fields the base is missing or left empty, applied in source-id order.
    """
    sources = get_sources(enabled_only=True)
    # Fast path: nothing registered -> return the base untouched, zero overhead.
    if not sources:
        return dict(base_by_key)

    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base_by_key.items()}
    for source in sources:
        try:
            part = orchestrator.query(source, items, ctx)
        except Exception:
            logger.exception("[CineInfo] enrich via %r failed", source.id)
            continue
        for key, payload in part.items():
            if not isinstance(payload, dict):
                continue
            target = merged.get(key)
            if isinstance(target, dict):
                _merge_fill(target, payload)
            else:
                merged[key] = dict(payload)
    return merged


def _merge_fill(base: dict, extra: dict) -> None:
    """Fill ``base`` with ``extra``'s fields where base is missing/empty.

    Base wins for any field it already has a non-empty value for. This keeps the
    built-in TMDB data authoritative and lets a source add only what TMDB lacks
    (custom fields, or gaps like an empty rating/provider list).
    """
    for k, v in extra.items():
        if v in _EMPTY:
            continue
        if base.get(k) in _EMPTY:
            base[k] = v
