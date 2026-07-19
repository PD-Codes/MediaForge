"""Extractor auto-discovery and dispatch registry.

Every module under ``extractors/provider/`` implements one hoster (VOE,
Filemoon, Vidoza, ...) and exposes plain functions named
``get_direct_link_from_<provider>`` and/or ``get_preview_image_link_from_<provider>``.
On import, this package scans all modules in ``provider/`` and collects every
such function into the ``provider_functions`` dict, keyed by function name.

Model code (e.g. episode/movie classes) never imports a specific provider
module directly for this lookup; instead it picks the hoster the site
offered (e.g. "VOE", "Vidmoly") and looks up
``provider_functions[f"get_direct_link_from_{provider.lower()}"]`` to get the
right extractor function at runtime. This lets new provider modules be added
without touching any dispatch code.

A third-party module cannot drop a file into this package (it lives inside
MediaForge's own source tree, not the external ``~/.mediaforge/thirdparties/``
directory everything else discovers from) -- instead it calls
:func:`register_hoster` from its own ``register(app)`` to add a hoster the
same auto-discovered modules above end up in: ``provider_functions``,
``HOST_PROVIDER_MAP`` and ``config.SUPPORTED_PROVIDERS`` all gain the new
entry live, no restart needed for search text URLs. Live playback also picks
it up immediately because ``web/runtime_state.py``'s ``WORKING_PROVIDERS`` is
refreshed as part of registration (see :func:`register_hoster`).
"""
import importlib
import inspect
import pkgutil
from pathlib import Path
from urllib.parse import urlparse

from ..logger import get_logger

logger = get_logger(__name__)

provider_functions = {}

provider_path = Path(__path__[0]) / "provider"

for _, module_name, _ in pkgutil.iter_modules([str(provider_path)]):
    try:
        mod = importlib.import_module(f".provider.{module_name}", __name__)
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith(("get_direct_link_from_", "get_preview_image_link_from_")):
                provider_functions[name] = obj
    except Exception as e:
        logger.warning(f"Failed to load provider module '{module_name}': {e}")

# Example usage:
# provider_functions["get_direct_link_from_voe"](url)
#
# Used by: models/s_to/episode.py, models/filmpalast_to/episode.py,
# models/aniworld_to/episode.py, models/megakino_to/episode.py and
# models/megakino_to/movie.py (all via get_direct_link_for() below, which is
# keyed on the resolved host with the episode/movie's selected_provider label
# only as a fallback).


# Map a resolved hoster domain (netloc) to the provider key whose extractor
# function handles it. Streaming sites frequently label an embed with one
# hoster name while the /redirect actually lands on a *different* hoster's
# domain (mirrored labels: an AniWorld "Vidara"/"Vidavaca" entry often points
# straight at a voe.sx embed). Dispatching the extractor by the resolved host
# instead of the site's label makes such mislabeled links resolve correctly.
# Matched against the URL netloc by exact or subdomain-suffix comparison.
HOST_PROVIDER_MAP = {
    "voe.sx": "voe",
    "vidoza.net": "vidoza", "vidoza.to": "vidoza",
    "vidmoly.to": "vidmoly", "vidmoly.net": "vidmoly", "vidmoly.biz": "vidmoly",
    "filemoon.sx": "filemoon", "filemoon.to": "filemoon",
    "streamtape.com": "streamtape", "streamtape.to": "streamtape",
    "doodstream.com": "doodstream", "dood.to": "doodstream",
    "dood.watch": "doodstream", "dood.li": "doodstream",
    "luluvdo.com": "luluvdo",
    "vidara.to": "vidara", "vidara.so": "vidara",
    "vidavaca.net": "vidavaca",
    "veev.to": "veev",
}


def provider_for_url(url):
    """Return the provider key (extractor suffix, e.g. "voe") that owns *url*'s
    host, or None when the host isn't a known provider domain.

    Lets callers pick the right extractor by the actually-resolved host rather
    than the (possibly wrong) hoster label the source site attached to the link
    -- see HOST_PROVIDER_MAP for why the two disagree."""
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        return None
    if not host:
        return None
    for netloc, provider in HOST_PROVIDER_MAP.items():
        if host == netloc or host.endswith("." + netloc):
            return provider
    return None


def get_direct_link_for(url, fallback_provider=None):
    """Resolve the direct stream URL for *url*, choosing the extractor by the
    resolved host first and only falling back to *fallback_provider* (the site's
    hoster label) when the host is unknown.

    Raises ValueError with the same "not yet implemented" message the per-model
    dispatch used before, so existing error handling keeps working."""
    provider = provider_for_url(url) or (fallback_provider or "").lower()
    fn = provider_functions.get(f"get_direct_link_from_{provider}")
    if fn is None:
        raise ValueError(f"The provider '{provider}' is not yet implemented.")
    return fn(url)


