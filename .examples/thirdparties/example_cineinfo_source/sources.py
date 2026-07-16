"""Example CineInfo sources -- demonstrates BOTH batch forms, offline-safe.

Two reference sources are registered by ``example_cineinfo_source/__init__.py``:

  * :class:`ExampleLoopSource` (``supports_bulk = False``) -- the orchestrator
    calls :meth:`fetch_one` once per item ("einzeln nach und nach").
  * :class:`ExampleBulkSource` (``supports_bulk = True``) -- the orchestrator
    calls :meth:`fetch_many` once for the whole chunk ("alles in einer Anfrage").

Neither makes a network call: they synthesize placeholder fields locally so the
example always works offline. A real source replaces the method bodies with an
HTTP call (via ``requests``, already a project dependency) and keeps everything
else -- the class shape and the ``supports_bulk`` flag -- exactly the same.
"""
from __future__ import annotations

from ...cineinfo.source import CineInfoSource, QueryContext
from ...db import get_setting
from ....logger import get_logger

logger = get_logger(__name__)

# The module's single master toggle (see __init__.py's register_thirdparty).
ENABLED_KEY = "example_cineinfo_source_enabled"


class _EnabledMixin:
    """Both demo sources follow the module's master toggle, so a disabled or
    uninstalled module stops contributing immediately -- no registry cleanup."""

    def is_enabled(self) -> bool:
        return get_setting(ENABLED_KEY, "0") == "1"


class ExampleLoopSource(_EnabledMixin, CineInfoSource):
    """One-by-one form: implements fetch_one()."""

    id = "example_loop"
    label = "Example (per-item)"
    supports_bulk = False
    rate = 5.0
    cache_ttl = 600.0  # short TTL so the demo refreshes visibly

    def fetch_one(self, item: dict, ctx: QueryContext) -> dict:
        title = item.get("title") or item.get("imdb_id") or "?"
        # A real source would call its API for this SINGLE title here.
        return {"example_loop_note": f"per-item hit for {title!r} in {ctx.country}"}


class ExampleBulkSource(_EnabledMixin, CineInfoSource):
    """All-at-once form: implements fetch_many()."""

    id = "example_bulk"
    label = "Example (bulk)"
    supports_bulk = True
    max_bulk = 20
    rate = 5.0
    cache_ttl = 600.0

    def fetch_many(self, items: list[dict], ctx: QueryContext) -> dict:
        # A real bulk source would send ONE request carrying every key here and
        # map the response back onto item["key"].
        out: dict = {}
        for it in items:
            key = it.get("key")
            if key is None:
                continue
            out[key] = {"example_bulk_note": f"bulk hit ({len(items)} items in one call)"}
        return out
