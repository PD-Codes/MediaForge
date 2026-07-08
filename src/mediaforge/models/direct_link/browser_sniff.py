"""Generic, site-agnostic fallback for the Direct Link discovery pipeline
(models/direct_link/probe.py): render *any* pasted URL in a real (headless)
browser and watch its network traffic for a raw media response (.m3u8/.mp4).

Unlike a dedicated per-site scraper, this doesn't know anything about a
specific site's markup or API -- it just loads the page the way a real
visitor's browser would, nudges playback with a couple of clicks (autoplay
is frequently blocked, or hidden behind a "click to play" overlay), and
returns whatever media URL the page's own player ends up requesting. This
is the same category of technique already used elsewhere in the app for
VeeV (extractors/provider/veev.py's CDN-sniffing) and for CAPTCHA solving
(playwright/captcha.py), just generalized to work for any page instead of
one specific host's markup.

Used as the LAST step in probe.py's discover_and_resolve(), after the
cheap static checks (self-is-embed-link, static-HTML-scan) have failed to
find anything -- launching a browser is comparatively slow (several
seconds), so it's only worth it once nothing faster has worked.
"""
import re

from ...logger import get_logger
from .resolver import DIRECT_LINK_USER_AGENT

logger = get_logger(__name__)

_MEDIA_URL_RE = re.compile(r"\.m3u8(\?|$)|\.mp4(\?|$)", re.IGNORECASE)
_NOISE_HINTS = ("adsystem", "banner", "/ads/", "doubleclick", "googlesyndication")


def sniff_media_url(url, timeout=20):
    """Load *url* in a headless browser and return (stream_url, headers) for
    the first .m3u8/.mp4 network request observed, or None if nothing shows
    up within *timeout* seconds (no player on the page, the browser couldn't
    load it, or patchright isn't installed).

    *headers* carries a Referer of the original page, since most CDNs
    reject a request that doesn't look like it came from the embed page
    itself -- the same reasoning as resolver.py's per-host PROVIDER_HEADERS_D.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        logger.debug("[DirectLink] patchright not installed -- skipping browser-based sniff")
        return None

    media_url = None

    def _on_request(request):
        nonlocal media_url
        if media_url:
            return
        req_url = request.url
        low = req_url.lower()
        if not _MEDIA_URL_RE.search(low):
            return
        if any(hint in low for hint in _NOISE_HINTS):
            return
        media_url = req_url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_context(ignore_https_errors=True).new_page()
                page.on("request", _on_request)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except Exception as e:
                    logger.debug(f"[DirectLink] browser-sniff: page load failed for {url}: {e}")
                    return None

                def _smart_sleep(ms=1500, step=100):
                    waited = 0
                    while not media_url and waited < ms:
                        page.wait_for_timeout(step)
                        waited += step

                # Give player scripts / iframe overlays time to render, short-circuiting as soon as stream is seen
                _smart_sleep(1200)

                # Nudge playback: many players only issue the real media
                # request once the player area is clicked (autoplay blocked,
                # or a "click to play" overlay sits on top of the <video>).
                for _ in range(2):
                    if media_url:
                        break
                    try:
                        page.mouse.click(640, 360)
                    except Exception:
                        pass
                    _smart_sleep(800)

                _smart_sleep(max(0, (timeout * 1000) - 2800))
            finally:
                browser.close()
    except Exception as e:
        logger.warning(f"[DirectLink] browser-sniff failed for {url}: {e}")
        return None

    if not media_url:
        return None

    headers = {"User-Agent": DIRECT_LINK_USER_AGENT, "Referer": url}
    return media_url, headers
