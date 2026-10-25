"""Shared low-level helpers for the Direct Link embed-host resolution
pipeline: which hosts MediaForge can resolve itself (:func:`fast_providers`),
the default desktop User-Agent, and :func:`resolve_stream_for_provider` --
the "call this host's extractor" step. Used by probe.py's
discover_and_resolve().

The host list is derived at runtime instead of being hard-coded: every name
the hoster-domain table (models/megakino_to/scraper.py's ``_HOSTER_DOMAINS``,
the authoritative domain-substring -> provider-name mapping) knows about is
kept if -- and only if -- MediaForge actually has a working extractor for it
under extractors/provider/. Names whose extractor is only a stub (raises
NotImplementedError) drop out on their own, so enabling a new provider is
enough to make Direct Link pick it up too.

Order matters: candidates are tried in the user's configured provider order
(Settings -> Provider order, see web/runtime_state.get_provider_order()), and
only when *no* supported provider can resolve the link does Direct Link fall
back to yt-dlp's generic extraction.
"""
import logging as _logging
import threading
import time

from ...config import PROVIDER_HEADERS_D
from ...extractors import provider_functions
from ..megakino_to.scraper import _HOSTER_DOMAINS, normalize_hoster_url

# Some CDNs reject yt-dlp's/requests' default User-Agent outright; this
# mirrors the desktop Chrome UA the user's own .bat script (see issue #8)
# used successfully.
DIRECT_LINK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Site-native players, not third-party embed hosts -- never resolvable through
# the generic embed-host path.
_NOT_EMBED_HOSTS = {"Hanime"}

_impl_lock = threading.Lock()
_impl_cache = {"ts": 0.0, "names": None}
_IMPL_CACHE_TTL = 300.0


class _MuteThread(_logging.Filter):
    """Drops the records of one thread (the stub probe below)."""

    def __init__(self, ident):
        super().__init__()
        self.ident = ident

    def filter(self, record):
        return record.thread != self.ident


def _all_handlers():
    handlers = set(_logging.getLogger().handlers)
    for lg in list(_logging.root.manager.loggerDict.values()):
        for handler in getattr(lg, "handlers", None) or ():
            handlers.add(handler)
    return handlers


def _extractor_implemented(name):
    """True if extractors/provider/ has a real (non-stub) extractor for *name*.

    Probes the extractor with an empty URL: NotImplementedError means the
    module is only a placeholder; any other exception means it is implemented
    and merely rejected the empty URL, as expected. Logging is silenced during
    the probe so those intentional errors don't spam the console.
    """
    fn = provider_functions.get(f"get_direct_link_from_{name.lower()}")
    if fn is None:
        return False
    # Only THIS thread's records are dropped. logging.disable() is process
    # wide: while the probe ran, every other thread's log lines -- ERRORs the
    # telemetry handler should have seen included -- were thrown away too.
    mute = _MuteThread(threading.get_ident())
    handlers = _all_handlers()
    for handler in handlers:
        handler.addFilter(mute)
    try:
        fn("")
    except NotImplementedError:
        return False
    except Exception:
        return True
    finally:
        for handler in handlers:
            handler.removeFilter(mute)
    return True


def _implemented_provider_names():
    """Every hoster name with a real extractor (cached, TTL 5 min)."""
    now = time.time()
    with _impl_lock:
        if _impl_cache["names"] is not None and now - _impl_cache["ts"] < _IMPL_CACHE_TTL:
            return _impl_cache["names"]

    names = []
    for _needle, name in _HOSTER_DOMAINS:
        if name in names or name in _NOT_EMBED_HOSTS:
            continue
        if _extractor_implemented(name):
            names.append(name)

    with _impl_lock:
        _impl_cache.update({"ts": now, "names": tuple(names)})
    return tuple(names)


def fast_providers():
    """The resolvable embed hosts, in the user's configured provider order.

    Providers the user ordered explicitly come first (Settings -> Provider
    order); anything else MediaForge can still resolve is appended after them,
    so a host is never silently ignored just because it isn't in that list.
    """
    implemented = list(_implemented_provider_names())
    try:
        from ...web.runtime_state import get_provider_order
        preferred = [p for p in get_provider_order() if p in implemented]
    except Exception:  # settings/DB not available (e.g. a plain CLI import)
        preferred = []
    return tuple(preferred + [p for p in implemented if p not in preferred])


class _FastProviderSet:
    """Live, backwards-compatible ``FAST_PROVIDERS`` view.

    This used to be a hard-coded set; it is now a view over
    :func:`fast_providers`, so existing ``name in FAST_PROVIDERS`` checks keep
    working while picking up newly enabled/implemented providers without a
    restart.
    """

    def __contains__(self, name):
        return name in fast_providers()

    def __iter__(self):
        return iter(fast_providers())

    def __len__(self):
        return len(fast_providers())

    def __repr__(self):
        return repr(set(fast_providers()))


FAST_PROVIDERS = _FastProviderSet()


def resolve_stream_for_provider(name, url, timeout=12):
    """Resolve a known embed-host page (*url*, already classified as *name*,
    e.g. "VOE") to a direct stream URL + the HTTP headers its CDN expects,
    using the same extractor the scraper sites use for this host.

    Raises RuntimeError if the extractor is missing, the link is dead
    (404/expired), or it returns nothing -- callers decide whether/how to
    fall back (e.g. try the next candidate link, or generic yt-dlp).
    """
    embed_url = normalize_hoster_url(url)
    fn = provider_functions.get(f"get_direct_link_from_{name.lower()}")
    if fn is None:
        raise RuntimeError(f"No extractor available for provider '{name}'")

    # VOE/Vidara/Vidavaca support tuning retries/timeout down for a quick
    # probe; the others are already single fast requests with no retry loop.
    if name == "VOE":
        resolved = fn(embed_url, max_retries=1, timeout=timeout)
    elif name in ("Vidara", "Vidavaca"):
        resolved = fn(embed_url, timeout=timeout)
    else:
        resolved = fn(embed_url)

    if not resolved:
        raise RuntimeError(f"{name} did not return a stream URL for this link")

    headers = dict(PROVIDER_HEADERS_D.get(name, {}))
    headers.setdefault("User-Agent", DIRECT_LINK_USER_AGENT)
    return resolved, headers

