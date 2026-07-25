"""Duplicate handling for downloads: quality upgrades and audio-track merging.

Two checks run whenever an episode is already present on disk. Both are opt-in
via app settings and both default to *off*, so an existing install keeps the
old "already there -> skip" behaviour until the user asks for more.

1. ``dl_quality_upgrade`` -- the file exists and already carries every track the
   job wanted, so the old code returned "skipped". With the toggle on, the
   remote stream is probed first (``ffprobe`` on the hoster URL, no payload
   downloaded) and compared against the file on disk. Only a strictly higher
   video height, or a meaningfully higher bitrate at the same height, counts as
   better; anything unknown counts as *not* better. Being conservative here is
   deliberate -- a false positive re-downloads a multi-GB file and overwrites a
   good copy, a false negative merely keeps the status quo.

2. ``dl_audio_track_merge`` -- the episode was downloaded in another language
   (German Dub) and the current job is a different one (English Dub, German
   Sub). The mux path in ``common.download()`` has always been able to add a
   missing audio track to an existing file, but it only ever looked at
   ``self._episode_path``. With language separation on (or ``{language}`` in the
   naming template) the German Dub file lives under a different folder/name, so
   the English Dub job never saw it and wrote a second, near-duplicate file.
   ``find_existing_variant()`` closes that gap by resolving the same episode
   across the sibling language folders.

Kept out of ``models/common/common.py`` on purpose: that module is already ~1.5k
lines and is imported by every site model, while this one is pure path/probe
logic with no ffmpeg-python dependency.
"""

import os
import re
from pathlib import Path

try:
    from ...languages import LANG_FOLDERS
except ImportError:  # pragma: no cover - direct/script import
    from mediaforge.languages import LANG_FOLDERS

try:
    from ...logger import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)


# Video containers a previous download may have produced. Used when scanning a
# sibling language folder for the same episode; anything else (.nfo, .srt,
# artwork, yt-dlp leftovers) must not be mistaken for the media file.
MEDIA_SUFFIXES = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm")

# A same-height re-download only counts as an upgrade when the new stream is at
# least this much fatter. Hoster bitrates fluctuate by a few percent between
# probes of the very same stream, so a tighter margin would make MediaForge
# re-download the same file forever.
BITRATE_MARGIN = 0.20

# Height difference below which two resolutions are treated as equal. Covers
# 1920x1080 vs 1920x1072 (letterboxed re-encodes) without ever merging 720p
# into 1080p.
HEIGHT_TOLERANCE = 32