# ---------------------------------------------------------------------------
# Third-party hosters
# ---------------------------------------------------------------------------
_EXTRA_HOSTERS = {}  # item_id -> provider key (lowercase)


def register_hoster(
    item_id,
    name,
    get_direct_link=None,
    get_preview_image=None,
    headers=None,
    host_patterns=(),
) -> None:
    """Register an additional video-hoster extractor from a third-party
    module's ``register(app)`` -- the external-module counterpart of dropping
    a file into ``extractors/provider/`` (see the module docstring above).

    - ``item_id``: the id already passed to ``register_thirdparty()`` for this
      module's entry, so ``web/thirdparties/registry.py``'s
      ``unregister_module()`` can undo this on disable/uninstall.
    - ``name``: the hoster's display name, e.g. ``"MyHoster"`` -- becomes the
      ``provider_functions`` key suffix (lower-cased) and the entry added to
      ``config.SUPPORTED_PROVIDERS``. Must not collide with an existing name.
    - ``get_direct_link(url) -> str``: same contract as a
      ``get_direct_link_from_<provider>`` function in ``extractors/provider/``.
      Optional if this hoster only needs preview images (unusual, but not
      disallowed).
    - ``get_preview_image(url) -> str | None``: optional, same contract as
      ``get_preview_image_link_from_<provider>``.
    - ``headers``: optional dict merged into both ``config.PROVIDER_HEADERS_D``
      and ``config.PROVIDER_HEADERS_W`` under ``name`` (only if not already
      present -- never overwrites a built-in hoster's headers).
    - ``host_patterns``: optional iterable of domains (e.g.
      ``("myhoster.com", "myhoster.cc")``) merged into ``HOST_PROVIDER_MAP``,
      so :func:`provider_for_url` recognizes a resolved/mirrored embed URL as
      this hoster even when the site's own label says something else.

    Live effect: ``provider_functions`` and ``HOST_PROVIDER_MAP`` are plain
    dict lookups, read fresh on every call, so both take effect immediately.
    ``config.SUPPORTED_PROVIDERS`` is mutated in place (``list.append``, never
    reassigned) for the same reason -- see that list's definition in
    ``config.py``. ``web/runtime_state.py``'s ``WORKING_PROVIDERS`` (what the
    UI actually offers users) is refreshed as part of this call too, via a
    lazy import (avoids a load-time circular import between this package and
    ``web/runtime_state.py``, which itself imports ``provider_functions`` from
    here).
    """
    if get_direct_link is None and get_preview_image is None:
        raise ValueError("register_hoster: need at least one of get_direct_link/get_preview_image")
    key = name.lower()
    from .. import config as _config

    existing = {p.lower() for p in _config.SUPPORTED_PROVIDERS} | set(_EXTRA_HOSTERS.values())
    if key in existing:
        raise ValueError(f"register_hoster: name already registered: {name!r}")

    if get_direct_link is not None:
        provider_functions[f"get_direct_link_from_{key}"] = get_direct_link
    if get_preview_image is not None:
        provider_functions[f"get_preview_image_link_from_{key}"] = get_preview_image
    for host in host_patterns:
        HOST_PROVIDER_MAP[host.lower()] = key
    if headers:
        _config.PROVIDER_HEADERS_D.setdefault(name, dict(headers))
        _config.PROVIDER_HEADERS_W.setdefault(name, dict(headers))

    _config.SUPPORTED_PROVIDERS.append(name)
    _EXTRA_HOSTERS[item_id] = key

    try:
        from ..web import runtime_state as _runtime_state
        _runtime_state.refresh_working_providers()
    except Exception:
        logger.exception("[Extractors] Failed to refresh WORKING_PROVIDERS after registering hoster '%s'", name)

    logger.info("[Extractors] Registered third-party hoster: %s (%s)", name, item_id)


def unregister_hoster(item_id) -> None:
    """Drop a hoster previously added via :func:`register_hoster`. Leaves
    ``config.SUPPORTED_PROVIDERS``/``HOST_PROVIDER_MAP`` entries in place
    (harmless once ``provider_functions`` no longer resolves them -- callers
    already treat an unresolvable provider as "not implemented") but removes
    it from ``provider_functions`` and refreshes ``WORKING_PROVIDERS`` so it
    stops being offered."""
    key = _EXTRA_HOSTERS.pop(item_id, None)
    if key is None:
        return
    provider_functions.pop(f"get_direct_link_from_{key}", None)
    provider_functions.pop(f"get_preview_image_link_from_{key}", None)
    try:
        from ..web import runtime_state as _runtime_state
        _runtime_state.refresh_working_providers()
    except Exception:
        logger.exception("[Extractors] Failed to refresh WORKING_PROVIDERS after unregistering hoster '%s'", item_id)
    logger.info("[Extractors] Unregistered third-party hoster: %s", item_id)
