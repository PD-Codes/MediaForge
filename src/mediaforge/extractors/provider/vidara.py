"""VIDARA stream extractor (vidara.to / vidaraa.cc / vidaratem.so / vidaarax.* …).

The VIDARA embed page fetches the actual stream from a small JSON API:
    POST <host>/api/stream   body {"filecode": <id>, "device": "web"}
    -> {"streaming_url": "<master.m3u8>", "thumbnail": …, "subtitles": …}

Unlike the original single-domain implementation, the API host is taken from
the embed URL itself, so every VIDARA mirror domain works (VIDARA rotates
domains frequently). The same scheme powers the Vidavaca clone (vidavaca.py).

Used by: dispatched generically via extractors.provider_functions (key
"get_direct_link_from_vidara"); see the provider alias table in
models/megakino_to/scraper.py (("vidara", "Vidara"), ("vidaar", "Vidara")
for vidaarax.* mirrors) and the generic provider dispatch in
models/megakino_to/{episode,movie}.py.
"""
import json
import logging
import re
import threading
import time

try:
    from ...config import GLOBAL_SESSION, DEFAULT_USER_AGENT
    from ..subtitle_parse import absolutize, tracks_from_config
except ImportError:
    from mediaforge.config import GLOBAL_SESSION, DEFAULT_USER_AGENT
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_config

logger = logging.getLogger(__name__)

# …/e/<filecode> or …/v/<filecode> ; query string optional and ignored for the id
_EMBED_PATTERN = re.compile(r"^(https?://[^/]+)/(?:e|v)/([A-Za-z0-9_\-]+)", re.IGNORECASE)


def _split_embed(embed_url):
    """Return (host_origin, filecode, leftover_query) from a VIDARA embed URL."""
    m = _EMBED_PATTERN.match(embed_url or "")
    if not m:
        raise ValueError(f"Cannot extract filecode from VIDARA URL: {embed_url}")
    host, filecode = m.group(1), m.group(2)
    query = embed_url.split("?", 1)[1] if "?" in embed_url else ""
    return host, filecode, query


# The last /api/stream answer per embed URL. The direct-link path always asks
# again (a fresh token); the subtitle and preview paths reuse it for a while --
# they used to POST the identical request again right after every download.
_DATA_TTL = 600.0
_DATA_MAX = 128
_data_cache = {}              # embed URL -> (stored_at, payload)
_data_lock = threading.Lock()


def get_stream_data(embed_url, headers=None, timeout=20, reuse=False):
    """Call the VIDARA /api/stream endpoint and return the parsed JSON payload.

    Domain-agnostic: the API host is derived from the embed URL, so every
    VIDARA / Vidavaca mirror works. *reuse* accepts an answer from the last
    few minutes (see _data_cache).
    """
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")
    if reuse:
        with _data_lock:
            hit = _data_cache.get(embed_url)
        if hit and time.monotonic() - hit[0] < _DATA_TTL:
            return hit[1]

    host, filecode, query = _split_embed(embed_url)
    api_url = host + "/api/stream" + (("?" + query) if query else "")
    req_headers = {
        "User-Agent": headers.get("User-Agent", DEFAULT_USER_AGENT) if headers else DEFAULT_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": host,
        "Referer": embed_url,
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        resp = GLOBAL_SESSION.post(
            api_url, data=json.dumps({"filecode": filecode, "device": "web"}),
            headers=req_headers, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise ValueError(f"VIDARA API request failed for {embed_url}: {exc}") from exc
    with _data_lock:
        _data_cache[embed_url] = (time.monotonic(), data)
        while len(_data_cache) > _DATA_MAX:
            _data_cache.pop(next(iter(_data_cache)), None)
    return data


def _stream_from_data(data, embed_url, name="VIDARA"):
    """Pull the stream URL out of a /api/stream JSON response, trying known key names.

    Shared with vidavaca.py's near-identical clone API, hence the caller-supplied
    ``name`` used only for error messages.
    """
    streaming_url = (data or {}).get("streaming_url") or (data or {}).get("file") or (data or {}).get("url")
    if not streaming_url:
        raise ValueError(
            f"No streaming_url in {name} API response for {embed_url}. "
            f"Response: {str(data)[:200]}"
        )
    return streaming_url


def get_direct_link_from_vidara(embed_url, headers=None, timeout=20):
    """Return the direct HLS (m3u8) stream URL from a VIDARA embed link."""
    data = get_stream_data(embed_url, headers=headers, timeout=timeout)
    url = _stream_from_data(data, embed_url, "VIDARA")
    logger.debug("VIDARA stream URL extracted: %s…", url[:60])
    return url


def get_subtitles_from_vidara(embed_url, headers=None, timeout=20):
    """Return VIDARA's subtitle tracks as ``[{"url","lang","label"}]``.

    The /api/stream response has carried a "subtitles" field all along;
    get_direct_link_from_vidara() reads only "streaming_url" and drops the
    rest. Reading it here is the only way to get these tracks -- the HLS
    master playlist VIDARA hands out does not list them.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        data = get_stream_data(embed_url, headers=headers, timeout=timeout, reuse=True)
        return absolutize(tracks_from_config(data), embed_url)
    except Exception as exc:
        logger.debug("VIDARA subtitle extraction failed for %s: %s", embed_url, exc)
        return []


def get_preview_image_link_from_vidara(embed_url, headers=None, timeout=20):
    """Return the VIDARA thumbnail/preview image URL, if any."""
    data = get_stream_data(embed_url, headers=headers, timeout=timeout, reuse=True)
    thumb = (data or {}).get("thumbnail")
    if not thumb:
        raise ValueError(f"No thumbnail in VIDARA API response for {embed_url}")
    return thumb


if __name__ == "__main__":
    url = input("Enter VIDARA embed URL: ").strip()
    print("Stream URL:", get_direct_link_from_vidara(url))
