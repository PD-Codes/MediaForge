"""Shared JWPlayer-embed resolution for the hosters that assemble their HLS
URL client-side (EchoVideo, MegaPlay).

Both sites hide the playlist behind a large obfuscated bundle: nothing usable
is in the raw HTML, so a headless browser loads the page and we take the
result. What changed here is WHERE the result is taken from.

Reading ``jwplayer().getPlaylist()`` was the original approach and it is kept
-- but only as one of two signals, because it fails in ways that have nothing
to do with the site being broken:

* it needs the player object to exist AND to have finished ``setup()``;
* a headless Chromium without media codecs can leave the player half-
  initialised, so the playlist stays empty even though the page is fine;
* any change to the player wrapper (a different variable, a shadow DOM, a
  newer JWPlayer API) silently returns null.

The network is the more reliable signal: whatever the player does internally,
it has to REQUEST the playlist, and that request is observable no matter how
the URL was assembled. So this module watches requests for an ``.m3u8`` (or
``.mp4``) and uses the first one that looks like content, falling back to the
JS read. Either signal alone is enough.

Used by: extractors/provider/echovideo.py and extractors/provider/megaplay.py.
"""
import logging
import re
import threading

logger = logging.getLogger(__name__)

# Requests that are playlists/segments but not the master we want, or plain
# noise (ads, analytics, the player bundle itself).
_IGNORE_RE = re.compile(
    r"(?:google|doubleclick|facebook|analytics|sentry|cdnbye|jsdelivr|"
    r"googleapis|gstatic|adserver|popads|/ads?/)",
    re.IGNORECASE,
)
# A media playlist (per-variant) rather than the master. Preferred only if no
# master shows up -- ffmpeg/yt-dlp handle either, but the master keeps the
# quality choice.
_VARIANT_HINT_RE = re.compile(r"(?:/\d{3,4}p?/|[?&](?:v|q|res)=\d{3,4})", re.IGNORECASE)

_MEDIA_RE = re.compile(r"\.(m3u8|mp4)(?:[?#]|$)", re.IGNORECASE)

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


class _Sniffer:
    """Collects candidate media URLs seen on the wire, best first."""

    def __init__(self):
        self.master = None
        self.variant = None
        self.other = None

    def note(self, url):
        if not url or _IGNORE_RE.search(url) or not _MEDIA_RE.search(url):
            return
        if url.lower().split("?")[0].endswith(".m3u8"):
            if _VARIANT_HINT_RE.search(url):
                self.variant = self.variant or url
            else:
                self.master = self.master or url
        else:
            self.other = self.other or url

    @property
    def best(self):
        return self.master or self.variant or self.other


def resolve_jwplayer_embed(embed_url, referer, label, timeout_ms=25_000,
                           user_agent=None):
    """Load *embed_url* headless and return ``{"url", "tracks"}`` or None.

    *label* only names the hoster in log lines. *referer* is sent as the
    navigation referer, matching the real embedding flow.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "patchright nicht installiert. "
            "Installieren mit: pip install patchright && patchright install chromium"
        )

    sniffer = _Sniffer()
    js_result = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            # Headless Chromium ships without proprietary codecs, and a player
            # that cannot decode may never leave the "loading" state -- muting
            # and allowing autoplay is what gets it to actually request the
            # stream, which is the signal this whole module is built on.
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--disable-blink-features=AutomationControlled",
        ])
        try:
            ctx_kwargs = {"ignore_https_errors": True}
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.on("request", lambda r: sniffer.note(r.url))
            page.on("response", lambda r: sniffer.note(r.url))

            try:
                page.goto(embed_url, referer=referer, wait_until="domcontentloaded",
                          timeout=timeout_ms)
            except Exception as exc:
                logger.debug("%s: navigation issue for %s: %s", label, embed_url, exc)

            # Nudge the player: several of these wrappers only call setup() on
            # the first interaction, and a click on a page that needs none is
            # harmless.
            for selector in ("video", ".jw-icon-display", "#player", "body"):
                try:
                    page.click(selector, timeout=1500)
                    break
                except Exception:
                    continue

            deadline = timeout_ms
            waited = 0
            step = 500
            while waited < deadline:
                if sniffer.best:
                    break
                try:
                    js_result = page.evaluate(_EXTRACT_JS)
                except Exception:
                    js_result = None
                if js_result and js_result.get("url"):
                    break
                page.wait_for_timeout(step)
                waited += step

            # One last read: even when the network gave us the URL, the JS side
            # is where the subtitle tracks are.
            if js_result is None:
                try:
                    js_result = page.evaluate(_EXTRACT_JS)
                except Exception:
                    js_result = None
        finally:
            try:
                browser.close()
            except Exception:
                pass

    url = (js_result or {}).get("url") or sniffer.best
    if not url:
        logger.debug("%s: neither the player nor the network produced a source for %s",
                     label, embed_url)
        return None
    if (js_result or {}).get("url") and sniffer.best and js_result["url"] != sniffer.best:
        logger.debug("%s: player and network disagree (%s vs %s) -- using the player's",
                     label, js_result["url"], sniffer.best)
    return {"url": url, "tracks": list((js_result or {}).get("tracks") or [])}


class EmbedCache:
    """Tiny per-extractor positive cache (embed_url -> resolved dict)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def set(self, key, value):
        if not value:
            return
        with self._lock:
            self._data[key] = value
