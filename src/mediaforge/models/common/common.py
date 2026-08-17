"""Site-agnostic episode-action implementations shared by every model family.

AniworldEpisode, SerienstreamEpisode, FilmPalastEpisode, MegakinoEpisode and
MegakinoMovie assign these directly (``download = episode_download`` etc. in
their episode.py). Hosters that cannot go through the yt-dlp+ffmpeg pipeline
here -- VeeV needs a dedicated curl_cffi/Playwright path -- are dispatched by
resolved host inside download() itself, see _download_via_hoster().
HanimeEpisode aliases watch()/syncplay() from here too, but has its
own download() (single HLS stream, no per-language/provider selection).

Also home to the ffmpeg/yt-dlp download pipeline, progress tracking
(_ffmpeg_progress, polled by the web UI), the codec-options helper that
reads the user's encoding settings from the SQLite DB, and the
ProviderData container used by AniWorld/s.to episodes to look up
per-(Audio, Subtitles) hoster links.
"""
import getpass
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading as _threading
from html import unescape as _html_unescape
from pathlib import Path
from typing import Tuple

import ffmpeg

from ...autodeps import ensure_ffmpeg

try:
    from ...autodeps import get_player_path, get_syncplay_path
    from ...config import (
        INVERSE_LANG_LABELS,
        LANG_CODE_MAP,
        LANG_KEY_MAP,
        MEDIAFORGE_TEMP_DIR,
        PROVIDER_HEADERS_D,
        PROVIDER_HEADERS_W,
        Audio,
        Subtitles,
        get_video_codec,
        logger,
    )
except ImportError:
    from mediaforge.autodeps import get_player_path, get_syncplay_path
    from mediaforge.config import (
        INVERSE_LANG_LABELS,
        LANG_CODE_MAP,
        LANG_KEY_MAP,
        MEDIAFORGE_TEMP_DIR,
        PROVIDER_HEADERS_D,
        PROVIDER_HEADERS_W,
        Audio,
        Subtitles,
        get_video_codec,
        logger,
    )

try:
    from .dupecheck import (
        audio_merge_enabled,
        find_existing_variant,
        is_better_quality,
        probe_remote_quality,
        quality_from_probe,
        quality_upgrade_enabled,
    )
except ImportError:
    from mediaforge.models.common.dupecheck import (
        audio_merge_enabled,
        find_existing_variant,
        is_better_quality,
        probe_remote_quality,
        quality_from_probe,
        quality_upgrade_enabled,
    )

try:
    from .subtitles import (
        cleanup_subtitle_files,
        collect_subtitle_files,
        fetch_hoster_subtitles,
        is_subtitle_file,
        subtitles_enabled,
        ytdlp_subtitle_opts,
    )
    from .opensubtitles import (
        fetch_missing_subtitles as _os_fetch_missing_subtitles,
        opensubtitles_enabled,
    )
except ImportError:
    from mediaforge.models.common.subtitles import (
        cleanup_subtitle_files,
        collect_subtitle_files,
        fetch_hoster_subtitles,
        is_subtitle_file,
        subtitles_enabled,
        ytdlp_subtitle_opts,
    )
    from mediaforge.models.common.opensubtitles import (
        fetch_missing_subtitles as _os_fetch_missing_subtitles,
        opensubtitles_enabled,
    )


# Precompile regex for forbidden filename characters
FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*]')


def _effective_provider(episode):
    """Provider whose HTTP headers actually apply to *episode*'s stream.

    Derived from the resolved provider_url host when it maps to a known hoster
    (a site's hoster label can be wrong — mirrored labels point at another
    hoster's domain, see extractors.provider_for_url), otherwise the episode's
    selected_provider. Resolving provider_url is safe here: it is cached and
    the download reads it moments later anyway. Falls back defensively so
    episodes without a provider_url (e.g. Direct/Hanime) keep their label."""
    try:
        from ...extractors import canonical_provider_name, provider_for_url
    except Exception:
        return getattr(episode, "selected_provider", None)
    try:
        pu = getattr(episode, "provider_url", None)
    except Exception:
        pu = None
    # canonical_provider_name() is not cosmetic: provider_for_url() answers in
    # the extractor namespace ("voe", "oneanime"), while the callers below look
    # the result up in PROVIDER_HEADERS_D/_W, which are keyed by the display
    # name ("VOE", "OneAnime"). Without the translation every host this
    # function DID recognise came back with an empty header set -- so the
    # hosters that need a Referer (VeeV, MegaPlay, EchoVideo, OneAnime) were
    # downloaded without one and answered 403, while the ones with an
    # unrecognised host kept working by accident, because those fall through to
    # selected_provider, which is already spelled correctly.
    resolved = provider_for_url(pu)
    if resolved:
        return canonical_provider_name(resolved)
    return getattr(episode, "selected_provider", None)


def _download_via_hoster(episode, cancel_event=None) -> bool:
    """Run a hoster's own downloader when the shared pipeline cannot fetch it.

    Returns True when the file was downloaded here and download() must stop.

    Currently only VeeV: its CDN validates the browser TLS fingerprint, so
    yt-dlp/ffmpeg get rejected and a curl_cffi + Playwright path is needed.
    Dispatch goes through _effective_provider(), i.e. the *resolved host*, not
    the site's hoster label -- labels lie (mirrored entries, " HD"/" HQ"
    suffixes), and doing it here means every model family and every third-party
    module gets it for free instead of re-implementing the branch in its own
    download().
    """
    provider = re.sub(r"\s+(HD|HQ)$", "", str(_effective_provider(episode) or ""),
                      flags=re.IGNORECASE).strip().lower()
    if provider != "veev":
        return False

    try:
        from ...extractors.provider.veev import download_from_veev
    except ImportError:
        from mediaforge.extractors.provider.veev import download_from_veev

    label = os.path.splitext(episode._file_name)[0] if episode._file_name else ""
    os.makedirs(episode._folder_path, exist_ok=True)
    download_from_veev(episode.provider_url, episode._episode_path,
                       cancel_event=cancel_event, label=label)
    return True


def _read_encoding_settings():
    """Read encoding settings directly from the AniWorld SQLite DB.
    Avoids importing mediaforge.web (which triggers __init__ → app.py → circular import).
    Returns a dict of {key: value} for all encoding_* keys, or None on failure.
    """
    try:
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        _db = _Path.home() / ".mediaforge" / "mediaforge.db"
        if not _db.exists():
            return None
        _conn = _sqlite3.connect(str(_db))
        _conn.row_factory = _sqlite3.Row
        try:
            rows = _conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE 'encoding_%'"
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}
        except Exception:
            return None
        finally:
            _conn.close()
    except Exception:
        return None


# Temp directory for intermediate download files (yt-dlp raw + ffmpeg tagged).
# All work-in-progress files land here; only the finished file moves to the
# configured destination folder.  Uses the OS system-temp on the main drive.
# Module-private alias kept for the existing call sites in this file; the
# canonical definition lives in config.MEDIAFORGE_TEMP_DIR so the web workers
# and this module can never drift to two different scratch directories.
_MEDIAFORGE_TEMP_DIR = MEDIAFORGE_TEMP_DIR

def _get_ffmpeg_codec_opts():
    """Return (vcodec, acodec, extra_vopts) from DB encoding settings.
    Falls back to config.get_video_codec() when DB not available.
    """
    import shlex as _shlex

    def _parse_flags(s):
        if not s:
            return {}
        try:
            tokens = _shlex.split(s.strip())
        except Exception:
            return {}
        result = {}
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.startswith("-"):
                key = t.lstrip("-")
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    val = tokens[i + 1]
                    try: val = int(val)
                    except ValueError:
                        try: val = float(val)
                        except ValueError: pass
                    result[key] = val
                    i += 2
                else:
                    result[key] = True  # boolean flag (no value)
                    i += 1
            else:
                i += 1
        return result

    s = _read_encoding_settings()
    if s is None:
        c = get_video_codec()
        return c, c, {}, []

    mode = s.get("encoding_mode", "copy") or "copy"

    if mode == "copy":
        audio = s.get("encoding_audio_copy", "copy") or "copy"
        amap  = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        return "copy", amap.get(audio, "copy"), {}, []

    if mode in ("h264", "h265"):
        hw     = s.get(f"encoding_hw_{mode}", "cpu") or "cpu"
        preset = s.get(f"encoding_preset_{mode}", "medium") or "medium"
        crf_d  = "23" if mode == "h264" else "28"
        crf    = int(s.get(f"encoding_crf_{mode}", crf_d) or crf_d)
        audio  = s.get(f"encoding_audio_{mode}", "copy") or "copy"

        codec_map = {
            "h264": {"cpu": "libx264", "nvenc": "h264_nvenc",
                     "vaapi": "h264_vaapi", "videotoolbox": "h264_videotoolbox"},
            "h265": {"cpu": "libx265", "nvenc": "hevc_nvenc",
                     "vaapi": "hevc_vaapi", "videotoolbox": "hevc_videotoolbox"},
        }
        vcodec = codec_map[mode].get(hw, "libx264" if mode == "h264" else "libx265")

        vopts = {}
        if hw == "nvenc":
            # NVENC uses different preset names than CPU encoders (p1-p7).
            # Map standard x264/x265 preset names to the nearest NVENC equivalent.
            _nvenc_preset_map = {
                "ultrafast": "p1", "superfast": "p1",
                "veryfast":  "p2", "faster":    "p3",
                "fast":      "p4", "medium":    "p5",
                "slow":      "p6", "slower":    "p6",
                "veryslow":  "p7",
            }
            nvenc_preset = _nvenc_preset_map.get(preset, "p5")
            vopts = {"preset": nvenc_preset, "rc": "vbr", "cq": crf}
        elif hw == "vaapi":
            vaapi_device = s.get("encoding_vaapi_device", "") or ""
            vopts = {"vf": "format=nv12,hwupload", "global_quality": crf}
        elif hw == "videotoolbox":
            # VideoToolbox uses q:v (1-100, higher=better), opposite of CRF.
            # Map CRF 0-51 → q:v 100-1 linearly.
            vt_quality = max(1, min(100, round(100 - (crf / 51) * 99)))
            vopts = {"q:v": vt_quality}
        else:
            vopts = {"preset": preset, "crf": crf}

        amap = {"copy": "copy", "aac": "aac", "ac3": "ac3"}
        _vaapi_global = ["-vaapi_device", vaapi_device] if (hw == "vaapi" and vaapi_device) else []
        return vcodec, amap.get(audio, "copy"), vopts, _vaapi_global

    if mode == "expert":
        vflags = s.get("encoding_expert_video", "") or ""
        aflags = s.get("encoding_expert_audio", "") or ""
        vparsed = _parse_flags(vflags)
        aparsed = _parse_flags(aflags)
        vcodec  = vparsed.pop("c:v", vparsed.pop("vcodec", "copy"))
        acodec  = aparsed.pop("c:a", aparsed.pop("acodec", "copy"))
        vopts   = dict(vparsed)
        for k, v in aparsed.items():
            vopts[f"a:{k}"] = v
        return vcodec, acodec, vopts, []

    # Fallback
    c = get_video_codec()
    return c, c, {}, []


