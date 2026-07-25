"""Soft-subtitle support for downloads.

Until this module existed, MediaForge's ``Subtitles`` enum was not about
subtitle *tracks* at all: it selects which pre-rendered video variant a site
offers (German Sub = a video with the subtitles burned into the picture), and
yt-dlp was never asked to fetch a subtitle rendition. Any real, selectable
subtitle track a hoster served was silently discarded -- ``extractors/provider/
vidara.py`` even documents a ``subtitles`` field in its return value that
nothing ever read.

With ``dl_subtitles`` on (the default), every subtitle rendition the source
offers is downloaded alongside the stream and muxed into the finished .mkv as a
soft-sub track with its language tag, so Jellyfin/Plex pick them up and the user
can switch them off. Subtitles are a few hundred KB against a multi-GB video, so
there is no "which languages" question to answer -- everything on offer is taken.

Deliberately hoster-only: no OpenSubtitles or other external lookup, which would
mean an API key, rate limits and hash matching for a feature that is meant to be
a side effect of the download that is happening anyway.

Failure here must never fail a download. A missing or malformed subtitle is a
cosmetic loss; the video is the deliverable. Every entry point returns an empty
result instead of raising.
"""

import os
import re
from pathlib import Path

try:
    from ...logger import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)


# What yt-dlp may drop next to the video. Kept in sync with SUBTITLE_FORMATS
# below; ``.vtt`` is what HLS sources produce in practice.
SUBTITLE_SUFFIXES = (".vtt", ".srt", ".ass", ".ssa")

# Hard ceiling on how many subtitle tracks get muxed in. "All available" is the
# intent, but a handful of sources advertise 40+ machine-translated renditions,
# and every one of them costs an ffmpeg input and a track in the player's menu.
MAX_SUBTITLE_TRACKS = 12

# ISO 639-1 -> ISO 639-2/B, which is what Matroska language tags use. Only the
# languages these sites actually serve; anything unknown is passed through when
# it is already three letters and tagged "und" otherwise, so a track is never
# dropped just because the mapping has a gap.
_LANG_2_TO_3 = {
    "de": "deu", "en": "eng", "ja": "jpn", "fr": "fra", "es": "spa",
    "it": "ita", "pt": "por", "nl": "nld", "pl": "pol", "ru": "rus",
    "tr": "tur", "ar": "ara", "zh": "zho", "ko": "kor", "sv": "swe",
    "da": "dan", "fi": "fin", "no": "nor", "cs": "ces", "hu": "hun",
    "el": "ell", "he": "heb", "hi": "hin", "ro": "ron", "uk": "ukr",
}

# Three-letter spellings that differ between ISO 639-2/B and /T. Normalised so
# a "ger" track and a "deu" track do not end up as two separate languages.
_LANG_3_ALIASES = {
    "ger": "deu", "fre": "fra", "dut": "nld", "gre": "ell", "chi": "zho",
    "cze": "ces", "ice": "isl", "mac": "mkd", "may": "msa", "per": "fas",
    "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym", "alb": "sqi",
    "arm": "hye", "baq": "eus", "bur": "mya", "geo": "kat",
}

# yt-dlp language codes as they appear in the file name: "de", "en-US",
# "ger-forced", "de_DE".
_LANG_SPLIT_RE = re.compile(r"[-_]")


def _get_setting(key, default="1"):
    """Read an app setting without importing the web stack at module load.

    Mirrors ``dupecheck._get_setting``: ``models.*`` also runs from the CLI,
    where ``web.db`` (and Flask with it) may not import at all.
    """
    try:
        from ...web.db import get_setting
    except Exception:
        try:
            from mediaforge.web.db import get_setting
        except Exception:
            return default
    try:
        return get_setting(key, default)
    except Exception:
        return default