# S01E01 / s1e1 / 1x01 -- the episode marker used to line up the same episode
# across language folders whose file names differ (naming template may contain
# {language}).
_EP_TOKEN_RE = re.compile(r"[sS](\d{1,4})[\s._-]*[eE](\d{1,4})")
_EP_TOKEN_ALT_RE = re.compile(r"(?<![\dxX])(\d{1,4})[xX](\d{1,4})(?![\dxX])")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _get_setting(key, default="0"):
    """Read an app setting without importing the web stack at module load.

    ``models.*`` is also used by the CLI, where ``web.db`` (and therefore Flask)
    may not be importable at all. Any failure means "setting unavailable", which
    for both toggles is the safe, feature-off answer.
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


def quality_upgrade_enabled() -> bool:
    """True when an already-present episode may be replaced by a better copy."""
    return str(
        _get_setting("dl_quality_upgrade", os.environ.get("MEDIAFORGE_DL_QUALITY_UPGRADE", "0"))
    ) == "1"


def audio_merge_enabled() -> bool:
    """True when a new language should be muxed into the existing file."""
    return str(
        _get_setting("dl_audio_track_merge", os.environ.get("MEDIAFORGE_DL_AUDIO_TRACK_MERGE", "0"))
    ) == "1"


# ---------------------------------------------------------------------------
# Quality probing
# ---------------------------------------------------------------------------

def _pick_video_stream(streams):
    """Best video stream of a probe result, ignoring cover art.

    ``mjpeg``/``png`` streams with the attached_pic disposition are embedded
    thumbnails; taking their 600x900 poster as "the resolution" would make every
    real 1080p file look like an upgrade candidate.
    """
    best = None
    for s in streams or []:
        if s.get("codec_type") != "video":
            continue
        if (s.get("disposition") or {}).get("attached_pic"):
            continue
        if s.get("codec_name") in ("mjpeg", "png", "bmp", "gif"):
            continue
        h = _as_int(s.get("height"))
        if best is None or h > _as_int(best.get("height")):
            best = s
    return best


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def quality_from_probe(probe: dict) -> dict:
    """Normalise an ffprobe result into ``{height, width, bitrate}``.

    ``bitrate`` is the video stream's own bitrate when the container reports it
    and falls back to the format bitrate (audio included, a few hundred kbit/s
    of noise) otherwise. That mix is fine because the comparison only ever comes
    into play at equal resolution and behind a 20 % margin.
    """
    out = {"height": 0, "width": 0, "bitrate": 0}
    if not probe:
        return out
    stream = _pick_video_stream(probe.get("streams"))
    if stream:
        out["height"] = _as_int(stream.get("height"))
        out["width"] = _as_int(stream.get("width"))
        out["bitrate"] = _as_int(stream.get("bit_rate"))
    if not out["bitrate"]:
        out["bitrate"] = _as_int((probe.get("format") or {}).get("bit_rate"))
    return out


def _ffprobe_bin():
    import shutil
    return shutil.which("ffprobe") or "ffprobe"


def _run_ffprobe(target: str, headers: dict | None = None, timeout: int = 25):
    """Raw ffprobe JSON for a local path or a remote URL, or None on failure.

    ``web.transcoder.probe_file()`` is deliberately not reused: it flattens the
    result and drops the bitrate, which is exactly the field the same-resolution
    comparison needs.

    The argument list is passed to subprocess without a shell, so a hoster URL
    or a file name containing shell metacharacters cannot turn into a command.
    """
    import json
    import subprocess

    cmd = [_ffprobe_bin(), "-v", "quiet", "-print_format", "json"]
    if headers:
        hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        if hdr_lines:
            cmd += ["-headers", hdr_lines]
        ua = headers.get("User-Agent") or headers.get("user-agent")
        if ua:
            cmd += ["-user_agent", ua]
    cmd += ["-show_format", "-show_streams", str(target)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Hard cap: this runs inside the single queue worker thread, so a
            # hoster that accepts the connection and then goes quiet would
            # otherwise stall every other download in the queue.
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("[QualityCheck] ffprobe failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def probe_local_quality(path) -> dict | None:
    """Resolution/bitrate of a file already on disk, or None."""
    try:
        if not Path(path).exists():
            return None
    except OSError:
        return None
    quality = quality_from_probe(_run_ffprobe(str(path)))
    return quality if quality.get("height") else None


def probe_remote_quality(stream_url: str, headers: dict | None = None) -> dict | None:
    """Probe a hoster stream for resolution/bitrate without downloading it.

    Returns None when the probe fails or yields no usable height -- callers must
    treat that as "unknown", never as "worse".
    """
    if not stream_url:
        return None
    quality = quality_from_probe(_run_ffprobe(stream_url, headers=headers or None))
    return quality if quality.get("height") else None


def is_better_quality(existing: dict, candidate: dict):
    """Decide whether *candidate* is worth replacing *existing* with.

    Returns ``(better: bool, reason: str)``. Unknown values on either side yield
    False -- see the module docstring on why this errs towards doing nothing.
    """
    if not existing or not candidate:
        return False, "unknown quality"

    old_h, new_h = _as_int(existing.get("height")), _as_int(candidate.get("height"))
    if not old_h or not new_h:
        return False, "unknown resolution"

    if new_h > old_h + HEIGHT_TOLERANCE:
        return True, f"{old_h}p -> {new_h}p"
    if new_h + HEIGHT_TOLERANCE < old_h:
        return False, f"lower resolution ({new_h}p < {old_h}p)"

    old_b, new_b = _as_int(existing.get("bitrate")), _as_int(candidate.get("bitrate"))
    if not old_b or not new_b:
        return False, "same resolution, unknown bitrate"
    if new_b > old_b * (1 + BITRATE_MARGIN):
        return True, (
            f"{new_h}p, bitrate {round(old_b / 1000)} -> {round(new_b / 1000)} kbit/s"
        )
    return False, "not better"


# ---------------------------------------------------------------------------
# Cross-language lookup
# ---------------------------------------------------------------------------

def _episode_token(name: str):
    """``(season, episode)`` parsed out of a file/folder name, or None.

    None is the normal answer for movies, which have no marker at all -- callers
    fall back to comparing the stem instead.
    """
    m = _EP_TOKEN_RE.search(name or "")
    if not m:
        m = _EP_TOKEN_ALT_RE.search(name or "")
    if not m:
        return None
    return _as_int(m.group(1), -1), _as_int(m.group(2), -1)


def _sibling_language_dirs(folder: Path):
    """The same folder under every other language separation folder.

    With ``MEDIAFORGE_LANG_SEPARATION=1`` the download root gains a
    ``german-dub/`` / ``english-sub/`` level (see ``languages.LANG_FOLDERS``).
    Only that one path component is swapped, so a series folder that happens to
    be *named* like a language is left alone as long as it is not at the
    separation level.
    """
    parts = list(folder.parts)
    lowered = [p.lower() for p in parts]
    out = []
    for idx, part in enumerate(lowered):
        if part not in LANG_FOLDERS:
            continue
        for other in LANG_FOLDERS:
            if other == part:
                continue
            swapped = list(parts)
            swapped[idx] = other
            out.append(Path(*swapped))
    return out


def _scan_dir_for_episode(folder: Path, token, stem: str):
    """First media file in *folder* that is the same episode as *stem*."""
    try:
        if not folder.is_dir():
            return None
        entries = sorted(folder.iterdir())
    except OSError:
        return None

    stem_l = (stem or "").lower()
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if entry.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if token is not None:
            if _episode_token(entry.name) == token:
                return entry
        elif entry.stem.lower() == stem_l:
            return entry
    return None


def find_existing_variant(episode_path: Path):
    """Locate an already downloaded copy of this episode in another language.

    Looks in the episode's own folder first (covers ``{language}`` inside the
    naming template) and then in the matching folder under every other language
    separation directory. Returns the path or None.

    Only ever *reads* directories; the caller decides whether to mux into what
    comes back.
    """
    episode_path = Path(episode_path)
    if episode_path.exists():
        return episode_path

    token = _episode_token(episode_path.name)
    stem = episode_path.stem

    candidates = [episode_path.parent] + _sibling_language_dirs(episode_path.parent)
    for folder in candidates:
        # A movie with no episode marker can only be matched by an exact stem,
        # and the exact stem in the *same* folder was already ruled out by the
        # exists() check above -- scanning it again would match nothing.
        if token is None and folder == episode_path.parent:
            continue
        hit = _scan_dir_for_episode(folder, token, stem)
        if hit is not None:
            logger.info("[TrackMerge] existing copy found: %s", hit)
            return hit
    return None
