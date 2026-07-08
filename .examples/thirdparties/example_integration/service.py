"""Example Integration — demo data service.

Demonstrates the same caching pattern real integrations use for external
APIs (see web/thirdparties/anime_seasons/service.py for a real fetch-from-
an-external-API version, including rate-limiting and pagination): read
from the shared ``provider_cache`` table first, and only regenerate on a
cold or expired cache entry.

This example never makes a network call — it generates a small fixed list
of placeholder items locally — so it always works offline and never fails
regardless of network access, which keeps it usable as a drop-in smoke
test for the plug-in system itself. A real integration would replace
_generate_items() with an HTTP call (via ``requests``, already a project
dependency) and keep everything else — the caching shape below — the same.
"""

from __future__ import annotations

from ...db import get_provider_cache, set_provider_cache
from ....logger import get_logger

logger = get_logger(__name__)

_NAMESPACE = "example_integration"
_CACHE_KEY = "items"
_CACHE_TTL = 3600  # seconds; tune to how often your real data source changes


def _generate_items() -> list:
    """Stand-in for a real external fetch. In a real integration this is
    where you'd call e.g. ``requests.get(...)`` and normalize the response
    into a list of plain dicts, the same way anime_seasons/service.py's
    _normalize_entry() does for Jikan's response shape."""
    return [
        {
            "id": 1,
            "title": "First example item",
            "description": "Replace _generate_items() with a real data source.",
        },
        {
            "id": 2,
            "title": "Second example item",
            "description": "Each item only needs an id, title, and description for this demo template.",
        },
        {
            "id": 3,
            "title": "Third example item",
            "description": "Cached via the shared provider_cache table, same as every other integration.",
        },
    ]


def get_items() -> list:
    """Cached item list, regenerated once per _CACHE_TTL seconds. Mirrors
    anime_seasons.service.get_season()'s shape, minus the in-flight
    de-duplication lock (add one back if your real fetch is slow/rate
    limited and might otherwise be triggered by several concurrent
    requests at once — see _JikanRateLimiter / _refresh_locks there)."""
    cached = get_provider_cache(_NAMESPACE, _CACHE_KEY, ttl=_CACHE_TTL)
    if cached is not None:
        return cached.get("items", [])

    items = _generate_items()
    set_provider_cache(_NAMESPACE, _CACHE_KEY, {"items": items})
    return items