def _get_ffmpeg_codec_opts_for_download():
    """Wrapper around _get_ffmpeg_codec_opts() used at every codec-options call
    site inside download().

    When the user has set encoding_timing to "after_download" and picked an
    actual transcode mode (h264/h265), this returns a plain stream-copy
    result instead so the download step stays fast — the real transcode is
    deferred to the encoding queue (see web/encoding_worker.py), which reads
    the live encoding_* settings itself when it processes the job, so the
    end result is identical either way, just deferred out of the download
    queue. "copy" and "expert" modes are returned unchanged since there is
    nothing worth deferring for those.
    """
    s = _read_encoding_settings()
    if s is not None:
        mode = s.get("encoding_mode", "copy") or "copy"
        timing = s.get("encoding_timing", "during_download") or "during_download"
        if timing == "after_download" and mode in ("h264", "h265"):
            return "copy", "copy", {}, []
    return _get_ffmpeg_codec_opts()


def _get_encoder_label():
    """Return a short human-readable label for the active encoder, e.g. 'H.265 · CRF 28'."""
    s = _read_encoding_settings()
    if s is None:
        return ""
    mode = s.get("encoding_mode", "copy") or "copy"
    if mode == "copy":
        audio = s.get("encoding_audio_copy", "copy") or "copy"
        return "Copy" if audio == "copy" else f"Copy · Audio {audio.upper()}"
    if mode in ("h264", "h265"):
        hw    = s.get(f"encoding_hw_{mode}", "cpu") or "cpu"
        crf_d = "23" if mode == "h264" else "28"
        crf   = s.get(f"encoding_crf_{mode}", crf_d) or crf_d
        label = "H.264" if mode == "h264" else "H.265"
        if hw != "cpu":
            label += f" {hw.upper()}"
        label += f" · CRF {crf}"
        return label
    if mode == "expert":
        vf = s.get("encoding_expert_video", "") or ""
        return f"Expert: {vf[:30]}" if vf else "Expert"
    return ""


def clean_title(title) -> str:
    """Clean a string to make it safe for use as a filename.

    Tolerates None. It gets fed whatever a page scrape produced, and a scrape produces
    None whenever the markup didn't match — a blocked page, a captcha wall, a layout
    change upstream. Blowing up in here turned that ordinary situation into
    ``TypeError: argument of type 'NoneType' is not iterable`` from deep inside
    html.unescape(), five frames below anything that knows what a series is. A
    filename sanitiser has no business being the thing that reports a failed fetch.
    """
    if not title:
        return ""
    # Unescape HTML entities first (e.g. &amp; → &) before stripping forbidden chars
    return FORBIDDEN_CHARS.sub("", _html_unescape(str(title))).strip()


# Shortest key that may take part in a REVERSE folder match -- see
# titles_match() for why the two directions are not treated the same.
_LOOSE_MIN_REVERSE = 10


def loose_title_key(title) -> str:
    """Punctuation-insensitive comparison key for a series/movie title.

    The Python twin of static/app.js's ``_looseTitleKey``; the two must agree,
    because the same "is this already on disk?" question is answered on both
    sides (the badge on a card, and the per-episode ticks in the download
    modal) and a user who sees the badge but no ticks has been told two
    different things about one file.

    Reduces a title to letters and digits: a year suffix and everything after
    it goes, then every space, quote, colon and dash. That is what lets a
    folder written by one provider ("Kaguya-sama Love is War") be recognised
    from another provider's spelling ("Kaguya-sama: Love is War").

    One deliberate asymmetry with the JS twin: ``str.isalnum()`` keeps CJK and
    Cyrillic, while the JS regex (``[^a-z0-9]``) drops them. So a purely
    non-Latin title yields a key here and an empty string there. That is safe
    in the one direction it matters -- the browser's keys are a subset of the
    server's, so a key the browser builds always exists server-side -- and
    making them identical would mean either losing non-Latin titles server-side
    or rewriting the JS key, which is stored in nothing but is compared against
    values that are. Left alone on purpose.
    """
    if not title:
        return ""
    text = _html_unescape(str(title)).lower()
    # Straighten the quote characters scrapes disagree about before dropping
    # them, so the two sides cannot differ by an apostrophe alone.
    for src, dst in (("‘", "'"), ("’", "'"), ("′", "'"),
                     ("`", "'"), ("“", '"'), ("”", '"'),
                     ("„", '"')):
        text = text.replace(src, dst)
    head = text.split("(")[0]
    return "".join(c for c in head if c.isalnum())


def titles_match(folder_name, title) -> bool:
    """Does *folder_name* on disk hold *title*?

    The old rule was ``folder.lower().startswith(provider_title.lower())`` in
    four different places, and it is the reason "already downloaded" worked
    from one provider and not from another: the folder is stamped with the
    title of whichever provider downloaded it FIRST, and every other provider
    then compares its own spelling against it. AniWorld's "Call of the Night"
    matched; a site that calls the same show "Call of the Night: Yofukashi no
    Uta" did not, because a longer title is not a prefix of a shorter folder.

    So the comparison goes both ways -- but not symmetrically:

    * folder starts with title  -- always accepted. This is the original rule
      and covers the normal "Solo Leveling" card vs. "Solo Leveling Season 2"
      folder case.
    * title starts with folder  -- accepted only when the folder key is at
      least ``_LOOSE_MIN_REVERSE`` characters. Without that floor a "Naruto"
      folder would claim "Naruto Shippuden" and a "One Piece" folder would
      claim "One Piece Film Red": short titles are prefixes of unrelated
      shows far too often, and a wrong "already downloaded" is worse than a
      missing one -- it stops a download the user asked for.

    Titles that share no prefix at all ("Attack on Titan" / "Shingeki no
    Kyojin") cannot be solved by string comparison and are deliberately out of
    scope here; that needs a provider-independent id stored at download time.
    """
    folder_key = loose_title_key(folder_name)
    title_key = loose_title_key(title)
    if not folder_key or not title_key:
        return False
    if folder_key.startswith(title_key):
        return True
    return (len(folder_key) >= _LOOSE_MIN_REVERSE
            and title_key.startswith(folder_key))


def check_downloaded(episode_path):
    result = {
        "exists": False,
        "video_langs": set(),
        "audio_langs": set(),
        # Resolution/bitrate of the copy on disk, taken from the same probe the
        # language scan already runs (so this costs nothing extra). Consumed by
        # the quality-upgrade check in download(); 0 means "could not tell",
        # which that check deliberately reads as "do not replace".
        "height": 0,
        "width": 0,
        "bitrate": 0,
    }

    episode_path = Path(episode_path)
    if not episode_path.exists():
        return result

    result["exists"] = True

    try:
        probe = ffmpeg.probe(episode_path)
    except ffmpeg.Error:
        return result

    streams = probe.get("streams", [])

    for s in streams:
        lang = s.get("tags", {}).get("language", "und")
        if s.get("codec_type") == "video":
            result["video_langs"].add(lang)
        elif s.get("codec_type") == "audio":
            result["audio_langs"].add(lang)

    result.update(quality_from_probe(probe))
    return result


class ProviderData:
    """
    Container for provider URLs grouped by language settings.

    The internal structure is:

        dict[(Audio, Subtitles)][provider_name]

    Meaning:
    - The key is a tuple of (Audio, Subtitles)
    - The value is a dictionary mapping provider names to their URLs
    """

    def __init__(self, data):
        self._data = data

    def __str__(self):
        # return f"{self.__class__.__name__}({self._data!r})"
        lines = []

        for (audio, subtitles), providers in sorted(
            self._data.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        ):
            header = f"{audio.value} audio"
            if subtitles != Subtitles.NONE:
                header += f" + {subtitles.value} subtitles"

            lines.append(header)

            for provider, url in providers.items():
                lines.append(f"  - {provider:<8} -> {url}")

            lines.append("")

        return "\n".join(lines).rstrip()

    def __repr__(self):
        return f"{self.__class__.__name__}({self._data!r})"

    # Accept a tuple directly
    def get(self, lang_tuple: Tuple[Audio, Subtitles]):
        return self._data.get(lang_tuple, {})

    # Behave like a dictionary
    def __getitem__(self, lang_tuple: Tuple[Audio, Subtitles]):
        return self._data[lang_tuple]


# -----------------------------------------------------------------------------
# Episode actions (moved from models/*/episode.py)
# -----------------------------------------------------------------------------


def _remove_empty_dirs(folder_path, base_folder):
    """Remove folder_path and base_folder if they are empty directories."""
    try:
        if folder_path.is_dir() and not any(folder_path.iterdir()):
            folder_path.rmdir()
        if base_folder.is_dir() and not any(base_folder.iterdir()):
            base_folder.rmdir()
    except OSError:
        pass


class _YtdlpQuietLogger:
    """Suppress yt-dlp console output while keeping error/warning visibility."""

    def debug(self, msg):
        if msg.startswith("[debug]"):
            logger.debug(f"[yt-dlp] {msg}")

    def info(self, msg):
        pass

    # yt-dlp lines that are normal operation, not a problem. They are logged
    # at DEBUG instead of WARNING because a WARNING in this log means "look at
    # me", and these were making a perfectly healthy download read like a
    # failure -- the generic-extractor line in particular, which is what
    # yt-dlp ALWAYS says when it is handed a direct stream URL rather than a
    # page it has a site plugin for. Every hoster extractor in this project
    # resolves to exactly such a URL, so that line accompanies every single
    # successful download.
    _BENIGN_WARNINGS = (
        "Live HLS streams are not supported",
        "Falling back on generic information extractor",
        "The information of all playlist entries will be held in memory",
        "Falling back to ffmpeg",
    )

    def warning(self, msg):
        text = str(msg or "")
        if any(fragment in text for fragment in self._BENIGN_WARNINGS):
            logger.debug(f"[yt-dlp] {text}")
            return
        logger.warning(f"[yt-dlp] {text}")

    def error(self, msg):
        # WARNING, not ERROR, on purpose. yt-dlp reports every dead or blocked
        # hoster link as an error ("HTTP Error 404: Not Found", "Unable to
        # download webpage", "Unable to extract ..."), and download() walks
        # through the hosters a post lists until one works -- so even a download
        # that ultimately SUCCEEDS routes several of these through here. At
        # ERROR level telemetry/hooks.py turned each one into a crash report
        # against MediaForge, which was by far the largest source of noise in
        # the crash channel.
        #
        # No signal is lost: a download that really fails still ends in
        # `raise RuntimeError(f"yt-dlp download failed (rc={ret})")` below, and
        # the queue worker reports THAT -- one report per actual failure instead
        # of one per attempted hoster. The line stays fully visible in the
        # user's console either way.
        logger.warning(f"[yt-dlp] {msg}")


