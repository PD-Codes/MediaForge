"""EchoVideo (echovideo.ru / echovideo.to) video hoster extractor -- aniwaves.ru's
"Vidplay" server.

Same shape as extractors/provider/megaplay.py, and for the same reason: the
embed page (``https://play.echovideo.ru/embed-1/<token>``) never puts the
resolved HLS URL in the raw HTML -- it's assembled client-side by the page's
own JWPlayer bundle and only exists as a JS value once the player has
initialized. There is no obfuscation worth reverse-engineering here; a
headless Playwright (patchright) browser loads the embed page and we read
``jwplayer().getPlaylist()`` straight out of it, exactly like MegaPlay.

Two things verified during research that make this *simpler* than MegaPlay:

  * The embed page itself does NOT enforce a Referer check the way MegaPlay's
    does -- navigating to it directly (no Referer) still serves the player.
    Playwright is still called with aniwaves.ru as the referer anyway, purely
    to mirror the real browsing flow and because it costs nothing.
  * The resolved master.m3u8 needs no auth at all (verified with curl: no
    Referer, no cookies) -- like MegaPlay, no dedicated download_from_echovideo()
    is needed, the URL goes through the generic ffmpeg/yt-dlp HLS pipeline
    every other hoster's stream_url does.

aniwaves.ru also offers two other "servers" per episode (labelled "BYFMS"/
"DGHG" in its UI) that both resolve into the same Byse-network backend
filmo.to/filemoon.to already use (see extractors/provider/filemoon.py) --
NOT implemented here: those mirrors rotate across several domains, one of
which sits behind a Cloudflare challenge, and the embed page itself demands
a genuine (non-JS-simulated) click to pass a "confirm you're human" gate.
EchoVideo/Vidplay alone already covers every language aniwaves.ru offers for
a given episode (Sub, Dub and Kor-Dub all listed it in research), so skipping
the Byse-network mirrors costs no content, only redundancy/fallback options.

Used by: models/aniwaves_ru/episode.py (only site that embeds EchoVideo).
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

_REFERER = "https://aniwaves.ru/"


def _resolve(embed_url: str, timeout_ms: int = 25_000) -> dict | None:
    """Load *embed_url* in a headless browser and return {"url", "tracks"},
    or None if neither the player nor the network produced a source.

    The heavy lifting -- and the reason this is no longer a bare
    ``jwplayer().getPlaylist()`` read -- lives in _jwplayer_embed.py: the
    playlist request is watched on the wire as well, so a player that fails to
    initialise in headless Chromium (no proprietary codecs) no longer means
    "no video source found" for a stream the page fetched perfectly well.
    """
    cached = _cache.get(embed_url)
    if cached is not None:
        return cached
    result = resolve_jwplayer_embed(
        embed_url, referer=_REFERER, label="EchoVideo",
        timeout_ms=timeout_ms, user_agent=DEFAULT_USER_AGENT,
    )
    _cache.set(embed_url, result)
    return result


def get_direct_link_from_echovideo(embed_url: str) -> str:
    """Resolve *embed_url* to its HLS master.m3u8 URL.

    Downstream consumption goes through the generic ffmpeg/yt-dlp pipeline
    (like every other hoster's stream_url) with PROVIDER_HEADERS_D/_W's
    "EchoVideo" Referer -- no cookie/session replay needed, see module
    docstring.
    """
    if not embed_url:
        raise ValueError("Embed URL darf nicht leer sein")
    data = _resolve(embed_url)
    if not data or not data.get("url"):
        raise ValueError(f"EchoVideo: Keine Videoquelle gefunden ({embed_url})")
    return data["url"]


def get_subtitles_from_echovideo(embed_url: str, headers: dict | None = None) -> list:
    """Subtitle tracks EchoVideo's player loads alongside the video.

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
        logger.debug("EchoVideo: subtitle lookup failed for %s: %s", embed_url, exc)
        return []
    if not data:
        return []
    return list(data.get("tracks") or [])
