"""CineInfo data-source contract.

A :class:`CineInfoSource` is what a third-party module registers (via
``web/cineinfo/registry.register_cineinfo_source``) to add extra CineInfo data
on top of the built-in TMDB lookup, WITHOUT any change to the core TMDB code.

The orchestrator (``web/cineinfo/orchestrator.py``) drives every source through
the SAME cache + rate-limit + in-flight-dedup layer and picks one of two fetch
forms automatically, based purely on the source's declared ``supports_bulk``
capability:

  * ``supports_bulk = False`` -> the orchestrator loops :meth:`fetch_one` per
    item ("one by one"), bounded by a worker pool and a per-source rate limiter.
  * ``supports_bulk = True``  -> the orchestrator calls :meth:`fetch_many` once
    per chunk of up to ``max_bulk`` items ("all at once, a single request").

A source never has to implement batching, caching or rate limiting itself; it
only declares its capability and implements the fetch method that matches it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QueryContext:
    """Immutable per-query context, resolved once by the caller.

    country:  CineInfo region code, e.g. "DE" (the ``cineinfo_country`` setting).
    ui_lang:  UI language, "de" or "en" (mapped to de-DE / en-US downstream).
    fields:   Optional hint of the fields the caller cares about; a source may
              use it to skip upstream calls it was not asked for. Empty means
              "everything you have".
    """

    country: str = "DE"
    ui_lang: str = "de"
    fields: frozenset = field(default_factory=frozenset)


class CineInfoSource:
    """Base class for a CineInfo data source. Subclass and register one instance.

    Item shape: every item passed to :meth:`fetch_one` / :meth:`fetch_many` is a
    plain dict carrying at least a stable ``"key"`` (assigned by the caller) plus
    whatever lookup fields the source needs, e.g.
    ``{"key": ..., "title": ..., "imdb_id": ..., "tmdb_id": ...}``. Return
    payloads are plain dicts of CineInfo fields (genres / fsk / rating /
    providers / trailer / recommendations / any custom field); only the fields a
    source actually knows need to be present.
    """

    # Stable, unique id. Also used as the provider-cache namespace and the
    # rate-limiter bucket key, so keep it constant across releases.
    id: str = "abstract"
    label: str = "Abstract source"

    # --- Batch-form capability (drives the automatic mode selection) ----------
    # False -> orchestrator loops fetch_one(); True -> orchestrator calls
    # fetch_many() once per chunk. This single flag is the whole "two forms"
    # decision -- chosen purely by what the source supports, no user setting.
    supports_bulk: bool = False
    # Hard upper bound per bulk request; the orchestrator chunks larger inputs.
    max_bulk: int = 20

    # --- Shared-infra knobs ---------------------------------------------------
    rate: float = 5.0           # max upstream requests/second for this source
    cache_ttl: float = 86400.0  # provider-cache TTL in seconds (0 disables it)

    @property
    def cache_ns(self) -> str:
        """Provider-cache namespace for this source (see db.get_provider_cache)."""
        return f"cineinfo_src_{self.id}"

    def is_enabled(self) -> bool:
        """Return True if this source should currently contribute.

        Modules typically read their own enabled setting here, so a disabled (or
        uninstalled) module stops contributing immediately, without any registry
        cleanup. Defaults to True for always-on sources.
        """
        return True

    # --- One-by-one form ("Einzeln nach und nach") ----------------------------
    def fetch_one(self, item: dict, ctx: QueryContext) -> dict:
        """Return the CineInfo payload for ONE item.

        Called only when ``supports_bulk`` is False. Raise to signal a hard
        failure for this item; the orchestrator isolates it and keeps going with
        the others.
        """
        raise NotImplementedError

    # --- All-at-once form ("Alles in einer Anfrage") --------------------------
    def fetch_many(self, items: list[dict], ctx: QueryContext) -> dict:
        """Return ``{item["key"]: payload}`` for the given items in a SINGLE
        upstream request.

        Called only when ``supports_bulk`` is True. Items whose key is missing
        from the returned dict are treated as "no data for this item".
        """
        raise NotImplementedError
