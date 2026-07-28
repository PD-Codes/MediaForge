"""Registry of CineInfo data sources + the enrichment entry point.

A module adds a source from its ``register(app)``::

    from ...cineinfo.registry import register_cineinfo_source
    register_cineinfo_source(MySource(), item_id=MODULE_ID)

``item_id`` is the id the module already passed to ``register_thirdparty()``.
Every other secondary registry (``providers``, ``search``, ``home_feed``,
``mirrors``, ``extractors``, ``uptime_monitor``) keys its registrations by it,
and it buys the same two things here: the Modulmanager can report "1 x CineInfo
source" among a module's capabilities, and ``web/thirdparties/registry.py``'s
``unregister_module()`` drops the source when the module is disabled or
uninstalled -- instead of leaving it in :data:`_SOURCES` for the rest of the
process' life, kept harmless only by its own ``is_enabled()``.

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

# source_id -> CineInfoSource. Keyed by the source's own id (not by item_id)
# because get_sources()/enrich() care about sources, and two modules must never
# silently end up with the same source id.
_SOURCES: dict = {}
# item_id -> {source_id, ...}: who registered what. Separate from _SOURCES so
# the merge order and the "one source id, one source" rule above stay untouched,
# and so a source registered without an owner (the pre-item_id signature) is
# still a perfectly working source -- just an unattributed one.
_OWNERS: dict = {}
_lock = threading.RLock()

# Values treated as "empty" when deciding whether a source may fill a field.
# 0 / False are intentionally NOT here: they are valid values and must survive.
_EMPTY = (None, "", [], {})

# The user-configurable order lives in the same setting as the provider-pill
# chain (Integrations -> CineInfo -> "Provider order"), because both answer the
# same question from a user's point of view -- "which source gets to speak
# first about this title" -- and two separate drag lists on one tab would be a
# worse answer than one. The two halves never collide because each consumer
# only looks at its own prefix: a CineInfo source is "ci:<source id>", a
# module's provider pill is "ext:<name>", and the built-in pills are bare ids
# (see static/app.js's _pillSources(), which ignores everything it does not
# know -- including every "ci:" entry).
ORDER_SETTING = "cineinfo_provider_order"
ORDER_PREFIX = "ci:"


def register_cineinfo_source(source: CineInfoSource, item_id=None) -> None:
    """Register (or replace) a CineInfo source by its stable id.

    ``item_id`` is optional only for backwards compatibility with modules
    written against the first version of this API. A third-party module should
    always pass the id it gave ``register_thirdparty()``: without it the source
    works, but it is invisible to the Modulmanager's capability list and
    survives the module being uninstalled.
    """
    if not isinstance(source, CineInfoSource):
        raise TypeError("register_cineinfo_source expects a CineInfoSource instance")
    if not getattr(source, "id", None) or source.id == "abstract":
        raise ValueError("CineInfoSource needs a unique, non-default id")
    with _lock:
        # Re-registering the same source id under a new owner must not leave the
        # old owner claiming it -- otherwise unregister_cineinfo_owner() for the
        # stale module would rip out the live module's source.
        for owner, source_ids in _OWNERS.items():
            if owner != item_id:
                source_ids.discard(source.id)
        _SOURCES[source.id] = source
        if item_id is not None:
            _OWNERS.setdefault(item_id, set()).add(source.id)
    if item_id is None:
        logger.warning(
            "[CineInfo] source %r registered without an item_id -- it will not "
            "show up in the module manager and is not cleaned up on uninstall",
            source.id)
    logger.info("[CineInfo] registered source %r (%s, bulk=%s, item=%s)",
                source.id, source.label, source.supports_bulk, item_id)


def unregister_cineinfo_source(source_id: str) -> None:
    """Remove one source by its own id. Optional: sources are also filtered by
    is_enabled(), so a disabled module already stops contributing without this
    call."""
    with _lock:
        _SOURCES.pop(source_id, None)
        for source_ids in _OWNERS.values():
            source_ids.discard(source_id)
        for owner in [o for o, s in _OWNERS.items() if not s]:
            _OWNERS.pop(owner, None)


def unregister_cineinfo_owner(item_id) -> None:
    """Remove every source registered under ``item_id``.

    The counterpart of :func:`register_cineinfo_source`'s ``item_id``, called by
    ``web/thirdparties/registry.py``'s ``unregister_module()``. Named after the
    owner rather than the source so it can never be confused with
    :func:`unregister_cineinfo_source`, which takes the *source's* id.
    """
    with _lock:
        source_ids = _OWNERS.pop(item_id, set())
        for source_id in source_ids:
            _SOURCES.pop(source_id, None)
    if source_ids:
        logger.info("[CineInfo] unregistered source(s) %s of module item %s",
                    ", ".join(sorted(source_ids)), item_id)


def thirdparty_cineinfo_source_ids() -> set:
    """item_ids that currently own at least one CineInfo source.

    Read-only counterpart of :func:`unregister_cineinfo_owner`, used by the
    Modulmanager's capability list so it can report what a module added without
    reaching into this module's private dicts.
    """
    with _lock:
        return {item_id for item_id, sources in _OWNERS.items() if sources}


def thirdparty_cineinfo_sources_by_item() -> dict:
    """``{item_id: (source_id, ...)}`` for every owned source.

    Unlike the other secondary registries, one module item may legitimately own
    *several* CineInfo sources (a catalog with a bulk and a per-item endpoint is
    two sources). The Modulmanager uses this so such a module reports "2 x
    CineInfo source" instead of collapsing to one.
    """
    with _lock:
        return {item_id: tuple(sorted(sources))
                for item_id, sources in _OWNERS.items() if sources}


def configured_order() -> list:
    """The saved source order, as bare source ids (the ``ci:`` prefix stripped).

    A preference, not a whitelist: :func:`get_sources` appends anything the
    saved order does not mention, so a source added by a module installed after
    the order was last saved still runs -- just last.
    """
    try:
        from ..db import get_setting
        raw = get_setting(ORDER_SETTING, "") or ""
    except Exception:
        # No app/DB context (a CLI import, a unit test): fall back to the
        # historical alphabetical order rather than failing the lookup.
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.startswith(ORDER_PREFIX):
            source_id = part[len(ORDER_PREFIX):]
            if source_id and source_id not in out:
                out.append(source_id)
    return out


def get_sources(enabled_only: bool = True) -> list:
    """Registered sources in the order :func:`enrich` should apply them.

    Configured sources first, in the user's saved order; everything else after
    them, alphabetically by id -- which is exactly the old behaviour when
    nothing is configured, so an untouched installation merges as before.
    """
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
    order = configured_order()

    def _key(source):
        try:
            return (0, order.index(source.id), "")
        except ValueError:
            return (1, 0, source.id)

    return sorted(sources, key=_key)


def describe_sources(enabled_only: bool = True) -> list:
    """``[{id, order_id, label, supports_bulk, item_id, module_name}]`` for the
    settings UI, in the same order :func:`get_sources` returns.

    ``order_id`` is the id the saved order uses (``ci:<id>``), so the frontend
    never has to know about the prefix convention. ``module_name`` is the
    folder that registered the source, used for the "Module" pill's tooltip;
    it is ``None`` for a source registered without an ``item_id``.
    """
    with _lock:
        owner_by_source = {sid: item for item, sids in _OWNERS.items() for sid in sids}
    out = []
    for source in get_sources(enabled_only=enabled_only):
        item_id = owner_by_source.get(source.id)
        module_name = None
        if item_id:
            try:
                from ..thirdparties.registry import module_name_for_item
                module_name = module_name_for_item(item_id)
            except Exception:
                logger.debug("[CineInfo] module name lookup failed for %r", item_id)
        out.append({
            "id": source.id,
            "order_id": ORDER_PREFIX + source.id,
            "label": getattr(source, "label", None) or source.id,
            "supports_bulk": bool(getattr(source, "supports_bulk", False)),
            "item_id": item_id,
            "module_name": module_name,
        })
    return out


def enrich(items: list[dict], base_by_key: dict, ctx: QueryContext) -> dict:
    """Layer every enabled source onto ``base_by_key`` and return the merged map.

    items:       list of item dicts, each carrying a stable ``"key"`` plus lookup
                 fields (title / imdb_id / tmdb_id / ...).
    base_by_key: ``{key: base_payload}`` from the built-in TMDB lookup.

    Returns a NEW ``{key: merged_payload}``. Base fields always win; a source only
    fills fields the base is missing or left empty, applied in the order
    :func:`get_sources` returns -- so of two sources that both know a field, the
    one the user put first wins it.
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
