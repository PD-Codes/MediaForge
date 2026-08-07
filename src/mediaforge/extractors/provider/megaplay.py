"""MegaPlay (megaplay.buzz) video hoster extractor -- used by 9anime.

MegaPlay's embed page (``https://megaplay.buzz/stream/mal/<malid>/<ep>/<sub|dub>``)
only serves real content when the request's Referer is the site that embeds
it (9anime.or.at); requested bare it answers a 410 "removed" page instead of
the player. The actual HLS master playlist URL is never present in the raw
HTML either -- it's assembled client-side by a heavily obfuscated player
bundle (``e1-player.min.js``) and only exists as a JS value (readable via the
page's own ``jwplayer().getPlaylist()`` once the player has initialized).

There is no JS-decoding trick worth reversing here (same call the project
already made for VeeV, see extractors/provider/veev.py): a headless Playwright
(patchright) browser loads the embed page with the correct Referer and we
read ``jwplayer().getPlaylist()`` directly out of the page.

Unlike VeeV, the resolved master.m3u8 URL IS a plain, unauthenticated fetch
once you have it (verified: no cookies or special headers needed beyond a
Referer) -- the long hash segments in its path are the token. So, unlike
VeeV, no dedicated download_from_megaplay() is needed: the returned URL goes
through the same generic ffmpeg/yt-dlp HLS pipeline every other hoster's
stream_url does, using PROVIDER_HEADERS_D["Megaplay"]/_W["Megaplay"] for the
Referer.

Used by: models/nineanime_to/episode.py (only site that embeds MegaPlay).
"""
import logging

try:
    from ._jwplayer_embed import EmbedCache, resolve_jwplayer_embed
    from ...config import DEFAULT_USER_AGENT
except ImportError:  # pragma: no cover - direct execution
    from mediaforge.extractors.provider._jwplayer_embed import EmbedCache, resolve_jwplayer_embed
    from mediaforge.config import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

_cache = EmbedCache()

_REFERER = "https://9anime.or.at/"


def _resolve(embed_url: str, timeout_ms: int = 25_000) -> dict | None:
    """Load *embed_url* in a headless browser and return {"url", "tracks"},
    or None if neither the player nor the network produced a source.

    Shares its implementation with EchoVideo (see _jwplayer_embed.py): both
    sites hide the playlist behind the same kind of obfuscated bundle, and both
    were reading it out of ``jwplayer().getPlaylist()`` alone -- which reports
    nothing when the player fails to initialise in headless Chromium, even
    though the page requested the stream just fine. The request itself is
    watched too now.
    """
    cached = _cache.get(embed_url)
    if cached is not None:
        return cached
    result = resolve_jwplayer_embed(
        embed_url, referer=_REFERER, label="MegaPlay",
        timeout_ms=timeout_ms, user_agent=DEFAULT_USER_AGENT,
    )
    _cache.set(embed_url, result)
    return result


def get_direct_link_from_megaplay(embed_url: str) -> str:
    """Resolve *embed_url* to its HLS master.m3u8 URL.

    Downstream consumption goes through the generic ffmpeg/yt-dlp pipeline
    (like every other hoster's stream_url) with PROVIDER_HEADERS_D/_W's
    "Megaplay" Referer -- no cookie/session replay needed, see module
    docstring.
    """
    if not embed_url:
        raise ValueError("Embed URL darf nicht leer sein")
    data = _resolve(embed_url)
    if not data or not data.get("url"):
        raise ValueError(f"MegaPlay: Keine Videoquelle gefunden ({embed_url})")
    return data["url"]


def get_subtitles_from_megaplay(embed_url: str, headers: dict | None = None) -> list:
    """Subtitle tracks (.vtt) MegaPlay's player loads alongside the video.

    Same contract as every other extractor's get_subtitles_from_<provider>:
    returns [] rather than raising on any failure -- subtitles are a
    convenience and must never break a download. *headers* is accepted (and
    ignored) only so extractors.get_subtitles_for()'s generic dispatch, which
    tries a headers kwarg before falling back to none, works unchanged.
    """
    if not embed_url:
        return []
    try:
        data = _resolve(embed_url)
    except Exception as exc:
        logger.debug("MegaPlay: subtitle lookup failed for %s: %s", embed_url, exc)
        return []
    if not data:
        return []
    return list(data.get("tracks") or [])
