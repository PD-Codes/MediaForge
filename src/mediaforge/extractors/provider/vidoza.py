"""Vidoza (videzz.net and other Vidoza mirrors) video hoster extractor.

Strategy: fetch the embed page HTML and, when it contains a "sourcesCode:"
JW-Player config block, regex out the plain-text ``src: "..."`` video URL
(and, separately, the ``poster: "..."`` thumbnail URL for previews). No
JS deobfuscation is required -- the URLs are inline, unobfuscated strings.

Used by: dispatched generically via extractors.provider_functions (key
"get_direct_link_from_vidoza"); see the provider alias table in
models/megakino_to/scraper.py (("vidoza", "Vidoza")) and the generic
provider dispatch in models/megakino_to/{episode,movie}.py.
"""
import logging
import re

import niquests

try:
    from ...config import DEFAULT_USER_AGENT, GLOBAL_SESSION, is_source_unavailable
    from ..subtitle_parse import absolutize, tracks_from_text
except ImportError:
    from mediaforge.config import DEFAULT_USER_AGENT, GLOBAL_SESSION, is_source_unavailable
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_text

logger = logging.getLogger(__name__)


# Compile regex pattern once for better performance
SOURCE_LINK_PATTERN = re.compile(r'src:\s*"([^"]+)"')
IMAGE_LINK_PATTERN = re.compile(r'poster:\s*"([^"]+)"')


def get_direct_link_from_vidoza(embeded_vidoza_link):
    """Fetch the Vidoza embed page and return the direct video URL from its JW-Player config."""
    try:
        resp = GLOBAL_SESSION.get(
            embeded_vidoza_link, headers={"User-Agent": DEFAULT_USER_AGENT}
        )
        resp.raise_for_status()
        html = resp.text

        if is_source_unavailable(html, resp.status_code):
            raise ValueError("Vidoza: Video nicht verfügbar oder wurde entfernt.")

        if "sourcesCode:" in html:
            match = SOURCE_LINK_PATTERN.search(html)
            if match:
                return match.group(1)

    except niquests.RequestException as err:
        raise ValueError(f"Failed to fetch Vidoza page: {err}") from err


def get_subtitles_from_vidoza(embeded_vidoza_link, headers=None):
    """Return Vidoza's subtitle tracks as ``[{"url","lang","label"}]``.

    The captions sit next to the ``src:``/``poster:`` entries in the same
    inline JW-Player config the direct-link path regexes one value out of;
    nothing about them reaches the stream manifest. Re-fetch the page and let
    the generic parser read the whole thing.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        resp = GLOBAL_SESSION.get(
            embeded_vidoza_link, headers=headers or {"User-Agent": DEFAULT_USER_AGENT}
        )
        resp.raise_for_status()
        return absolutize(tracks_from_text(resp.text), embeded_vidoza_link)
    except Exception as exc:
        logger.debug("Vidoza subtitle extraction failed for %s: %s", embeded_vidoza_link, exc)
        return []


def get_preview_image_link_from_vidoza(embeded_vidoza_link):
    """Fetch the Vidoza embed page and return its poster/preview image URL."""
    try:
        resp = GLOBAL_SESSION.get(
            embeded_vidoza_link, headers={"User-Agent": DEFAULT_USER_AGENT}
        )
        resp.raise_for_status()
        html = resp.text

        if "sourcesCode:" in html:
            match = IMAGE_LINK_PATTERN.search(html)
            if match:
                return match.group(1)

    except niquests.RequestException as err:
        raise ValueError(f"Failed to fetch Vidoza page: {err}") from err


if __name__ == "__main__":
    # Tested on 2026/01/27 -> WORKING
    # Example: https://videzz.net/embed-xneznizpludf.html

    # logging.basicConfig(level=logging.DEBUG)

    link = input("Enter Vidoza Link: ").strip()
    if not link:
        print("Error: No link provided")
        exit(1)

    try:
        print("=" * 25)

        direct_link = get_direct_link_from_vidoza(link)
        print("Direct link:", direct_link)
        print("=" * 25)

        print("Preview image:", get_preview_image_link_from_vidoza(link))
        print("=" * 25)

        print(
            f'mpv "{direct_link}" --http-header-fields=User-Agent: "{DEFAULT_USER_AGENT}"'
        )

        print("=" * 25)
    except ValueError as e:
        print("Error:", e)
