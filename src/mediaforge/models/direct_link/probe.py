"""Probe a raw URL with yt-dlp (no download) to list its available quality
variants, for the "Direct Link" feature's format-picker modal (GitHub issue #8).

Unlike the scraper sites (aniworld.to, s.to, ...), a direct link has no
series/season/episode structure and no dub/sub language concept -- the user
just pastes a raw stream URL (typically an .m3u8 HLS master playlist, but
anything yt-dlp supports works) and picks a resolution. This module only
does the read-only "what's in here" step; the actual download is
models/direct_link/episode.py, invoked once the user has picked a variant
via web/routes/direct_link.py's POST /api/direct-link/download.
"""

import re

from ...logger import get_logger
from ..common.common import _YtdlpQuietLogger
from ..megakino_to.scraper import classify_hoster
from .browser_sniff import sniff_media_url
from .resolver import DIRECT_LINK_USER_AGENT, FAST_PROVIDERS, resolve_stream_for_provider

logger = get_logger(__name__)


def _clean_title(raw):
    """Strip filesystem-unsafe characters from a suggested filename."""
    return re.sub(r'[<>:"/\\|?*]', "", raw or "").strip()


def detect_fast_provider(url):
    """Return the canonical provider name (e.g. "VOE") if *url* is a known,
    fast-to-resolve embed host, else None."""
    name = classify_hoster(url)
    return name if name in FAST_PROVIDERS else None


# Matches a plain http(s) URL inside raw HTML/JS text, stopping at the
# nearest quote/whitespace/angle-bracket. Run AFTER un-escaping JSON-style
# "\/" sequences (see find_candidate_urls_in_page), so URLs embedded inside
# an inline <script> block's JSON blob are matched too, not just plain
# href/src attributes.
_URL_IN_HTML = re.compile(r"""https?://[^\s"'<>\\]+""")


def find_candidate_urls_in_page(url, timeout=15):
    """Fetch *url* as a plain HTML page (no JS execution) and scan it for
    links to fast-resolvable embed hosts -- e.g. an
    ``<iframe src="https://voe.sx/e/...">``, a plain ``<a href="...">``, or a
    URL sitting inside an inline ``<script>`` block's JSON/JS.

    This is a best-effort STATIC scan only: it cannot find a hoster link
    that a site only reveals after a click-driven AJAX call (the link
    simply isn't present in the HTML at all in that case).

    Returns a list of (provider_name, candidate_url) tuples, in the order
    found, or an empty list if the page can't be fetched or no known
    hoster link appears in it. Deliberately returns ALL matches (not just
    the first) so the caller can try each until one actually resolves --
    a page can link to several mirrors and some may be dead.
    """
    import requests

    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": DIRECT_LINK_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.debug(f"[DirectLink] Could not fetch page for embed-link scan: {url}: {e}")
        return []

    # Un-escape JSON-style "\/" so URLs embedded inside an inline <script>
    # block's JSON/JS string literals (a common way sites pass the player
    # config to the frontend) are matched by _URL_IN_HTML too.
    html = html.replace("\\/", "/")

    seen = set()
    candidates = []
    for candidate in _URL_IN_HTML.findall(html):
        candidate = candidate.rstrip("\\/,.;)")
        name = classify_hoster(candidate)
        if name in FAST_PROVIDERS and candidate not in seen:
            seen.add(candidate)
            candidates.append((name, candidate))

    return candidates


def discover_and_resolve(url, timeout=12):
    """Full Direct Link discovery pipeline, used identically by the probe
    step and by DirectLinkEpisode's fresh re-resolution at download time.

    Tries, in order, until one actually resolves successfully (a candidate
    "looking like" a supported host is not enough -- individual mirrors are
    frequently dead/expired, so every step below retries across ALL of its
    candidates rather than giving up after the first match):

    1. *url* itself, if it's already a known embed-host link.
    2. Any other page: scan its static HTML for embedded hoster links
       (find_candidate_urls_in_page) and try each one.
    3. Last resort: render the page in a real (headless) browser and watch
       its network traffic for a raw .m3u8/.mp4 request (browser_sniff.py)
       -- catches players that build their video URL client-side (in JS)
       rather than embedding a plain link anywhere in the static HTML.

    Always returns a usable (provider_name_or_None, stream_url, headers)
    tuple -- on total failure it falls back to (None, url, {"User-Agent":
    ...}), so callers can still hand `url` to yt-dlp's own generic
    extraction.
    """
    default_headers = {"User-Agent": DIRECT_LINK_USER_AGENT}

    name = detect_fast_provider(url)
    if name:
        try:
            stream_url, headers = resolve_stream_for_provider(name, url, timeout=timeout)
            logger.warning(f"[DirectLink] {url} is itself a known embed link ({name}), resolved successfully")
            return name, stream_url, headers
        except Exception as e:
            logger.warning(f"[DirectLink] {url} is itself a known embed link ({name}) but resolution failed: {e}")

    for cand_name, cand_url in find_candidate_urls_in_page(url, timeout=timeout):
        try:
            stream_url, headers = resolve_stream_for_provider(cand_name, cand_url, timeout=timeout)
            logger.warning(f"[DirectLink] {url}: resolved via embedded {cand_name} link ({cand_url})")
            return cand_name, stream_url, headers
        except Exception as e:
            logger.warning(f"[DirectLink] {url}: embedded {cand_name} link ({cand_url}) failed, trying next candidate: {e}")
            continue

    sniffed = sniff_media_url(url, timeout=timeout)
    if sniffed:
        stream_url, headers = sniffed
        logger.warning(f"[DirectLink] {url}: resolved via browser network-sniff ({stream_url[:100]})")
        return None, stream_url, headers

    logger.warning(f"[DirectLink] No resolvable embed-host link found for {url}; falling back to generic yt-dlp extraction")
    return None, url, default_headers