# Thread-safe global for current ffmpeg download progress (used by web UI)
_ffmpeg_progress_lock = _threading.Lock()
_ffmpeg_active_count = 0  # number of concurrently running ffmpeg processes
_ffmpeg_progress = {
    "percent": 0.0,
    "time": "",
    "speed": "",
    "fps": "",
    "encoder": "",
    "bandwidth": "",
    "downloaded_mb": 0.0,
    "active": False,
    "phase": "",  # "download" (yt-dlp) or "ffmpeg" (muxing/processing)
}


def get_ffmpeg_progress():
    """Return a snapshot of the current ffmpeg download progress."""
    with _ffmpeg_progress_lock:
        return dict(_ffmpeg_progress)


def _parse_ffmpeg_time(time_str):
    """Parse ffmpeg time string (HH:MM:SS.xx) to seconds."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


def _print_cli_progress(percent, time_str, speed_str, label=""):
    """Print a simple CLI progress bar without ANSI colors."""
    if not sys.stderr.isatty():
        return
    bar_width = 30
    filled = int(bar_width * percent / 100)
    bar = "#" * filled + "-" * (bar_width - filled)
    prefix = f"{label} - " if label else ""
    line = f"\r{prefix}[{bar}] {percent:5.1f}% | {time_str} | {speed_str}  "
    sys.stderr.write(line)
    sys.stderr.flush()


def print_episode_summary(title, ep_url, success):
    """Print a persistent one-liner to stderr after each episode finishes.

    Clears any leftover progress-bar characters on the current line, then
    writes a newline-terminated summary so it stays visible in the terminal.

    Example output:
        My Hero Academia - S01E03 - Abgeschlossen
        My Hero Academia - S01E04 - Fehler
    """
    ep_id = ""
    m = re.search(r"staffel-(\d+)/episode-(\d+)", ep_url, re.IGNORECASE)
    if m:
        ep_id = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    else:
        f = re.search(r"filme/film-(\d+)", ep_url, re.IGNORECASE)
        if f:
            ep_id = f"Film {f.group(1)}"
        else:
            ep_id = ep_url.split("/")[-1] or ep_url

    if isinstance(success, str):
        status = success
    else:
        status = "Abgeschlossen" if success else "Fehler"
    # \r clears any partial progress bar on the same line before writing
    sys.stderr.write(f"\r{' ' * 120}\r{title} - {ep_id} - {status}\n")
    sys.stderr.flush()


def _run_ffmpeg_with_progress(node, overwrite_output=True, label="", cancel_event=None, process_ref=None, phase="ffmpeg"):
    """Run an ffmpeg node and stream its progress output cleanly.

    Includes stall detection: if FFmpeg stops making progress (same frame/time
    values) for STALL_TIMEOUT seconds the process is killed so the caller's
    retry logic can kick in.

    Optional args:
        cancel_event:  threading.Event — if set, the ffmpeg process is killed immediately.
        process_ref:   list of length 1 — will be populated with the Popen object so the
                       caller can kill the process from another thread.
    """
    global _ffmpeg_active_count
    import queue
    import threading
    import time

    STALL_TIMEOUT = 600  # 10 minutes without progress → kill (must exceed reconnect_delay_max=300)

    debug_mode = os.getenv("MEDIAFORGE_DEBUG_MODE", "0") == "1"
    is_tty = sys.stderr.isatty()

    # Regex to extract progress indicators from ffmpeg status lines
    _RE_FRAME = re.compile(r"frame=\s*(\d+)")
    _RE_FPS   = re.compile(r"fps=\s*(\S+)")
    _RE_TIME = re.compile(r"time=(\S+)")
    _RE_SPEED = re.compile(r"speed=\s*(\S+)")
    _RE_BITRATE = re.compile(r"bitrate=\s*(\S+)")
    _RE_SIZE = re.compile(
        r"size=\s*(\d+(?:\.\d+)?)\s*([kKmM])(?:i)?B", re.IGNORECASE
    )
    _RE_DURATION = re.compile(r"Duration:\s*(\d+:\d+:\d+\.\d+)")
    # ffmpeg's startup banner (version / build flags / library versions). It is
    # noise here, and it used to crowd the real error out of the last-20-lines
    # detail built below -- the "configuration:" line alone is ~1.5 kB, so a
    # failure report reaching telemetry could consist of nothing but the
    # banner. Matched defensively even though -hide_banner is passed, because
    # some builds still emit parts of it.
    _RE_BANNER = re.compile(
        r"^(ffmpeg version |built with |configuration:|lib(avutil|avcodec|avformat|"
        r"avdevice|avfilter|swscale|swresample|postproc)\s)"
    )

    # Use shorter stats_period for smoother progress (1s in non-debug, 10s in debug)
    stats_period = "10" if debug_mode else "1"

    args = ffmpeg.compile(node, overwrite_output=overwrite_output)
    # Global option -- must sit right after the binary, before any input.
    if "-hide_banner" not in args:
        args.insert(1, "-hide_banner")
    if "-stats_period" not in args:
        args.insert(-1, "-stats_period")
        args.insert(-1, stats_period)

    process = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=False
    )

    # Expose process to caller for external cancellation
    if process_ref is not None:
        process_ref[0] = process

    # --- reader thread: reads stderr byte-by-byte and pushes complete lines ---
    line_queue = queue.Queue()

    def _reader():
        buf = bytearray()
        while True:
            char = process.stderr.read(1)
            if not char:
                # EOF – push whatever is left
                if buf:
                    line_queue.put(buf.decode("utf-8", errors="replace").strip())
                line_queue.put(None)  # sentinel
                return
            if char in (b"\r", b"\n"):
                if buf:
                    line_queue.put(buf.decode("utf-8", errors="replace").strip())
                    buf.clear()
            else:
                buf.extend(char)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # --- main loop: consume lines, log them, and watch for stalls ---
    stderr_lines = []  # collect non-progress stderr lines for error reporting
    kill_reason = None  # set when *we* killed ffmpeg, so the error can say so
    last_frame = None
    last_time = None
    last_size_kb = None
    last_size_ts = None
    last_change = time.monotonic()
    total_duration = 0.0
    wall_start = time.monotonic()

    with _ffmpeg_progress_lock:
        _ffmpeg_active_count += 1
        _ffmpeg_progress.update(percent=0.0, time="", speed="", fps="", encoder=_get_encoder_label(), bandwidth="", active=True, phase=phase)

    try:
        while True:
            # Cancellation has to be honoured on EVERY iteration, not only when
            # the line queue runs dry: ffmpeg prints a progress line about twice
            # a second, so the queue.Empty branch below was never reached while a
            # download/encode was actually moving — a cancelled episode kept
            # running to its end and only then noticed it had been cancelled.
            if cancel_event is not None and cancel_event.is_set():
                logger.debug("[FFmpeg] Cancelled by external event. Killing process.")
                process.kill()
                break
            try:
                line_str = line_queue.get(timeout=1.0)
            except queue.Empty:
                # Check external cancellation first
                if cancel_event is not None and cancel_event.is_set():
                    logger.debug("[FFmpeg] Cancelled by external event. Killing process.")
                    process.kill()
                    break
                # No new line within 1 s – just check the stall timer
                if time.monotonic() - last_change > STALL_TIMEOUT:
                    logger.warning(
                        "[FFmpeg] Stall detected – no progress for "
                        f"{STALL_TIMEOUT}s. Killing process."
                    )
                    kill_reason = (
                        f"no progress for {STALL_TIMEOUT}s "
                        "(source stalled or unreachable) – ffmpeg was killed"
                    )
                    process.kill()
                    break
                continue

            if line_str is None:
                # Reader thread finished (EOF)
                break

            # Log the line
            if line_str.startswith("frame=") or line_str.startswith("size="):
                # --- extract progress values ---
                cur_frame = None
                cur_time = None
                cur_time_str = ""
                cur_speed_str = ""
                cur_bitrate_str = ""
                cur_bw_str = ""
                cur_downloaded_mb = None
                cur_bitrate_str = ""
                cur_bw_str = ""
                m = _RE_FRAME.search(line_str)
                if m:
                    cur_frame = m.group(1)
                cur_fps_str = ""
                m = _RE_FPS.search(line_str)
                if m:
                    try:
                        fps_val = float(m.group(1))
                        cur_fps_str = f"{fps_val:.0f}" if fps_val >= 1 else ""
                    except ValueError:
                        pass
                m = _RE_TIME.search(line_str)
                if m:
                    cur_time = m.group(1)
                    cur_time_str = m.group(1)
                m = _RE_SPEED.search(line_str)
                if m:
                    cur_speed_str = m.group(1)
                m = _RE_BITRATE.search(line_str)
                if m:
                    cur_bitrate_str = m.group(1)
                    if cur_bitrate_str.lower() == "n/a":
                        cur_bitrate_str = ""
                m = _RE_SIZE.search(line_str)
                if m:
                    size_val = float(m.group(1))
                    size_unit = m.group(2).lower()
                    size_kb = size_val * (1024 if size_unit == "m" else 1)
                    now = time.monotonic()
                    if last_size_kb is not None and last_size_ts is not None:
                        dt = now - last_size_ts
                        if dt > 0:
                            kb_per_sec = (size_kb - last_size_kb) / dt
                            if kb_per_sec > 0:
                                mb_per_sec = kb_per_sec / 1024
                                cur_bw_str = f"{mb_per_sec:.1f} MB/s"
                    last_size_kb = size_kb
                    last_size_ts = now
                    cur_downloaded_mb = round(size_kb / 1024, 1)

                # Compute percentage + ETA
                percent = 0.0
                eta_sec = 0
                if total_duration > 0 and cur_time_str:
                    elapsed_enc = _parse_ffmpeg_time(cur_time_str)
                    percent = min((elapsed_enc / total_duration) * 100, 100.0)
                    wall_elapsed = time.monotonic() - wall_start
                    if wall_elapsed > 0 and elapsed_enc > 0:
                        speed_factor = elapsed_enc / wall_elapsed
                        remaining = total_duration - elapsed_enc
                        eta_sec = max(0, int(remaining / speed_factor))

                # Update global progress for web UI
                with _ffmpeg_progress_lock:
                    prev_bw = _ffmpeg_progress.get("bandwidth", "")
                    prev_dl = _ffmpeg_progress.get("downloaded_mb", 0.0)
                    prev_fps = _ffmpeg_progress.get("fps", "")
                    _ffmpeg_progress.update(
                        percent=round(percent, 1),
                        speed=cur_speed_str,
                        fps=cur_fps_str or prev_fps,
                        bandwidth=cur_bw_str or prev_bw,
                        downloaded_mb=cur_downloaded_mb if cur_downloaded_mb is not None else prev_dl,
                        eta_sec=eta_sec,
                        active=True,
                    )

                if debug_mode:
                    logger.info(f"[FFmpeg Progress] {line_str}")
                elif is_tty:
                    _print_cli_progress(percent, cur_time_str, cur_speed_str, label)

                # --- stall detection ---
                if cur_frame != last_frame or cur_time != last_time:
                    last_frame = cur_frame
                    last_time = cur_time
                    last_change = time.monotonic()
                elif time.monotonic() - last_change > STALL_TIMEOUT:
                    logger.warning(
                        "[FFmpeg] Stall detected – no progress for "
                        f"{STALL_TIMEOUT}s. Killing process."
                    )
                    kill_reason = (
                        f"no progress for {STALL_TIMEOUT}s "
                        "(encode/download frozen) – ffmpeg was killed"
                    )
                    process.kill()
                    break
            elif line_str:
                # Try to capture total duration from ffmpeg header
                if total_duration == 0.0:
                    dm = _RE_DURATION.search(line_str)
                    if dm:
                        total_duration = _parse_ffmpeg_time(dm.group(1))

                logger.debug(f"[FFmpeg] {line_str}")
                stderr_lines.append(line_str)

        # Clear the progress line in CLI
        if not debug_mode and is_tty:
            sys.stderr.write("\r" + " " * 120 + "\r")
            sys.stderr.flush()

    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_active_count -= 1
            _ffmpeg_progress.update(
                percent=0.0, time="", speed="", bandwidth="", downloaded_mb=0.0,
                active=_ffmpeg_active_count > 0, phase="" if _ffmpeg_active_count == 0 else phase
            )

    reader_thread.join(timeout=5)
    process.wait()
    if process.returncode != 0:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Download cancelled")
        # Prefer real diagnostics over banner leftovers, and keep the tail:
        # ffmpeg prints the line that explains the failure last.
        _useful = [ln for ln in stderr_lines if not _RE_BANNER.match(ln)]
        _detail_lines = (_useful or stderr_lines)[-20:]
        detail = "\n".join(_detail_lines) if _detail_lines else f"exit code {process.returncode}"
        if len(detail) > 4000:
            # Keep the end -- that is where the cause is.
            detail = "…" + detail[-4000:]
        logger.error(f"[FFmpeg] Process failed (rc={process.returncode}):\n{detail}")
        if kill_reason:
            # Not an ffmpeg failure at all -- we pulled the plug. Saying so
            # keeps these out of the "ffmpeg is broken" bucket in telemetry,
            # while the stderr tail above still reaches the log.
            raise RuntimeError(f"ffmpeg aborted (rc={process.returncode}): {kill_reason}")
        raise RuntimeError(f"ffmpeg error (rc={process.returncode}): {detail}")


# Leftovers yt-dlp writes next to its output while a download is in flight.
# Matched against the part of a file name that follows "<stem>." so nothing
# outside this download's own file family is ever considered.
_YTDLP_LEFTOVER_MARKERS = (".part", ".ytdl", ".temp.", ".part-Frag")
_YTDLP_FORMAT_TEMP_RE = re.compile(r"^f\d+\.")


def _cleanup_partial_downloads(output_path, reason="cancelled"):
    """Delete yt-dlp's in-flight leftovers for *output_path*.

    yt-dlp streams into ``<stem>.<ext>.part`` (plus ``.ytdl`` resume state,
    ``.part-FragN`` fragment chunks and per-format ``<stem>.f137.mp4`` files
    when video and audio are fetched separately) and only renames to the final
    name once it is done. Aborting mid-download therefore used to leave those
    behind — in the temp dir for the regular pipeline, and directly in the
    user's library folder for Direct Link jobs, where they are plainly visible.

    Only files whose name starts with ``<stem>.`` AND carries one of the
    leftover markers are removed, so a finished file that happens to share the
    stem (the episode's own .mkv, a subtitle sidecar) is never touched.
    """
    try:
        output_path = Path(output_path)
        folder = output_path.parent
        stem = output_path.stem
        if not stem or not folder.is_dir():
            return
        prefix = stem + "."
        removed = 0
        for entry in folder.iterdir():
            name = entry.name
            if not name.startswith(prefix) or name == output_path.name:
                continue
            tail = name[len(prefix):]
            is_leftover = (
                any(marker in name for marker in _YTDLP_LEFTOVER_MARKERS)
                or bool(_YTDLP_FORMAT_TEMP_RE.match(tail))
            )
            if not is_leftover:
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
            except OSError as exc:
                logger.debug(f"[cleanup] Could not remove {entry.name}: {exc}")
        if removed:
            logger.info(f"[cleanup] Removed {removed} partial download file(s) ({reason}): {stem}")
    except Exception as exc:  # never let cleanup mask the original failure
        logger.debug(f"[cleanup] Partial-file cleanup failed: {exc}")


# Public alias: module providers that run their own yt-dlp download (rather
# than going through episode_download here) should call this when their
# download is cancelled or abandoned, so their leftovers are cleaned up the
# same way the built-in pipeline cleans up its own.
cleanup_partial_downloads = _cleanup_partial_downloads


def _run_ytdlp_download(url, output_path, headers=None, label="", cancel_event=None, impersonate=None, audio_lang=None, format_override=None, want_subtitles=False):
    """Download an HLS stream using yt-dlp with concurrent fragment downloads.

    Significantly faster than ffmpeg for HLS/m3u8 streams because yt-dlp fetches
    multiple segments in parallel (configurable via MEDIAFORGE_CONCURRENT_FRAGMENTS,
    default 16).  ffmpeg (probe + mux) is still used for all local-file operations.

    Args:
        url:          The HLS m3u8 URL to download.
        output_path:  Desired output path (will end up as .mkv).
        headers:      Optional dict of HTTP headers to send.
        impersonate:  Optional browser target for curl_cffi TLS impersonation (e.g. "chrome").
        label:        Label shown in the CLI progress bar.
        cancel_event: threading.Event — if set the download is aborted.
        audio_lang:   Optional ISO 639-2 code ("deu"/"eng"/"jpn") of the desired
                      audio track.  Some HLS masters bundle multiple audio
                      renditions (e.g. Deutsch *and* English) in one playlist; by
                      default yt-dlp picks "bestaudio" by bitrate and can grab the
                      wrong language (thanks for nothing).  When set we constrain the format selector to
                      that language (with a fallback to bestaudio if no match).
        want_subtitles: When True, every subtitle rendition the source offers is
                      written as a sidecar next to output_path (see
                      models/common/subtitles.py). The caller collects and muxes
                      them; nothing here embeds anything.
        format_override: Optional literal yt-dlp format selector (e.g. "303+bestaudio")
                      that takes precedence over the audio_lang-based selector below.
                      Used by the Direct Link feature (models/direct_link/episode.py),
                      where the user picks an exact format from a probed list rather
                      than a dub/sub language.
    """
    global _ffmpeg_active_count
    import yt_dlp
    from pathlib import Path

    output_path = Path(output_path)
    debug_mode = os.getenv("MEDIAFORGE_DEBUG_MODE", "0") == "1"
    is_tty = sys.stderr.isatty()
    n_fragments = int(os.getenv("MEDIAFORGE_CONCURRENT_FRAGMENTS", "8"))

    # outtmpl without extension — yt-dlp appends %(ext)s itself
    outtmpl = str(output_path.with_suffix("")) + ".%(ext)s"

    # Build a format selector for the selected audio language
    _LANG_VARIANTS = {
        "deu": ["de", "deu", "ger", "de-DE"],
        "eng": ["en", "eng", "en-US", "en-GB"],
        "jpn": ["ja", "jpn", "jp", "ja-JP"],
    }
    if format_override:
        _fmt = format_override
    elif audio_lang and audio_lang in _LANG_VARIANTS:
        _variants = _LANG_VARIANTS[audio_lang]
        # Prefer video + language-matched audio, then any video+audio, then best
        _fmt = "/".join(
            f"bestvideo+bestaudio[language={v}]" for v in _variants
        ) + "/bestvideo+bestaudio/best"
    else:
        _fmt = "bestvideo+bestaudio/best"

    def _progress_hook(d):
        # Honour external cancellation via threading.Event
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by external event")

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            elapsed = d.get("elapsed") or 0

            if total > 0:
                percent = downloaded / total * 100.0
            else:
                # HLS/m3u8 fragment streams (Pornhub and most other
                # segment-based sources) report no byte total at all when
                # yt-dlp can't estimate one -- a live stream, or a playlist
                # whose fragments vary enough in size that the running
                # estimate in yt_dlp/downloader/fragment.py never kicks in.
                # That downloader still reports fragment_index/fragment_count
                # on every callback either way (same file, same function),
                # so fall back to a fragment-count-based percentage instead
                # of leaving the queue stuck at 0% for the whole download.
                frag_index = d.get("fragment_index") or 0
                frag_count = d.get("fragment_count") or 0
                percent = (frag_index / frag_count * 100.0) if frag_count > 0 else 0.0

            speed_str = f"{speed / 1_048_576:.1f} MB/s" if speed else ""
            downloaded_mb = round(downloaded / 1_048_576, 1)
            total_mb = round(total / 1_048_576, 1) if total > 0 else 0.0
            eta_sec = d.get("eta") or 0
            elapsed_str = (
                f"{int(elapsed // 3600):02d}:"
                f"{int((elapsed % 3600) // 60):02d}:"
                f"{int(elapsed % 60):02d}"
                if elapsed
                else ""
            )

            with _ffmpeg_progress_lock:
                _ffmpeg_progress.update(
                    percent=round(percent, 1),
                    time=elapsed_str,
                    speed=speed_str,
                    bandwidth=speed_str,
                    downloaded_mb=downloaded_mb,
                    total_mb=total_mb,
                    eta_sec=int(eta_sec),
                    active=True,
                )

            if is_tty and not debug_mode:
                _print_cli_progress(percent, elapsed_str, speed_str, label)
            elif debug_mode:
                logger.debug(f"[yt-dlp] {label} {percent:.1f}% {speed_str}")

        elif d["status"] == "finished" and debug_mode:
            logger.debug(f"[yt-dlp] Finished segment/file: {d.get('filename')}")

    ydl_opts = {
        "outtmpl": outtmpl,
        # Download best video+audio together; merge_output_format ensures .mkv output.
        # `_fmt` constrains the audio rendition to the requested language when known.
        "format": _fmt,
        "concurrent_fragment_downloads": n_fragments,
        "http_headers": headers or {},
        "quiet": False,   # must be False so progress hooks fire reliably
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpQuietLogger(),  # custom logger suppresses console spam
        "progress_hooks": [_progress_hook],
        "noplaylist": True,  # download only the requested video when URLs contain playlist params (&list=...)
        "js_runtimes": {"node": {}, "deno": {}},  # allow yt-dlp to use node/deno for JS deciphering (e.g. YouTube)
        "merge_output_format": "mkv",
        # Do not try to fix broken streams — our HLS URLs are fine
        "fixup": "never",
        "overwrites": True,
        # Resilience: retry each fragment up to 10x with exponential back-off,
        # use a generous socket timeout so slow CDNs don't time out mid-segment.
        "nocheckcertificate": True,
        "retries": 10,
        "fragment_retries": 50,  # cap to avoid infinite loops on broken CDN segments
        "socket_timeout": 30,
        "retry_sleep_functions": {
            "http": lambda n: min(2 ** n, 30),
            "fragment": lambda n: min(2 ** n, 30),
        },
    }

    if want_subtitles:
        ydl_opts.update(ytdlp_subtitle_opts())

    # Optional global bandwidth throttle (KB/s; 0 / unset = unlimited).
    # yt-dlp's `ratelimit` (bytes/sec) is enforced *per concurrent fragment
    # connection*, so with N parallel fragments the aggregate speed is N×ratelimit.
    # Spread the configured limit across the fragments so the *total* download
    # speed matches the user setting (streams here are always fragmented HLS).
    try:
        _rate_kb = int(os.getenv("MEDIAFORGE_DOWNLOAD_RATE_LIMIT", "0") or "0")
    except ValueError:
        _rate_kb = 0
    if _rate_kb > 0:
        _conns = max(1, n_fragments)
        ydl_opts["ratelimit"] = max(1, (_rate_kb * 1024) // _conns)

    # Some CDNs (e.g. VeeV) validate the TLS fingerprint (JA3/JA4) and reject
    # non-browser clients.  curl_cffi impersonates a real browser TLS stack.
    if impersonate:
        try:
            import curl_cffi  # noqa: F401 — just check availability
            ydl_opts["impersonate"] = impersonate
            # curl_cffi/libcurl bypasses the socket.getaddrinfo DNS patch, so
            # route its resolution through the project DoH server explicitly.
            from ...config import ensure_curl_cffi_doh
            ensure_curl_cffi_doh()
        except ImportError:
            logger.warning("curl_cffi not installed — TLS impersonation skipped (install with: pip install curl_cffi)")

    with _ffmpeg_progress_lock:
        _ffmpeg_active_count += 1
        _ffmpeg_progress.update(percent=0.0, time="", speed="", bandwidth="", active=True, phase="download")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ret = ydl.download([url])

        if is_tty and not debug_mode:
            sys.stderr.write("\r" + " " * 120 + "\r")
            sys.stderr.flush()

        if ret != 0:
            raise RuntimeError(f"yt-dlp download failed (rc={ret})")

        # Normalise output to the expected .mkv path in case yt-dlp chose
        # a different extension (e.g. .mp4, .ts, .m4a for single-stream HLS).
        expected = output_path.with_suffix(".mkv")
        if not expected.exists():
            for ext in (".mkv", ".mp4", ".ts", ".m4v", ".webm", ".m4a", ".aac"):
                candidate = output_path.with_suffix(ext)
                if candidate.exists() and candidate != expected:
                    candidate.rename(expected)
                    logger.debug(f"[yt-dlp] Renamed {candidate.name} → {expected.name}")
                    break
            else:
                # Last-resort: find any file with this stem
                # Subtitle sidecars share this stem ("<stem>.de.vtt"). Renaming
                # one to .mkv would hand the caller a text file as the video.
                matches = [
                    m for m in sorted(output_path.parent.glob(output_path.stem + ".*"))
                    if not is_subtitle_file(m)
                ]
                if matches:
                    matches[0].rename(expected)
                    logger.debug(f"[yt-dlp] Renamed {matches[0].name} → {expected.name}")
                else:
                    raise RuntimeError(
                        f"yt-dlp output not found near: {output_path}"
                    )

    except yt_dlp.utils.DownloadCancelled:
        logger.debug(f"[yt-dlp] Download cancelled: {label}")
        # yt-dlp keeps its .part/.ytdl/fragment files on purpose so a later run
        # can resume. A user-initiated cancel is not a "later run" — nothing
        # resumes a cancelled queue item, so the leftovers are pure garbage
        # (and for Direct Link jobs they sit in the user's library folder).
        _cleanup_partial_downloads(output_path, reason="cancelled")
        raise RuntimeError("Download cancelled")

    except Exception:
        # Same reasoning for an external cancel that surfaced as something
        # other than DownloadCancelled (e.g. the ffmpeg merge step being
        # killed). A genuine error keeps its leftovers so the retry can resume.
        if cancel_event is not None and cancel_event.is_set():
            _cleanup_partial_downloads(output_path, reason="cancelled")
        raise

    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_active_count -= 1
            _ffmpeg_progress.update(
                percent=0.0,
                time="",
                speed="",
                bandwidth="",
                downloaded_mb=0.0,
                total_mb=0.0,
                eta_sec=0,
                active=_ffmpeg_active_count > 0,
                phase="" if _ffmpeg_active_count == 0 else "download",
            )


def _hoster_subtitle_fallback(episode, raw_path, headers):
    """Fetch subtitles from the hoster's player config when yt-dlp found none.

    These hosters do not list subtitle renditions in the HLS master playlist —
    their player loads them separately from its own config — so yt-dlp, which
    only ever sees the manifest, reports "no subtitles" even for a source whose
    web player shows a working CC menu. The extractors already parse that
    config to get the stream URL; ``extractors.get_subtitles_for`` re-reads it
    for the track list.

    Written as sidecars using yt-dlp's own naming so the existing collect/mux
    path picks them up without knowing the difference. Returns [] on any
    failure — this is a best-effort second attempt, not a required step.
    """
    try:
        try:
            from ...extractors import get_subtitles_for
        except ImportError:
            from mediaforge.extractors import get_subtitles_for

        tracks = get_subtitles_for(
            episode.provider_url,
            getattr(episode, "selected_provider", None),
            headers=headers or None,
        )
        if not tracks:
            return []
        logger.info(
            "[Subtitles] hoster offers %d track(s) outside the stream manifest: %s",
            len(tracks), ", ".join(t.get("lang", "und") for t in tracks),
        )
        fetch_hoster_subtitles(tracks, raw_path, headers=headers or None)
        return collect_subtitle_files(raw_path)
    except Exception as exc:
        logger.debug("[Subtitles] hoster fallback failed: %s", exc)
        return []


def _subtitle_search_meta(episode):
    """What OpenSubtitles needs to identify the title, from any episode model.

    Every model spells this differently (and a direct link has none of it), so
    everything is read defensively: a missing field just means one less search
    parameter, and the moviehash path does not need any of them.
    """
    def _attr(*names):
        for name in names:
            try:
                value = getattr(episode, name, None)
            except Exception:
                value = None
            if value not in (None, "", 0):
                return value
        return None

    season = _attr("season", "season_number")
    # ``file_episode_number`` is the number that matches the file on disk; the
    # plain one can be a page-display number (see the AniWorld absolute-episode
    # option), and OpenSubtitles indexes by the real broadcast number.
    episode_no = _attr("file_episode_number", "episode_number")
    try:
        season = int(season) if season is not None else None
    except (TypeError, ValueError):
        season = None
    try:
        episode_no = int(episode_no) if episode_no is not None else None
    except (TypeError, ValueError):
        episode_no = None

    return {
        "query": _attr("series", "title_en", "title_de", "title"),
        "season": season,
        "episode": episode_no,
        "imdb_id": _attr("imdb_id"),
        "tmdb_id": _attr("tmdb_id"),
    }


def _opensubtitles_fallback(episode, raw_path, found):
    """Fill the still-missing languages from OpenSubtitles.

    Runs last on purpose: the hoster's own tracks are free, already timed to
    the exact stream and cost no quota, so anything the previous two steps
    delivered is kept and only the gaps are bought from the external service.
    Returns the full list of sidecars afterwards. Never raises.
    """
    try:
        if not opensubtitles_enabled():
            return found
        have = {lang for _path, lang in found}
        written = _os_fetch_missing_subtitles(
            raw_path, have_langs=have, meta=_subtitle_search_meta(episode)
        )
        if not written:
            return found
        return collect_subtitle_files(raw_path)
    except Exception as exc:
        logger.debug("[OpenSubtitles] fallback failed: %s", exc)
        return found


def _module_subtitle_fallback(episode, raw_path, found):
    """Ask every subtitle source a third-party module registered.

    Runs after OpenSubtitles for the same reason OpenSubtitles runs after the
    hoster: by this point only the genuinely missing languages are left, so a
    module source is never asked for something already on disk. One failing
    source must not stop the next one, so each is guarded on its own.
    """
    try:
        try:
            from ...subtitle_sources import iter_subtitle_sources
        except ImportError:
            from mediaforge.subtitle_sources import iter_subtitle_sources
        sources = iter_subtitle_sources()
    except Exception:
        return found
    if not sources:
        return found

    meta = _subtitle_search_meta(episode)
    changed = False
    for source in sources:
        have = {lang for _path, lang in found}
        try:
            written = source["fetch"](raw_path, have, meta)
        except Exception as exc:
            logger.warning("[Subtitles] source %r failed: %s", source["source_id"], exc)
            continue
        if written:
            changed = True
            found = collect_subtitle_files(raw_path)
    return collect_subtitle_files(raw_path) if changed else found


def _gather_subtitles(episode, raw_path, headers, want_hoster=True):
    """All three subtitle sources for *raw_path*, cheapest first.

    yt-dlp's sidecars -> the hoster's out-of-band player config -> OpenSubtitles.
    Split out so the three download branches (full / audio-only / video-only)
    cannot drift apart, which they had already started to do.
    """
    found = collect_subtitle_files(raw_path) if want_hoster else []
    if want_hoster and not found:
        found = _hoster_subtitle_fallback(episode, raw_path, headers)
    found = _opensubtitles_fallback(episode, raw_path, found)
    found = _module_subtitle_fallback(episode, raw_path, found)
    _log_subtitle_result(getattr(episode, "_file_name", ""), found)
    return found


def _log_subtitle_result(file_name, subs):
    """Say what the subtitle fetch produced — including when it produced nothing.

    A source that simply carries no selectable subtitle renditions is the normal
    case on these sites (their "German Sub" variants have the text burned into
    the picture), and silence there is indistinguishable from a broken feature.
    Logged at INFO for the same reason the quality check is.
    """
    if subs:
        logger.info(
            "[Subtitles] %s — found %d track(s): %s",
            file_name, len(subs), ", ".join(lang for _, lang in subs),
        )
    else:
        logger.info(
            "[Subtitles] %s — source offers no selectable subtitle tracks "
            "(burned-in subtitles cannot be extracted)",
            file_name,
        )


def _embed_subtitles(video_path, subs, label="", cancel_event=None):
    """Mux subtitle sidecars into *video_path* as tagged soft-sub tracks.

    Rewrites the file in place (temp file + replace) so callers keep using the
    path they already hold and nothing downstream needs to know this ran.

    Runs as the last step before the finished file is moved to its destination,
    deliberately *after* any upscale pass: upscaling re-encodes through its own
    ffmpeg invocation that maps video only, so subtitles added earlier would be
    dropped again.

    Never raises. A failed subtitle mux leaves the original file untouched and
    logs a warning — losing a subtitle track is not worth failing a download
    that has already fetched several gigabytes.
    """
    video_path = Path(video_path)
    if not subs or not video_path.exists():
        cleanup_subtitle_files(subs)
        return video_path

    out_path = video_path.with_name(video_path.stem + ".subbed.mkv")
    try:
        inputs = [ffmpeg.input(str(video_path))]
        kwargs = {"c": "copy", "c:s": "srt"}
        for idx, (sub_path, lang) in enumerate(subs):
            inputs.append(ffmpeg.input(str(sub_path)))
            kwargs[f"metadata:s:s:{idx}"] = f"language={lang}"

        # ffmpeg-python emits "-map 0 -map 1 …" for multiple inputs, i.e. every
        # stream of the video plus each subtitle file — the same pattern the
        # existing track mux relies on.
        _run_ffmpeg_with_progress(
            ffmpeg.output(*inputs, str(out_path), **kwargs),
            label=(label + " [subs]") if label else "",
            cancel_event=cancel_event,
        )
        if not out_path.exists() or out_path.stat().st_size <= 0:
            raise RuntimeError("subtitle mux produced no output")

        out_path.replace(video_path)
        logger.info(
            "[Subtitles] %d track(s) muxed in: %s",
            len(subs), ", ".join(lang for _, lang in subs),
        )
    except Exception as exc:
        logger.warning("[Subtitles] could not mux subtitles, keeping video as-is: %s", exc)
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
    finally:
        cleanup_subtitle_files(subs)

    return video_path


def _replace_destination(dst, replace_existing):
    """Delete *dst* before a quality upgrade overwrites it.

    ``_move_with_progress`` falls back to a chunked copy when ``rename()`` hits
    an existing file (Windows never overwrites on rename), which would stream
    gigabytes through Python for what is a same-device move. Removing the old
    file first keeps the fast rename path — and only ever runs when the quality
    check already decided this exact file is to be replaced.
    """
    if not replace_existing:
        return
    dst = Path(dst)
    try:
        if dst.exists():
            dst.unlink()
    except OSError as exc:
        # Not fatal: the move below still succeeds via the copy path.
        logger.warning("[QUALITY UPGRADE] could not remove %s: %s", dst, exc)


def _move_with_progress(src, dst, label="", cancel_event=None):
    """Move *src* to *dst* while streaming progress into _ffmpeg_progress.

    If src and dst are on the same device the move is an instant rename and
    100 % is reported immediately.  Otherwise a chunked copy is performed so
    the Web UI can show a real progress bar with speed and ETA.
    """
    src, dst = Path(src), Path(dst)
    total = src.stat().st_size

    global _ffmpeg_active_count
    with _ffmpeg_progress_lock:
        _ffmpeg_active_count += 1
        _ffmpeg_progress.update(
            percent=0.0, time="", speed="", fps="", bandwidth="",
            downloaded_mb=0.0, total_mb=round(total / 1_048_576, 1),
            eta_sec=0, active=True, phase="move",
        )

    try:
        # Try fast same-device rename first
        try:
            src.rename(dst)
            with _ffmpeg_progress_lock:
                _ffmpeg_progress.update(percent=100.0, eta_sec=0, speed="")
            return
        except OSError:
            pass  # cross-device — fall through to chunked copy

        CHUNK = 4 * 1024 * 1024  # 4 MB
        import time as _time
        copied = 0
        start = _time.time()

        try:
            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                while True:
                    chunk = fsrc.read(CHUNK)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    copied += len(chunk)
                    elapsed = _time.time() - start or 0.001
                    pct   = copied / total * 100 if total else 100.0
                    speed = copied / elapsed          # bytes/s
                    eta   = int((total - copied) / speed) if speed > 0 else 0
                    speed_str = f"{speed / 1_048_576:.1f} MB/s"
                    with _ffmpeg_progress_lock:
                        _ffmpeg_progress.update(
                            percent=round(pct, 1),
                            downloaded_mb=round(copied / 1_048_576, 1),
                            speed=speed_str,
                            bandwidth=speed_str,
                            eta_sec=eta,
                        )
                    if cancel_event is not None and cancel_event.is_set():
                        break
        except Exception:
            # A failed cross-device copy used to leave the truncated file at
            # the destination: the cancel path below removed it, the error
            # path did not. MediaScan, the dupe check and Jellyfin/Plex all
            # then saw the episode as present, so the next sync skipped it --
            # and what was there could not be played.
            try:
                if dst.exists():
                    dst.unlink()
            except OSError:
                logger.warning("Unvollständige Zieldatei %s konnte nicht entfernt werden", dst)
            raise

        if cancel_event is not None and cancel_event.is_set():
            if dst.exists():
                dst.unlink()
            if src.exists():
                src.unlink()
            raise RuntimeError("Download cancelled")
        src.unlink()
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(percent=100.0, eta_sec=0, speed="")

    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_active_count -= 1
            _ffmpeg_progress.update(
                percent=0.0, time="", speed="", bandwidth="", downloaded_mb=0.0,
                total_mb=0.0, eta_sec=0,
                active=_ffmpeg_active_count > 0,
                phase="" if _ffmpeg_active_count == 0 else _ffmpeg_progress.get("phase", ""),
            )



def download(self, cancel_event=None):
    """Download required audio/video streams for an episode and mux them into
    the final .mkv, skipping any language/track already present on disk.

    Used directly by AniworldEpisode, SerienstreamEpisode, FilmPalastEpisode
    and MegakinoEpisode/MegakinoMovie (all assigned as
    ``download = episode_download``). VeeV is handled here as well, routed to
    extractors.provider.veev because its CDN validates the browser TLS
    fingerprint -- see _download_via_hoster().
    HanimeEpisode does NOT use this -- it has its own single-stream
    download() with no language/provider selection to reconcile.
    """
    # Fail fast and in a language the queue can explain: everything below
    # either muxes with ffmpeg or probes with ffprobe. ensure_ffmpeg() is
    # cached process-wide, so this costs a dict lookup after the first episode
    # instead of the DependencyManager construction + winget attempt it used
    # to run for every single one.
    ensure_ffmpeg()

    if _download_via_hoster(self, cancel_event=cancel_event):
        return True

    try:
        # Where the finished file goes. Normally the episode's own path, but the
        # audio-track merge can redirect it onto an existing copy of the same
        # episode that a *different* language job downloaded earlier (see
        # dupecheck.find_existing_variant): the new audio track is muxed into
        # that file instead of writing a second, near-identical one.
        target_path = self._episode_path
        merged_into_existing = False
        if not target_path.exists() and audio_merge_enabled():
            variant = find_existing_variant(self._episode_path)
            if variant is not None:
                target_path = variant
                merged_into_existing = True
                logger.info(
                    "[TrackMerge] %s -> muxing into existing %s",
                    self.selected_language, target_path.name,
                )

        check = check_downloaded(target_path)

        headers = PROVIDER_HEADERS_D.get(_effective_provider(self), {})

        # episode.stream_url is a plain property, not a cached one: every read
        # re-runs the hoster extractor, which is a fresh HTTP round trip and, on
        # VOE, a fresh captcha risk. It also hands back a *different*, freshly
        # signed URL each time, so the quality probe and the download that
        # follows would otherwise be looking at two different tokens. Resolve
        # once per download() and reuse -- lazily, because an episode that is
        # skipped must not pay for (or fail on) an extractor run at all.
        _stream_url_memo = []

        def _stream_url():
            if not _stream_url_memo:
                _stream_url_memo.append(self.stream_url)
            return _stream_url_memo[0]

        input_kwargs = {
            "reconnect": 1,
            "reconnect_streamed": 1,
            "reconnect_delay_max": 60,  # wait up to 5 min for connection recovery
}
        if headers:
            header_list = [f"{k}: {v}" for k, v in headers.items()]
            input_kwargs["headers"] = "\r\n".join(header_list) + "\r\n"

        url = (getattr(self, "url", "") or "").lower()
        is_serienstream = ("serienstream.to" in url) or ("s.to" in url)

        # s.to (models/s_to/episode.py) defines its own Audio/Subtitles enums,
        # separate from mediaforge.config's LANG_KEY_MAP/INVERSE_LANG_LABELS used
        # by AniWorld -- the two enum classes are not interchangeable, so this
        # function can't just isinstance()-check the episode. `_normalize_language`
        # only exists on SerienstreamEpisode, so hasattr() is used as the
        # discriminator between the two language systems.
        if is_serienstream and hasattr(self, "_normalize_language"):
            audio_enum, sub_enum = self._normalize_language(self.selected_language)
            audio_code = {"German": "deu", "English": "eng", "Japanese": "jpn"}.get(
                getattr(audio_enum, "value", None)
            )
            if not audio_code:
                raise ValueError(
                    f"Unsupported audio language for serienstream.to: {audio_enum}"
                )
            wants_clean_video = (sub_enum is None) or (getattr(sub_enum, "value", None) == "None")
            sub_video_code = None if wants_clean_video else {"German": "deu"}.get(getattr(sub_enum, "value", None))
        else:
            selected_key = INVERSE_LANG_LABELS[self.selected_language]
            audio_enum, sub_enum = LANG_KEY_MAP[selected_key]

            audio_code = LANG_CODE_MAP[audio_enum]
            wants_clean_video = sub_enum == Subtitles.NONE
            sub_video_code = None if wants_clean_video else LANG_CODE_MAP[sub_enum]

        has_video = bool(check["video_langs"])
        has_audio = audio_code in check["audio_langs"]

        need_audio = not has_audio
        if not has_video:
            need_video = True
        elif not wants_clean_video:
            need_video = sub_video_code not in check["video_langs"]
        else:
            need_video = False

        # Every wanted track is already in the file. Before booking this as
        # "already present", offer the quality check a look: the copy on disk may
        # be a 720p from months ago while the hoster now serves 1080p. Skipped
        # for a merge target, because replacing a file that belongs to another
        # language job would throw away tracks this job never had.
        replace_existing = False
        if not need_audio and not need_video:
            if merged_into_existing or not quality_upgrade_enabled():
                logger.debug(f"[SKIPPED] {self._file_name}")
                return False

            existing_q = {
                "height": check.get("height"),
                "width": check.get("width"),
                "bitrate": check.get("bitrate"),
            }
            # Enumerates the source's formats without fetching payload, so a
            # "not better" answer costs one request instead of a full download.
            candidate_q = probe_remote_quality(_stream_url(), headers)
            better, reason = is_better_quality(existing_q, candidate_q)

            # INFO, not DEBUG: this decides whether a multi-GB re-download
            # happens, and at DEBUG the default log level swallowed it entirely
            # -- leaving no way to tell a working check that found nothing
            # better from a check that failed to probe the source at all.
            logger.info(
                "[QualityCheck] %s — on disk: %sp / %s kbit/s, source: %s — %s",
                self._file_name,
                existing_q.get("height") or "?",
                round((existing_q.get("bitrate") or 0) / 1000) or "?",
                (f"{candidate_q['height']}p / "
                 f"{round(candidate_q['bitrate'] / 1000) or '?'} kbit/s")
                if candidate_q else "could not be probed",
                "upgrading" if better else f"keeping ({reason})",
            )
            if not better:
                return False

            need_audio = True
            need_video = True
            replace_existing = True

        # target_path.parent, not self._folder_path: when merging into another
        # language's file the destination folder is that file's, and creating
        # this job's own folder would leave an empty directory behind.
        os.makedirs(target_path.parent, exist_ok=True)

        # Label for CLI progress bar (e.g. "Title S01E001")
        ep_label = os.path.splitext(self._file_name)[0] if self._file_name else ""

        full_stream_needed = need_audio and need_video

        # All intermediate files go to the local temp drive to avoid writing
        # partial data to the destination.  Only the finished file is moved there.
        os.makedirs(_MEDIAFORGE_TEMP_DIR, exist_ok=True)
        _stem = self._episode_path.stem
        temp_audio = _MEDIAFORGE_TEMP_DIR / f"{_stem}.temp_audio.mkv"
        temp_video = _MEDIAFORGE_TEMP_DIR / f"{_stem}.temp_video.mkv"
        temp_full  = _MEDIAFORGE_TEMP_DIR / f"{_stem}.temp_full.mkv"
        # Raw yt-dlp download files (before ffmpeg metadata pass)
        raw_full  = _MEDIAFORGE_TEMP_DIR / f"{_stem}.raw_full.mkv"
        raw_audio = _MEDIAFORGE_TEMP_DIR / f"{_stem}.raw_audio.mkv"
        raw_video = _MEDIAFORGE_TEMP_DIR / f"{_stem}.raw_video.mkv"

        _impersonate = None

        # Subtitle sidecars yt-dlp writes during whichever download branch runs.
        # Collected here and muxed in once, right before the finished file is
        # moved — see _embed_subtitles for why it has to be that late.
        # Two independent opt-ins: the hoster's own tracks (on by default,
        # free) and the OpenSubtitles lookup (off by default, external account
        # and quota). Either one alone is reason enough to run the collect/mux
        # path, but only the first one makes yt-dlp write sidecars.
        _want_hoster_subs = subtitles_enabled()
        _want_subs = _want_hoster_subs or opensubtitles_enabled()
        _subtitle_files = []

        if full_stream_needed:
            logger.debug("[DOWNLOADING] full stream via yt-dlp (concurrent HLS)")

            # 1. Fast HLS download with yt-dlp (parallel segments)
            _run_ytdlp_download(
                _stream_url(), raw_full, headers=headers, label=ep_label,
                cancel_event=cancel_event, impersonate=_impersonate,
                audio_lang=audio_code, want_subtitles=_want_hoster_subs,
            )
            if _want_subs:
                _subtitle_files = _gather_subtitles(
                    self, raw_full, headers, want_hoster=_want_hoster_subs
                )

            # 2. Apply codec + language metadata via ffmpeg (local file → fast)
            stream_metadata = {"metadata:s:a:0": f"language={audio_code}"}
            if (not wants_clean_video) and sub_video_code:
                stream_metadata["metadata:s:v:0"] = f"language={sub_video_code}"

            _enc_vcodec, _enc_acodec, _enc_vopts, _enc_global = _get_ffmpeg_codec_opts_for_download()
            _enc_node = ffmpeg.input(str(raw_full)).output(
                str(temp_full),
                vcodec=_enc_vcodec,
                acodec=_enc_acodec,
                **_enc_vopts,
                **stream_metadata,
            )
            if _enc_global:
                _enc_node = _enc_node.global_args(*_enc_global)
            _run_ffmpeg_with_progress(
                _enc_node,
                label=ep_label + " [tag]",
                cancel_event=cancel_event,
            )
            if raw_full.exists():
                raw_full.unlink()

            # replace_existing means the quality check decided the file on disk
            # is worse than what the hoster now serves — the new stream replaces
            # it outright instead of being muxed alongside the old tracks.
            if target_path.exists() and not replace_existing:
                inputs = [
                    ffmpeg.input(str(target_path)),
                    ffmpeg.input(str(temp_full)),
                ]
                output_path = _MEDIAFORGE_TEMP_DIR / f"{_stem}.new.mkv"
                _run_ffmpeg_with_progress(
                    ffmpeg.output(*inputs, str(output_path), c="copy"),
                    cancel_event=cancel_event,
                )
                _to_move1 = _maybe_upscale_before_move(output_path, ep_label, cancel_event)
                _to_move1 = _embed_subtitles(_to_move1, _subtitle_files, ep_label, cancel_event)
                _move_with_progress(_to_move1, target_path, label=ep_label, cancel_event=cancel_event)
            else:
                _to_move2 = _maybe_upscale_before_move(temp_full, ep_label, cancel_event)
                _to_move2 = _embed_subtitles(_to_move2, _subtitle_files, ep_label, cancel_event)
                _replace_destination(target_path, replace_existing)
                _move_with_progress(_to_move2, target_path, label=ep_label, cancel_event=cancel_event)

            if temp_full.exists():
                temp_full.unlink()
            # Tell the queue worker where the file really landed — with a track
            # merge that is not self._episode_path, and the size / NFO /
            # post-encode hooks all key off this.
            self._last_output_path = target_path
            return True

        def _dl_audio(cancel_event=None, process_ref=None):
            logger.debug("[DOWNLOADING] audio stream via yt-dlp (concurrent HLS)")
            # 1. Download full HLS stream with yt-dlp (fast parallel segments)
            _run_ytdlp_download(
                _stream_url(), raw_audio, headers=headers,
                label=ep_label + " [A]", cancel_event=cancel_event,
                impersonate=_impersonate, audio_lang=audio_code,
                want_subtitles=_want_hoster_subs,
            )
            if _want_subs:
                _found = _gather_subtitles(
                    self, raw_audio, headers, want_hoster=_want_hoster_subs
                )
                _subtitle_files.extend(_found)
            # 2. Extract audio + apply language tag via ffmpeg (local → fast copy)
            _enc_vcodec_a, _enc_acodec_a, _enc_vopts_a, _enc_global_a = _get_ffmpeg_codec_opts_for_download()
            _run_ffmpeg_with_progress(
                ffmpeg.input(str(raw_audio)).output(
                    str(temp_audio),
                    acodec=_enc_acodec_a,
                    map="0:a:0?",
                    **{"metadata:s:a:0": f"language={audio_code}"},
                ),
                label=ep_label + " [A-tag]",
                cancel_event=cancel_event,
            )
            if raw_audio.exists():
                raw_audio.unlink()

        def _dl_video(cancel_event=None, process_ref=None):
            logger.debug("[DOWNLOADING] video stream via yt-dlp (concurrent HLS)")
            # 1. Download full HLS stream with yt-dlp (fast parallel segments)
            _run_ytdlp_download(
                _stream_url(), raw_video, headers=headers,
                label=ep_label + " [V]", cancel_event=cancel_event,
                impersonate=_impersonate, want_subtitles=_want_hoster_subs,
            )
            if _want_subs:
                _found = _gather_subtitles(
                    self, raw_video, headers, want_hoster=_want_hoster_subs
                )
                _subtitle_files.extend(_found)
            # 2. Extract video + apply language tag via ffmpeg (local → fast copy)
            _enc_vcodec_v, _enc_acodec_v, _enc_vopts_v, _enc_global_v = _get_ffmpeg_codec_opts_for_download()
            _enc_node_v = ffmpeg.input(str(raw_video)).output(
                str(temp_video),
                vcodec=_enc_vcodec_v,
                map="0:v:0?",
                **_enc_vopts_v,
                **(
                    {}
                    if wants_clean_video
                    else {"metadata:s:v:0": f"language={sub_video_code}"}
                ),
            )
            if _enc_global_v:
                _enc_node_v = _enc_node_v.global_args(*_enc_global_v)
            _run_ffmpeg_with_progress(
                _enc_node_v,
                label=ep_label + " [V-tag]",
                cancel_event=cancel_event,
            )
            if raw_video.exists():
                raw_video.unlink()

        if need_audio and need_video:
            import threading as _th
            _exc = [None, None]
            _cancel = _th.Event()
            _proc_a = [None]  # holds the audio ffmpeg Popen
            _proc_v = [None]  # holds the video ffmpeg Popen
            # Bridge external cancel_event → internal _cancel
            if cancel_event is not None:
                def _ext_watcher():
                    cancel_event.wait()
                    _cancel.set()
                _th.Thread(target=_ext_watcher, daemon=True).start()

            def _run_audio():
                try:
                    _dl_audio(_cancel, _proc_a)
                except Exception as e:
                    _exc[0] = e
                    # Kill the video process if still running
                    _cancel.set()
                    if _proc_v[0] is not None:
                        try:
                            _proc_v[0].kill()
                        except Exception:
                            pass

            def _run_video():
                try:
                    _dl_video(_cancel, _proc_v)
                except Exception as e:
                    _exc[1] = e
                    # Kill the audio process if still running
                    _cancel.set()
                    if _proc_a[0] is not None:
                        try:
                            _proc_a[0].kill()
                        except Exception:
                            pass

            t_a = _th.Thread(target=_run_audio, daemon=True)
            t_v = _th.Thread(target=_run_video, daemon=True)
            t_a.start()
            t_v.start()
            t_a.join()
            t_v.join()
            if _exc[0]:
                raise _exc[0]
            if _exc[1]:
                raise _exc[1]
        elif need_audio:
            _dl_audio(cancel_event=cancel_event)
        elif need_video:
            _dl_video(cancel_event=cancel_event)

        logger.debug("[MUXING] combining streams")
        inputs = (
            [ffmpeg.input(str(target_path))]
            if target_path.exists()
            else []
        )

        if need_audio:
            inputs.append(ffmpeg.input(str(temp_audio)))
        if need_video:
            inputs.append(ffmpeg.input(str(temp_video)))

        output_path = _MEDIAFORGE_TEMP_DIR / f"{_stem}.new.mkv"
        _run_ffmpeg_with_progress(
            ffmpeg.output(*inputs, str(output_path), c="copy"),
            cancel_event=cancel_event,
        )
        _to_move = _maybe_upscale_before_move(output_path, ep_label, cancel_event)
        _to_move = _embed_subtitles(_to_move, _subtitle_files, ep_label, cancel_event)
        _move_with_progress(_to_move, target_path, label=ep_label, cancel_event=cancel_event)

        for f in (temp_audio, temp_video):
            if f.exists():
                f.unlink()

        # See the note at the other success return: the merge path writes into
        # another language's file, so the nominal episode path may not exist.
        self._last_output_path = target_path
        return True

    except Exception:
        # Clean up temp files from failed attempt (both destination and temp dir)
        _stem_exc = self._episode_path.stem
        for suffix in (
            ".temp_full.mkv", ".temp_audio.mkv", ".temp_video.mkv", ".new.mkv",
            ".raw_full.mkv", ".raw_audio.mkv", ".raw_video.mkv",
        ):
            for candidate in (
                self._episode_path.with_suffix(suffix),
                _MEDIAFORGE_TEMP_DIR / f"{_stem_exc}{suffix}",
            ):
                if candidate.exists():
                    candidate.unlink()

        # The list above only covers the *finished* intermediates. A download
        # that was cancelled or died mid-stream also leaves yt-dlp's own
        # .part/.ytdl/fragment files behind, named after those same stems —
        # sweep them too, otherwise a cancelled multi-GB episode keeps its
        # fragments in the temp dir until the OS cleans it up.
        for _raw in (".raw_full", ".raw_audio", ".raw_video"):
            _cleanup_partial_downloads(
                _MEDIAFORGE_TEMP_DIR / f"{_stem_exc}{_raw}.mkv", reason="failed attempt"
            )
            # Subtitle sidecars sit next to those raw files under the same stem
            # and survive the suffix sweep above, which only knows about .mkv.
            cleanup_subtitle_files(
                collect_subtitle_files(_MEDIAFORGE_TEMP_DIR / f"{_stem_exc}{_raw}.mkv")
            )
        # _embed_subtitles names its temp after whichever intermediate it was
        # handed (".temp_full.subbed.mkv", ".new.subbed.mkv", …), so match by glob.
        for _subbed in _MEDIAFORGE_TEMP_DIR.glob(f"{_stem_exc}*.subbed.mkv"):
            try:
                _subbed.unlink()
            except OSError:
                pass

        _remove_empty_dirs(self._folder_path, self._base_folder)
        raise


def _maybe_upscale_before_move(src_path, ep_label, cancel_event=None):
    """Upscale src_path BEFORE it is moved to the final destination.

    Returns the path that should be moved:
    - If upscaling is enabled and succeeds → upscaled temp file
    - Otherwise → src_path unchanged

    Progress is written into _ffmpeg_progress (phase="upscaling") so the
    normal download queue modal shows it — NOT the upscale queue.
    """
    import threading as _threading
    from pathlib import Path as _Path

    try:
        from ...web.db import get_setting
    except ImportError:
        try:
            from mediaforge.web.db import get_setting
        except ImportError:
            return src_path

    mode = get_setting("upscaling_mode", "disabled")
    if mode != "during_download":
        return src_path

    # Per-download upscale flag (set by queue worker via thread-local)
    try:
        from ...playwright import captcha as _captcha_mod
        if not getattr(_captcha_mod._local, "upscale", False):
            return src_path
    except ImportError:
        try:
            from mediaforge.playwright import captcha as _captcha_mod
            if not getattr(_captcha_mod._local, "upscale", False):
                return src_path
        except ImportError:
            pass

    try:
        from ...anime4k.anime4k import upscale_file, get_upscale_progress
    except ImportError:
        try:
            from mediaforge.anime4k.anime4k import upscale_file, get_upscale_progress
        except ImportError:
            return src_path

    src = _Path(src_path)
    if not src.exists():
        return src_path

    tmp_out = src.with_suffix(".upscaled_tmp.mkv")

    settings = {
        "preset":     get_setting("upscaling_shader_preset", "B"),
        "quality":    get_setting("upscaling_shader_quality", "high"),
        "resolution": get_setting("upscaling_resolution", "1080p"),
        "engine":     get_setting("upscaling_engine", "auto"),
        "out_vcodec": get_setting("upscaling_out_vcodec", "libx264"),
        "out_crf":    int(get_setting("upscaling_out_crf", "18") or "18"),
        "out_preset": get_setting("upscaling_out_preset", "medium"),
    }

    # Signal upscaling phase to the download queue UI via _ffmpeg_progress
    with _ffmpeg_progress_lock:
        _ffmpeg_progress.update(active=True, phase="upscaling", percent=0.0,
                                speed="", time="", fps="", eta_sec=0)

    # Background thread: copy _upscale_progress percent → _ffmpeg_progress every 2 s
    _stop = _threading.Event()
    def _progress_loop():
        while not _stop.wait(2.0):
            try:
                prog = get_upscale_progress()
                with _ffmpeg_progress_lock:
                    _ffmpeg_progress.update(
                        phase="upscaling",
                        active=True,
                        percent=prog.get("percent", 0.0),
                        speed=prog.get("speed", ""),
                        time=prog.get("time", ""),
                        eta_sec=prog.get("eta_sec", 0),
                    )
            except Exception:
                pass
    _pt = _threading.Thread(target=_progress_loop, daemon=True)
    _pt.start()

    logger.info(f"[Anime4K] Starte Upscaling vor Move: {src.name}")
    try:
        upscale_file(
            input_path=str(src),
            output_path=str(tmp_out),
            settings=settings,
            cancel_event=cancel_event,
            label=ep_label,
        )
        _stop.set()
        _pt.join(timeout=3)

        if tmp_out.exists():
            src.unlink(missing_ok=True)
            logger.info(f"[Anime4K] Upscaling fertig: {tmp_out.name}")
            return tmp_out
        return src_path

    except Exception as exc:
        _stop.set()
        _pt.join(timeout=3)
        logger.error(f"[Anime4K] Upscaling fehlgeschlagen: {exc}")
        if tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        return src_path
    finally:
        with _ffmpeg_progress_lock:
            _ffmpeg_progress.update(active=False, phase="", percent=0.0)



def watch(self):
    """Play the stream directly in mpv/IINA (no download to disk).

    Used by AniworldEpisode, SerienstreamEpisode, FilmPalastEpisode,
    MegakinoEpisode, MegakinoMovie and HanimeEpisode (all alias
    ``watch = episode_watch``). AniSkip flags are only honoured when the
    episode object actually has a `skip_times` attribute (AniWorld only).
    """

    print(f"[WATCHING] {self._file_name}")

    headers = PROVIDER_HEADERS_W.get(_effective_provider(self), {})
    cmd = [str(get_player_path()), self.stream_url]

    # AniSkip: AniWorld only; ignore for s.to
    aniskip_enabled = os.getenv("MEDIAFORGE_ANISKIP", "0") == "1"
    if aniskip_enabled and hasattr(self, "skip_times"):
        skip_times = self.skip_times
    else:
        skip_times = None

    if skip_times:
        from ...aniskip import build_mpv_flags, setup_aniskip

        setup_aniskip()
        skip_flags = build_mpv_flags(skip_times).split()
        cmd.extend(skip_flags)
        logger.debug(f"[SKIP TIMES FOUND]: {skip_flags}")

    cmd.extend(
        ["--no-ytdl", "--fs", "--quiet", f"--force-media-title={self._file_name}"]
    )

    if headers:
        header_args = [f"{k}: {v}" for k, v in headers.items()]
        cmd.append("--http-header-fields=" + ",".join(header_args))

    logger.debug(shlex.join(cmd))
    subprocess.run(cmd)


def syncplay(self):
    """Watch the current episode in a synced Syncplay room shared with others.

    Used by AniworldEpisode, SerienstreamEpisode, FilmPalastEpisode,
    MegakinoEpisode, MegakinoMovie and HanimeEpisode (all alias
    ``syncplay = episode_syncplay``). The room name is derived from the
    file name (and optionally a shared password), so viewers watching the
    same episode land in the same room without prior coordination.
    """

    print(f"[Syncplaying] {self._file_name}")

    # TODO: implement IINA support for syncplay (Syncplay may not detect IINA binary reliably)
    # Force mpv for now (get_player_path() reads this env var)
    os.environ["MEDIAFORGE_USE_IINA"] = "0"

    syncplay_host = os.getenv("MEDIAFORGE_SYNCPLAY_HOST") or "syncplay.pl:8998"
    syncplay_password = os.getenv("MEDIAFORGE_SYNCPLAY_PASSWORD")

    # getpass.getuser() is usually fine, but can fail in some environments
    syncplay_username = os.getenv("MEDIAFORGE_SYNCPLAY_USERNAME")

    if not syncplay_username:
        try:
            syncplay_username = getpass.getuser()
        except Exception:
            syncplay_username = "MediaForge"

    room = "AniWorld"
    file_name = self._file_name.replace(" ", "_")

    if syncplay_password:
        # Log what we're using to derive the room (helps debugging)
        logger.debug(f"{room}-{file_name}-[REDACTED]")
        room += (
            "-"
            + hashlib.sha256(
                f"-{file_name}-{syncplay_password}".encode("utf-8")
            ).hexdigest()
        )
    else:
        logger.debug(f"{room}-{file_name}")
        room += f"-{file_name}"

    syncplay_room = os.getenv("MEDIAFORGE_SYNCPLAY_ROOM") or room

    logger.debug(room)

    cmd = [
        str(get_syncplay_path()),
        "--no-gui",
        "--no-store",
        "--host",
        syncplay_host,
        "--room",
        syncplay_room,
        "--name",
        syncplay_username,
        "--player-path",
        str(get_player_path()),
        self.stream_url,
        # "/Users/phoenixthrush/Downloads/Caramelldansen.webm",
    ]

    # MPV flags come after this
    cmd.append("--")

    aniskip_enabled = os.getenv("MEDIAFORGE_ANISKIP", "0") == "1"
    skip_times = self.skip_times if aniskip_enabled else None

    if skip_times:
        from ...aniskip import build_mpv_flags, setup_aniskip

        setup_aniskip()
        skip_flags = build_mpv_flags(skip_times).split()
        cmd.extend(skip_flags)
        logger.debug(f"[SKIP TIMES FOUND]: {skip_flags}")

    cmd.extend(
        ["--no-ytdl", "--fs", "--quiet", f"--force-media-title={self._file_name}"]
    )

    headers = PROVIDER_HEADERS_W.get(_effective_provider(self), {})

    if headers:
        header_args = [f"{k}: {v}" for k, v in headers.items()]
        cmd.append("--http-header-fields=" + ",".join(header_args))

    logger.debug("\n" + shlex.join(cmd))
    subprocess.run(cmd)


if __name__ == "__main__":
    from mediaforge.models import AniworldEpisode

    ep = AniworldEpisode(
        "https://aniworld.to/anime/stream/highschool-dxd/staffel-1/episode-1"
    )

    ep.syncplay()
