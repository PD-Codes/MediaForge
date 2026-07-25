"""Vidavaca video hoster extractor (vidavaca.net …).

Vidavaca is a VIDARA clone using the identical jwplayer + /api/stream scheme,
so extraction is delegated to the shared helpers in vidara.py.

Used by: dispatched generically via extractors.provider_functions (key
"get_direct_link_from_vidavaca"); see the provider alias table in
models/megakino_to/scraper.py (("vidavaca", "Vidavaca")) and the generic
provider dispatch in models/megakino_to/{episode,movie}.py.
"""
import logging

try:
    from .vidara import get_stream_data as _get_stream_data
    from .vidara import _stream_from_data
    from ..subtitle_parse import absolutize, tracks_from_config
except ImportError:
    from mediaforge.extractors.provider.vidara import get_stream_data as _get_stream_data
    from mediaforge.extractors.provider.vidara import _stream_from_data
    from mediaforge.extractors.subtitle_parse import absolutize, tracks_from_config

logger = logging.getLogger(__name__)


def get_direct_link_from_vidavaca(embed_url, headers=None, timeout=20):
    """Return the direct HLS (m3u8) stream URL for a Vidavaca embed link."""
    data = _get_stream_data(embed_url, headers=headers, timeout=timeout)
    return _stream_from_data(data, embed_url, "Vidavaca")


def get_subtitles_from_vidavaca(embed_url, headers=None, timeout=20):
    """Return Vidavaca's subtitle tracks as ``[{"url","lang","label"}]``.

    Same /api/stream payload as VIDARA: the subtitle list is in the JSON the
    direct-link path throws away, and never in the HLS master playlist.

    Returns [] on any failure -- subtitles must never break a download.
    """
    try:
        data = _get_stream_data(embed_url, headers=headers, timeout=timeout)
        return absolutize(tracks_from_config(data), embed_url)
    except Exception as exc:
        logger.debug("Vidavaca subtitle extraction failed for %s: %s", embed_url, exc)
        return []


def get_preview_image_link_from_vidavaca(embed_url, headers=None, timeout=20):
    """Return the Vidavaca thumbnail/preview image URL, if any."""
    data = _get_stream_data(embed_url, headers=headers, timeout=timeout)
    thumb = (data or {}).get("thumbnail")
    if not thumb:
        raise ValueError(f"No thumbnail in Vidavaca API response for {embed_url}")
    return thumb


if __name__ == "__main__":
    link = input("Enter Vidavaca Link: ").strip()
    print("Direct link:", get_direct_link_from_vidavaca(link))
