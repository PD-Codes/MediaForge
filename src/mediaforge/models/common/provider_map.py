"""Map a streaming site's hoster label (or a resolved hoster host/URL) to the
canonical provider label MediaForge uses everywhere — the same spelling as in
config.SUPPORTED_PROVIDERS / an episode's ``selected_provider`` (e.g. "VOE").

The burning-series and kinox models call :func:`host_to_provider` to turn the
hoster name a site prints next to each mirror ("VOE", "Streamtape", "Dood", …)
into the provider whose extractor can resolve it, so the resulting bucket keys
line up with ``selected_provider`` and :func:`config.build_provider_attempt_order`.

Lives natively in MediaForge so those models don't depend on the upstream
``aniworld`` package.
"""

from __future__ import annotations

from ...config import SUPPORTED_PROVIDERS
from ...extractors import provider_for_url

# provider key (lowercase extractor suffix) -> canonical display label. Includes
# providers that ship an extractor but are currently commented out of
# SUPPORTED_PROVIDERS, since a site can still list them.
_CANONICAL_LABEL = {
    "voe": "VOE",
    "vidmoly": "Vidmoly",
    "vidoza": "Vidoza",
    "veev": "VeeV",
    "vidara": "Vidara",
    "vidavaca": "Vidavaca",
    "doodstream": "Doodstream",
    "filemoon": "Filemoon",
    "loadx": "LoadX",
    "luluvdo": "Luluvdo",
    "streamtape": "Streamtape",
    "megakino": "MegaKino",
}

# Display-name alias (lowercased) -> provider key. Kept conservative (full names
# plus the common "Dood" short form) so a loose substring match can't misfire.
_NAME_ALIASES = {
    "voe": "voe",
    "vidmoly": "vidmoly",
    "vidoza": "vidoza",
    "veev": "veev",
    "vidara": "vidara",
    "vidavaca": "vidavaca",
    "doodstream": "doodstream",
    "dood": "doodstream",
    "filemoon": "filemoon",
    "loadx": "loadx",
    "luluvdo": "luluvdo",
    "streamtape": "streamtape",
}

# Every currently-enabled provider must resolve to its own canonical label.
for _p in SUPPORTED_PROVIDERS:
    _NAME_ALIASES.setdefault(str(_p).strip().lower(), str(_p).strip().lower())
    _CANONICAL_LABEL.setdefault(str(_p).strip().lower(), _p)


def _canonical(key):
    return _CANONICAL_LABEL.get(key, key)


def host_to_provider(name):
    """Return the canonical provider label for a hoster *name*.

    *name* may be a display label ("VOE", "Streamtape HD") or a resolved hoster
    host/URL ("voe.sx", "https://vidoza.net/embed"). Returns None when the
    hoster isn't one MediaForge has an extractor for.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    low = raw.lower()

    # A host or URL: dispatch by the resolved host via HOST_PROVIDER_MAP.
    if "://" in low or "/" in low or "." in low:
        probe = raw if "://" in low else "https://" + low
        key = provider_for_url(probe)
        if key:
            return _canonical(key)

    # A display label: exact alias first, then a loose substring match so a
    # decorated label ("VOE Player", "Streamtape HD") still resolves.
    key = _NAME_ALIASES.get(low)
    if not key:
        for alias, provider_key in _NAME_ALIASES.items():
            if alias in low:
                key = provider_key
                break
    return _canonical(key) if key else None