def subtitles_enabled() -> bool:
    """True when subtitle renditions should be fetched and muxed in.

    Defaults to *on*, unlike the duplicate-handling toggles: this only ever adds
    tracks to a file that is being written anyway, so there is nothing to lose
    by having it on and nothing existing for it to overwrite.
    """
    return str(
        _get_setting("dl_subtitles", os.environ.get("MEDIAFORGE_DL_SUBTITLES", "1"))
    ) == "1"


def ytdlp_subtitle_opts() -> dict:
    """Options to merge into yt-dlp's opts so it writes subtitle sidecars.

    ``writeautomaticsub`` stays off on purpose: those are machine-generated ASR
    captions (a YouTube concept), not the source's own subtitles, and muxing
    them in would present guessed text as if it were a real translation.

    Deliberately does NOT set ``ignoreerrors``. It would cover a failing
    subtitle rendition, but it also suppresses *video* download errors, and
    ``_run_ytdlp_download`` decides success from yt-dlp's return code -- a
    masked failure would be booked as a completed download. yt-dlp already
    treats an unavailable subtitle as a warning rather than a fatal error, so
    the protection was not needed in the first place.
    """
    return {
        "writesubtitles": True,
        "writeautomaticsub": False,
        "subtitleslangs": ["all"],
        "subtitlesformat": "best",
    }


def normalize_lang(code: str) -> str:
    """ISO 639-2/B tag for a yt-dlp language code, or ``und``.

    Handles ``de``, ``de-DE``, ``ger``, ``deu`` and the ``-forced`` suffix that
    some HLS manifests carry.
    """
    if not code:
        return "und"
    base = _LANG_SPLIT_RE.split(str(code).strip().lower())[0]
    if len(base) == 2:
        return _LANG_2_TO_3.get(base, "und")
    if len(base) == 3:
        return _LANG_3_ALIASES.get(base, base)
    return "und"


def is_subtitle_file(path) -> bool:
    """True for a path that looks like a subtitle sidecar.

    Used to keep yt-dlp's "find any file with this stem" fallback in
    ``_run_ytdlp_download`` from grabbing a .vtt and renaming it to .mkv.
    """
    try:
        return Path(path).suffix.lower() in SUBTITLE_SUFFIXES
    except (TypeError, ValueError):
        return False


def collect_subtitle_files(output_path):
    """Subtitle sidecars yt-dlp wrote for *output_path*, as ``[(Path, lang)]``.

    yt-dlp names them ``<stem>.<lang>.<ext>`` off the same outtmpl stem as the
    video, so they are found by globbing rather than by parsing yt-dlp's info
    dict -- which the download path never sees, because it calls
    ``ydl.download()`` and not ``extract_info()``.

    Duplicate languages are collapsed (a source offering both a normal and a
    "forced" German track yields one German track, the first one seen).
    """
    output_path = Path(output_path)
    stem = output_path.with_suffix("").name
    try:
        candidates = sorted(output_path.parent.glob(f"{stem}.*"))
    except OSError as exc:
        logger.debug("[Subtitles] could not scan for sidecars: %s", exc)
        return []

    found = []
    seen = set()
    for candidate in candidates:
        if candidate.suffix.lower() not in SUBTITLE_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size <= 0:
                continue
        except OSError:
            continue
        # "Show.raw_full.de.vtt" -> the part between the video stem and the
        # extension is the language code.
        raw_lang = candidate.with_suffix("").name[len(stem):].lstrip(".")
        lang = normalize_lang(raw_lang)
        if lang in seen:
            continue
        seen.add(lang)
        found.append((candidate, lang))
        if len(found) >= MAX_SUBTITLE_TRACKS:
            logger.info(
                "[Subtitles] capped at %d tracks — further renditions ignored",
                MAX_SUBTITLE_TRACKS,
            )
            break
    return found


def cleanup_subtitle_files(subs):
    """Remove sidecars after they have been muxed in (or after a failure)."""
    for entry in subs or []:
        path = entry[0] if isinstance(entry, tuple) else entry
        try:
            path = Path(path)
            if path.exists():
                path.unlink()
        except OSError:
            pass
