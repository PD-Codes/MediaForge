"""Vidmoly (vidmoly.net / vidmoly.biz / vidmoly.to) video hoster extractor.

Strategy: fetch the embed page HTML, concatenate the contents of all
<script> tags, and regex out the JW-Player-style ``file: '...m3u8...'``
source URL (and, separately, the ``image: '...'`` poster URL for previews).
No JS deobfuscation is needed here -- the URLs sit in plain inline JS.

Some callers hand in a Vidmoly *view* URL instead of an embed URL; see
_normalize_embed_url() below for how that is rewritten first.

Used by: dispatched generically via extractors.provider_functions (key
"get_direct_link_from_vidmoly"); see the provider alias table in
models/megakino_to/scraper.py (("vidmoly", "Vidmoly")) and the generic
provider dispatch used by models/megakino_to/{episode,movie}.py and
models/aniworld_to/episode.py.
"""
import logging
import re

try:
    from ...config import GLOBAL_SESSION, is_source_unavailable
    from ..subtitle_parse import absolutize, tracks_from_text
except ImportError:
    from mediaforge.config import GLOBAL_SESSION, is_source_unavailable
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_text

logger = logging.getLogger(__name__)

# -----------------------------
# Constants
# -----------------------------
FILE_LINK_PATTERN = re.compile(r'file\s*:\s*[\'"]([^\'"]+?\.m3u8[^\'"]*)[\'"]')
PREVIEW_IMAGE_PATTERN = re.compile(
    r'image\s*:\s*[\'"]([^\'"]+\.(?:jpg|jpeg|png|webp))[\'"]'
)


# -----------------------------
# Helper Functions
# -----------------------------
def _get_headers():
    """Return headers for Vidmoly requests."""
    return {"Referer": "https://vidmoly.biz"}


def _extract_regex(pattern, content, name, url):
    """Extract regex match or raise ValueError."""
    if not content:
        raise ValueError(f"No HTML content for {url}")
    match = pattern.search(content)
    if not match:
        raise ValueError(f"{name} not found in {url}")
    return match.group(1)


def _extract_script_content(html):
    """Return all script contents concatenated."""
    scripts = re.findall(
        r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE
    )
    return "\n".join(filter(None, scripts))  # join non-empty scripts


# Some sources (e.g. megakino.to) hand out the Vidmoly *view* URL
# (vidmoly.<tld>/v/<id>?…) instead of the embed URL. The player's inline
# ``file:`` m3u8 only appears on the embed page, and only the vidmoly.net
# host currently serves /embed-<id>.html. Normalise to that form.
_VIDMOLY_VIEW_PATTERN = re.compile(r"https?://vidmoly\.[a-z]+/v/([a-z0-9]+)", re.IGNORECASE)


def _normalize_embed_url(url):
    """Rewrite a Vidmoly *view* URL (vidmoly.<tld>/v/<id>) to its embed-page equivalent."""
    if not url:
        return url
    m = _VIDMOLY_VIEW_PATTERN.match(url)
    if m:
        return f"https://vidmoly.net/embed-{m.group(1)}.html"
    return url


# -----------------------------
# Main Vidmoly Functions
# -----------------------------
def get_direct_link_from_vidmoly(embed_url):
    """Get direct Vidmoly video link."""
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")

    embed_url = _normalize_embed_url(embed_url)
    resp = GLOBAL_SESSION.get(embed_url, headers=_get_headers())
    resp.raise_for_status()
    html = resp.text

    if is_source_unavailable(html, resp.status_code):
        raise ValueError("Vidmoly: Video nicht verfügbar oder wurde entfernt.")

    script_content = _extract_script_content(html)
    return _extract_regex(
        FILE_LINK_PATTERN, script_content, "Direct video URL", embed_url
    )


def get_subtitles_from_vidmoly(embed_url, headers=None):
    """Return Vidmoly's subtitle tracks as ``[{"url","lang","label"}]``.

    Vidmoly's player config is plain inline JS, and its JW-Player
    ``tracks:`` array is the only place the caption files are named -- the
    m3u8 the direct-link path returns does not reference them. So we re-fetch
    the embed page and hand the same concatenated script text to the generic
    parser.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        if not embed_url:
            return []
        embed_url = _normalize_embed_url(embed_url)
        resp = GLOBAL_SESSION.get(embed_url, headers=headers or _get_headers())
        resp.raise_for_status()
        html = resp.text

        script_content = _extract_script_content(html)
        tracks = tracks_from_text(script_content)
        if not tracks:
            tracks = tracks_from_text(html)
        return absolutize(tracks, embed_url)
    except Exception as exc:
        logger.debug("Vidmoly subtitle extraction failed for %s: %s", embed_url, exc)
        return []


def get_preview_image_link_from_vidmoly(embed_url):
    """Get Vidmoly preview image URL."""
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")

    embed_url = _normalize_embed_url(embed_url)
    resp = GLOBAL_SESSION.get(embed_url, headers=_get_headers())
    resp.raise_for_status()
    html = resp.text

    script_content = _extract_script_content(html)
    return _extract_regex(
        PREVIEW_IMAGE_PATTERN, script_content, "Preview image URL", embed_url
    )


if __name__ == "__main__":
    # Tested on 2026/02/18 -> WORKING
    # Example: https://vidmoly.net/embed-zquo82b8dm1k.html

    link = input("Enter Vidmoly Link: ").strip()
    if not link:
        print("Error: No link provided")
        exit(1)

    try:
        print("=" * 25)

        direct_link = get_direct_link_from_vidmoly(link)
        print("Direct link:", direct_link)
        print("=" * 25)

        preview_img = get_preview_image_link_from_vidmoly(link)
        print("Preview image:", preview_img)
        print("=" * 25)

        print(
            f'mpv --http-header-fields="Referer: https://vidmoly.biz" "{direct_link}"'
        )
        print("=" * 25)

    except Exception as e:
        print("Error:", e)
        exit(1)
