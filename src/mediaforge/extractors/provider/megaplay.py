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
import threading

logger = logging.getLogger(__name__)

# embed_url -> {"url": m3u8, "tracks": [{"url","lang","label"}]}
_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}

_REFERER = "https://9anime.or.at/"

# JS run inside the loaded embed page once the player has initialized, to
# pull the resolved playlist straight out of JWPlayer's own state instead of
# re-implementing the obfuscated bundle that put it there.
_EXTRACT_JS = r"""
() => {
  try {
    if (!window.jwplayer) return null;
    const pl = jwplayer().getPlaylist();
    if (!pl || !pl.length) return null;
    const item = pl[0];
    const src = (item.sources || []).find(s => s.type === 'hls') || (item.sources || [])[0];
    if (!src || !src.file) return null;
    const tracks = (item.tracks || [])
      .filter(t => t.kind === 'captions' && t.file)
      .map(t => ({ url: t.file, lang: (t.label || '').slice(0, 2).toLowerCase(), label: t.label || '' }));
    return { url: src.file, tracks };
  } catch (e) {
    return null;
  }
}
"""


def _resolve(embed_url: str, timeout_ms: int = 20_000) -> dict | None:
    """Load *embed_url* in a headless browser (with the required Referer) and
    return {"url", "tracks"}, or None if the player never produced a source
    (dead/removed link -- MegaPlay embeds rot quickly, see module docstring's
    410 case)."""
    with _cache_lock:
        cached = _cache.get(embed_url)
    if cached is not None:
        return cached

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "patchright nicht installiert. "
            "Installieren mit: pip install patchright && patchright install chromium"
        )

    result = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            try:
                # Playwright's own `referer` kwarg is what gets MegaPlay past
                # its Referer check -- a plain goto() without it lands on the
                # site's "410 removed" error page (reproduced with curl during
                # research: identical page, identical response either way).
                page.goto(embed_url, referer=_REFERER, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector("#megaplay-player, video", timeout=8000)
            except Exception as e:
                logger.debug("MegaPlay: page load issue for %s: %s", embed_url, e)

            for _ in range(20):
                try:
                    data = page.evaluate(_EXTRACT_JS)
                except Exception:
                    data = None
                if data and data.get("url"):
                    result = data
                    break
                page.wait_for_timeout(500)
        finally:
            browser.close()

    if result:
        with _cache_lock:
            _cache[embed_url] = result
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