def probe_direct_link_formats(url):
    """Run yt-dlp's info extraction (no download) against *url* and return a
    simplified, deduplicated list of quality variants.

    If *url* (or a link discovered from it, see discover_and_resolve) is
    recognized as one of MediaForge's already-supported embed hosts (VOE,
    Vidoza, ...), it is resolved to its direct stream URL first (using the
    same host-specific extractor the scraper sites use) and yt-dlp probes
    THAT URL with the matching provider headers -- this covers hosts whose
    embed pages hide the stream behind JavaScript/obfuscation that yt-dlp's
    generic page scraper alone would not find. Anything else (a raw .m3u8
    link, or any other site) is probed as-is; yt-dlp's own generic
    extractor still tries to locate embedded video links in plain HTML for
    those.

    Returns:
        {
            "title": <suggested filename, "" if unknown>,
            "provider": <detected provider name, or None for a generic link>,
            "formats": [
                # first entry is always the safe "let yt-dlp decide" default
                {"selector": "bestvideo+bestaudio/best", "height": None, "filesize_mb": None, "best": True},
                {"selector": "303+bestaudio", "height": 1080, "filesize_mb": 734, "best": False},
                ...
            ],
        }

    Raises RuntimeError if yt-dlp cannot extract any info at all (invalid/
    unsupported URL, unreachable host, DRM, ...). Individual missing fields
    (no formats found) are NOT an error -- the caller still gets the
    "automatic" fallback entry in that case.
    """
    import yt_dlp

    provider, probe_url, headers = discover_and_resolve(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpQuietLogger(),
        "skip_download": True,
        "http_headers": headers,
        "socket_timeout": 20,
        "nocheckcertificate": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(probe_url, download=False)
    except Exception as e:
        raise RuntimeError(str(e)) from e

    if not info:
        raise RuntimeError("yt-dlp returned no information for this URL.")

    # A playlist/channel URL: direct-link jobs are single-file only, so just
    # use the first entry. Anything more elaborate should go through the
    # normal search instead of Direct Link.
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("This URL is a playlist with no entries.")
        info = entries[0]

    raw_formats = info.get("formats") or []

    # Keep only formats that actually carry video, one per resolution
    # (highest bitrate wins) -- a typical HLS master already has exactly one
    # rendition per resolution, so this mostly just filters out audio-only
    # and subtitle-only tracks rather than deduplicating much.
    best_by_height = {}
    for f in raw_formats:
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        if not height:
            continue
        tbr = f.get("tbr") or 0
        if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
            best_by_height[height] = f

    formats = [
        {"selector": "bestvideo+bestaudio/best", "height": None, "filesize_mb": None, "best": True}
    ]
    for height in sorted(best_by_height.keys(), reverse=True):
        f = best_by_height[height]
        format_id = f.get("format_id")
        if not format_id:
            continue
        # HLS video renditions are frequently video-only (audio is a
        # separate rendition yt-dlp merges in) -- append +bestaudio unless
        # this format already carries its own audio track.
        needs_audio = f.get("acodec") in (None, "none")
        selector = f"{format_id}+bestaudio/{format_id}" if needs_audio else format_id

        size = f.get("filesize") or f.get("filesize_approx")
        formats.append({
            "selector": selector,
            "height": height,
            "filesize_mb": round(size / 1_048_576) if size else None,
            "best": False,
        })

    return {
        "title": _clean_title(info.get("title")),
        "provider": provider,
        "formats": formats,
    }
