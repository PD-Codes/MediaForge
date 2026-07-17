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
    "watch.gxplayer.xyz": "megakino",
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
