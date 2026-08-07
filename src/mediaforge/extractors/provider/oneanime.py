"""OneAnime video hoster extractor (my.1anime.site) -- used by 9anime.or.at.

9anime moved off MegaPlay: its server list now offers a single entry labelled
"HD" whose embed points at ``https://my.1anime.site/play/<hash>`` -- the site's
own player rather than a third-party hoster. (The MegaPlay extractor next door
is kept: it is a different host and 9anime may serve it again, or another site
may embed it.)

The good news is that this one needs no browser and no JS reversing, unlike
MegaPlay:

    /play/<hash>   -> a small Plyr page carrying
                      <source src="https://my.1anime.site/stream/<hash>"
                              type="video/mp4">
    /stream/<hash> -> HTTP 302 to the real .mp4

Two things the caller must get right, both verified against the live host:

* ``/stream/<hash>`` answers **403** without a Referer, so
  ``PROVIDER_HEADERS_D/_W["OneAnime"]`` set one and the download pipeline
  passes them through. The embed page itself wants the *9anime* Referer (it is
  the embedding site); the stream wants the *player's own* origin.
* The result is a progressive MP4, not HLS. That is fine: the download path
  hands whatever URL it gets to ffmpeg/yt-dlp, both of which read plain MP4
  perfectly well -- it simply means no variant selection.

The redirect is deliberately NOT followed here. The signed-looking filename it
lands on is short-lived, while ``/stream/<hash>`` is stable, and every consumer
(ffmpeg, yt-dlp, the stream proxy) follows redirects on its own.

Used by: models/nineanime_to/episode.py, via the generic host-based dispatch in
extractors.get_direct_link_for() -- 9anime labels this hoster "HD", so the
label is useless and only the resolved host identifies it (see
extractors.HOST_PROVIDER_MAP).
"""
import logging
import re

try:
    from ...config import GLOBAL_SESSION, NINEANIME_BASE_URL
except ImportError:  # pragma: no cover - direct execution
    from mediaforge.config import GLOBAL_SESSION, NINEANIME_BASE_URL

logger = logging.getLogger(__name__)

# The embed page is served by the player host but only for the site that
# embeds it, so the Referer has to be 9anime, not my.1anime.site.
def _embed_headers():
    return {
        "Referer": NINEANIME_BASE_URL.rstrip("/") + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


# <source src="https://my.1anime.site/stream/<hash>" type="video/mp4">
# Attribute order is not assumed: some responses put type= first.
_SOURCE_RE = re.compile(
    r"""<source\b[^>]*\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)

# Fallback for a page that inlines the stream path without a <source> element
# (the player is swapped out from time to time; this keeps a cosmetic change
# from breaking extraction outright).
_STREAM_PATH_RE = re.compile(
    r"""["'](https?://[^"']*/stream/[A-Za-z0-9]+)["']""",
    re.IGNORECASE,
)


def get_direct_link_from_oneanime(embed_url, headers=None, timeout=20):
    """Return the direct video URL for a my.1anime.site ``/play/<hash>`` embed.

    Raises ValueError when the page carries no playable source -- which is also
    what an episode removed upstream looks like, and what the caller's provider
    fallback chain reacts to.
    """
    if not embed_url:
        # The empty-string call is the availability probe in
        # web/runtime_state.py's _get_working_providers(): raising anything
        # other than NotImplementedError marks this extractor as working.
        raise ValueError("No embed URL given")

    req_headers = dict(_embed_headers())
    if headers:
        req_headers.update(headers)

    resp = GLOBAL_SESSION.get(embed_url, headers=req_headers, timeout=timeout,
                              allow_redirects=True)
    resp.raise_for_status()
    html = resp.text or ""

    match = _SOURCE_RE.search(html) or _STREAM_PATH_RE.search(html)
    if not match:
        raise ValueError(f"No video source found in OneAnime embed page: {embed_url}")

    url = match.group(1).strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        from urllib.parse import urlparse
        parsed = urlparse(embed_url)
        url = f"{parsed.scheme}://{parsed.netloc}{url}"
    logger.debug("OneAnime: resolved %s -> %s", embed_url, url)
    return url


if __name__ == "__main__":
    link = input("Enter OneAnime (my.1anime.site) Link: ").strip()
    print("Direct link:", get_direct_link_from_oneanime(link))
