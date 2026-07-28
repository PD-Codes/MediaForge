"""CineInfo source registry, contract and query orchestrator.

This package is the *extension point* that lets third-party modules add new
CineInfo providers/sources -- with automatic per-item vs. single-request batch
handling -- without touching the core TMDB code. See
:func:`registry.register_cineinfo_source` and :class:`source.CineInfoSource`.

It adds no behaviour on its own: with no source registered,
:func:`registry.enrich` is a pass-through, so the built-in CineInfo/TMDB
behaviour is completely unchanged.
"""
from .source import CineInfoSource, QueryContext
from .registry import (
    register_cineinfo_source,
    unregister_cineinfo_source,
    unregister_cineinfo_owner,
    thirdparty_cineinfo_source_ids,
    thirdparty_cineinfo_sources_by_item,
    configured_order,
    describe_sources,
    get_sources,
    enrich,
)

__all__ = [
    "CineInfoSource",
    "QueryContext",
    "register_cineinfo_source",
    "unregister_cineinfo_source",
    "unregister_cineinfo_owner",
    "thirdparty_cineinfo_source_ids",
    "thirdparty_cineinfo_sources_by_item",
    "configured_order",
    "describe_sources",
    "get_sources",
    "enrich",
]
