"""Shared low-level helpers for the Direct Link embed-host resolution
pipeline: the set of hosts MediaForge can quickly resolve (FAST_PROVIDERS),
the default desktop User-Agent, and resolve_stream_for_provider() -- the
"call this host's extractor" step. Used by probe.py's discover_and_resolve().
"""
from ...config import PROVIDER_HEADERS_D
from ...extractors import provider_functions
from ..megakino_to.scraper import normalize_hoster_url

# Some CDNs reject yt-dlp's/requests' default User-Agent outright; this
# mirrors the desktop Chrome UA the user's own .bat script (see issue #8)
# used successfully.
DIRECT_LINK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Embed hosts MediaForge already has a working, fast (no full browser)
# extractor for -- see extractors/provider/*.py. classify_hoster() (from
# models/megakino_to/scraper.py, which maintains the authoritative
# domain-substring -> provider-name table) can recognize a few more names
# than this: Streamtape/Luluvdo/LoadX are known names but their extractors
# are unimplemented stubs, VeeV needs a multi-second headless-browser
# resolution and has its own dedicated download path elsewhere in the app,
# and Hanime is a site-native player, not a third-party embed host. For all
# of those, Direct Link falls back to generic yt-dlp probing instead of
# trying (and failing, or being too slow) to resolve them here.
FAST_PROVIDERS = {"VOE", "Vidoza", "Vidmoly", "Vidara", "Vidavaca", "Filemoon", "Doodstream"}


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
