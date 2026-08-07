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
import threading

logger = logging.getLogger(__name__)

# embed_url -> {"url": m3u8, "tracks": [{"url","lang","label"}]}
_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}

_REFERER = "https://aniwaves.ru/"

# JS run inside the loaded embed page once the player has initialized, to
# pull the resolved playlist straight out of JWPlayer's own state instead of
# re-implementing the client-side assembly that put it there.
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
    """Load *embed_url* in a headless browser and return {"url", "tracks"},
    or None if the player never produced a source."""
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
                page.goto(embed_url, referer=_REFERER, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector("video", timeout=8000)
            except Exception as e:
                logger.debug("EchoVideo: page load issue for %s: %s", embed_url, e)

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
