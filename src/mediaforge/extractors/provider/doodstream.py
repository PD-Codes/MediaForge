"""Doodstream (dood.li and dood.* mirrors) extractor.

Strategy: fetch the embed page HTML and regex out two values that Doodstream
embeds in inline JS: the "pass_md5" signing-API URL and a "token". Calling
the pass_md5 URL returns a base CDN URL; the final direct link is assembled
by appending a random string, the token, and the current Unix timestamp as
an expiry -- this matches Doodstream's own short-lived signed-link scheme.

Used by: dispatched generically via extractors.provider_functions
(key "get_direct_link_from_doodstream"); see the provider alias table in
models/megakino_to/scraper.py (("dood", "Doodstream")) and the generic
provider dispatch in models/megakino_to/episode.py and movie.py.
"""
import logging
import random
import re
import time
import warnings
from urllib.parse import urljoin

import niquests
from urllib3.exceptions import InsecureRequestWarning

try:
    from ...config import DEFAULT_USER_AGENT, is_source_unavailable
    from ..subtitle_parse import absolutize, tracks_from_text
except ImportError:
    from mediaforge.config import DEFAULT_USER_AGENT, is_source_unavailable
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_text

warnings.simplefilter("ignore", InsecureRequestWarning)

# -----------------------------
# Constants
# -----------------------------
DOODSTREAM_BASE_URL = "https://dood.li"
RANDOM_STRING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PASS_MD5_PATTERN = r"\$\.get\('([^']*\/pass_md5\/[^']*)'"
TOKEN_PATTERN = r"token=([a-zA-Z0-9]+)"


# -----------------------------
# Helper Functions
# -----------------------------
def _get_headers(referer=None):
    """Return headers for Doodstream requests."""
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer or f"{DOODSTREAM_BASE_URL}/",
    }


def _extract_regex(pattern, content, name, url):
    """Extract a regex match or raise ValueError."""
    match = re.search(pattern, content)
    if not match:
        raise ValueError(f"{name} not found in {url}")
    return match.group(1)


def _generate_random_string(length=10):
    """Generate a random alphanumeric string."""
    return "".join(random.choices(RANDOM_STRING_CHARS, k=length))


def _get_embed_page(embed_url, headers=None):
    """Fetch HTML content of the embed page."""
    headers = headers or _get_headers()
    resp = niquests.get(embed_url, headers=headers, verify=True)
    resp.raise_for_status()
    html = resp.text
    if is_source_unavailable(html, resp.status_code):
        raise ValueError("Doodstream: Video nicht verfügbar oder wurde entfernt.")
    return html


def _get_pass_md5_url(embed_html, embed_url):
    """Extract the pass_md5 signing-API URL embedded in the embed page's inline JS.

    The embed page calls this URL client-side (via jQuery ``$.get``) to fetch
    the base CDN URL used to build the final direct link.
    """
    pass_md5_url = _extract_regex(
        PASS_MD5_PATTERN, embed_html, "pass_md5 URL", embed_url
    )
    if not pass_md5_url.startswith("http"):
        pass_md5_url = urljoin(DOODSTREAM_BASE_URL, pass_md5_url)
    return pass_md5_url


def _get_token(embed_html, embed_url):
    """Extract the signing token embedded in the embed page's inline JS.

    This token is appended as a query parameter on the final direct link and
    is validated server-side against the requested expiry.
    """
    return _extract_regex(TOKEN_PATTERN, embed_html, "token", embed_url)


# -----------------------------
# Main Doodstream Functions
# -----------------------------
def get_direct_link_from_doodstream(embed_url):
    """Resolve a Doodstream embed URL into a direct, time-limited video URL.

    Steps: fetch the embed page, pull the pass_md5 URL and token out of its
    inline JS, call pass_md5 to get the CDN base URL, then append a random
    string + token + expiry timestamp to build the final signed link.
    """
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")

    logging.info(f"Extracting Doodstream direct link from: {embed_url}")
    headers = _get_headers(embed_url)

    embed_html = _get_embed_page(embed_url, headers)
    pass_md5_url = _get_pass_md5_url(embed_html, embed_url)
    token = _get_token(embed_html, embed_url)

    md5_resp = niquests.get(pass_md5_url, headers=headers, verify=True)
    md5_resp.raise_for_status()
    video_base_url = md5_resp.text.strip()
    if not video_base_url:
        raise ValueError(f"Empty video base URL returned from {pass_md5_url}")

    random_str = _generate_random_string(10)
    expiry = int(time.time())
    direct_link = f"{video_base_url}{random_str}?token={token}&expiry={expiry}"

    logging.info("Successfully extracted Doodstream direct link")
    return direct_link


def get_subtitles_from_doodstream(embed_url, headers=None):
    """Return Doodstream's subtitle tracks as ``[{"url","lang","label"}]``.

    Doodstream's direct link is a signed CDN URL with no manifest listing
    caption files at all; any tracks the player shows come from the embed
    page's inline JS. So we re-fetch that page and scan it generically.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        if not embed_url:
            return []
        embed_html = _get_embed_page(embed_url, headers or _get_headers(embed_url))
        return absolutize(tracks_from_text(embed_html), embed_url)
    except Exception as exc:
        logging.debug("Doodstream subtitle extraction failed for %s: %s", embed_url, exc)
        return []


def get_preview_image_link_from_doodstream(embed_url):
    """Get the preview image URL from a Doodstream embed (not implemented)."""
    raise NotImplementedError("Preview image extraction is not implemented yet.")


if __name__ == "__main__":
    # Tested on 2026/01 -> WORKING
    # Example URLs: https://doodsearch.site

    # logging.basicConfig(level=logging.DEBUG)

    link = input("Enter Doodstream Link: ").strip()
    if not link:
        print("Error: No link provided")
        exit(1)

    try:
        print("=" * 25)

        direct_link = get_direct_link_from_doodstream(link)
        print("Direct link:", direct_link)
        print("=" * 25)

        # Preview image extraction not yet implemented
        try:
            preview_img = get_preview_image_link_from_doodstream(link)
            print("Preview image:", preview_img)
        except NotImplementedError:
            print("Preview image: Not implemented")
        print("=" * 25)

        print(
            f"mpv --http-header-fields='Referer: {DOODSTREAM_BASE_URL}/' '{direct_link}'"
        )
        print("=" * 25)

    except Exception as e:
        print("Error:", e)
        exit(1)
