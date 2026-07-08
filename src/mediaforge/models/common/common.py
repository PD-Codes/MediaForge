"""Site-agnostic episode-action implementations shared by every model family.

AniworldEpisode and SerienstreamEpisode assign these directly
(``download = episode_download`` etc. in their episode.py); FilmPalastEpisode,
MegakinoEpisode and MegakinoMovie do the same for watch()/syncplay() but wrap
download() so they can special-case the VeeV provider (which needs a
dedicated curl_cffi/Playwright path instead of the yt-dlp+ffmpeg pipeline
here). HanimeEpisode aliases watch()/syncplay() from here too, but has its
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
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading as _threading
from html import unescape as _html_unescape
from pathlib import Path
from typing import Tuple

import ffmpeg

from ...autodeps import DependencyManager

try:
    from ...autodeps import get_player_path, get_syncplay_path
    from ...config import (
        INVERSE_LANG_LABELS,
        LANG_CODE_MAP,
        LANG_KEY_MAP,
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
        PROVIDER_HEADERS_D,
        PROVIDER_HEADERS_W,
        Audio,
        Subtitles,
        get_video_codec,
        logger,
    )


# Precompile regex for forbidden filename characters
FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*]')


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
_MEDIAFORGE_TEMP_DIR = Path(tempfile.gettempdir()) / "mediaforge"

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


def clean_title(title: str) -> str:
    """Clean a string to make it safe for use as a filename."""
    # Unescape HTML entities first (e.g. &amp; → &) before stripping forbidden chars
    return FORBIDDEN_CHARS.sub("", _html_unescape(title)).strip()


def check_downloaded(episode_path):
    result = {
        "exists": False,
        "video_langs": set(),
        "audio_langs": set(),
    }

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

    def warning(self, msg):
        # Suppress known harmless HLS noise
        if "Live HLS streams are not supported" not in msg:
            logger.warning(f"[yt-dlp] {msg}")

    def error(self, msg):
        logger.error(f"[yt-dlp] {msg}")


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

    # Use shorter stats_period for smoother progress (1s in non-debug, 10s in debug)
    stats_period = "10" if debug_mode else "1"

    args = ffmpeg.compile(node, overwrite_output=overwrite_output)
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
        detail = "\n".join(stderr_lines[-20:]) if stderr_lines else f"exit code {process.returncode}"
        logger.error(f"[FFmpeg] Process failed (rc={process.returncode}):\n{detail}")
        raise RuntimeError(f"ffmpeg error (rc={process.returncode}): {detail}")


def _run_ytdlp_download(url, output_path, headers=None, label="", cancel_event=None, impersonate=None, audio_lang=None, format_override=None):
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

            percent = (downloaded / total * 100.0) if total > 0 else 0.0
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
                matches = sorted(output_path.parent.glob(output_path.stem + ".*"))
                if matches:
                    matches[0].rename(expected)
                    logger.debug(f"[yt-dlp] Renamed {matches[0].name} → {expected.name}")
                else:
                    raise RuntimeError(
                        f"yt-dlp output not found near: {output_path}"
                    )

    except yt_dlp.utils.DownloadCancelled:
        logger.debug(f"[yt-dlp] Download cancelled: {label}")
        raise RuntimeError("Download cancelled")

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


def _move_with_progress(src, dst, label="", cancel_event=None):
    """Move *src* to *dst* while streaming progress into _ffmpeg_progress.

    If src and dst are on the same device the move is an instant rename and
    100 % is reported immediately.  Otherwise a chunked copy is performed so
    the Web UI can show a real progress bar with speed and ETA.
    """
    import stat as _stat
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

    Used directly by AniworldEpisode and SerienstreamEpisode (assigned as
    ``download = episode_download``). FilmPalastEpisode.download() and
    MegakinoEpisode/MegakinoMovie.download() call this too for every
    provider except VeeV, which is routed to extractors.provider.veev
    instead because its CDN validates the browser TLS fingerprint.
    HanimeEpisode does NOT use this -- it has its own single-stream
    download() with no language/provider selection to reconcile.
    """
    if platform.system() == "Windows":
        manager = DependencyManager()
        manager.fetch_binary("ffmpeg")

    try:
        check = check_downloaded(self._episode_path)

        headers = PROVIDER_HEADERS_D.get(self.selected_provider, {})
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

        if not need_audio and not need_video:
            logger.debug(f"[SKIPPED] {self._file_name}")
            return False

        os.makedirs(self._folder_path, exist_ok=True)

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

        if full_stream_needed:
            logger.debug("[DOWNLOADING] full stream via yt-dlp (concurrent HLS)")

            # 1. Fast HLS download with yt-dlp (parallel segments)
            _run_ytdlp_download(
                self.stream_url, raw_full, headers=headers, label=ep_label,
                cancel_event=cancel_event, impersonate=_impersonate,
                audio_lang=audio_code,
            )

            # 2. Apply codec + language metadata via ffmpeg (local file → fast)
            stream_metadata = {"metadata:s:a:0": f"language={audio_code}"}
            if (not wants_clean_video) and sub_video_code:
                stream_metadata["metadata:s:v:0"] = f"language={sub_video_code}"

            _enc_vcodec, _enc_acodec, _enc_vopts, _enc_global = _get_ffmpeg_codec_opts()
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

            if self._episode_path.exists():
                inputs = [
                    ffmpeg.input(str(self._episode_path)),
                    ffmpeg.input(str(temp_full)),
                ]
                output_path = _MEDIAFORGE_TEMP_DIR / f"{_stem}.new.mkv"
                _run_ffmpeg_with_progress(
                    ffmpeg.output(*inputs, str(output_path), c="copy"),
                    cancel_event=cancel_event,
                )
                _to_move1 = _maybe_upscale_before_move(output_path, ep_label, cancel_event)
                _move_with_progress(_to_move1, self._episode_path, label=ep_label, cancel_event=cancel_event)
            else:
                _to_move2 = _maybe_upscale_before_move(temp_full, ep_label, cancel_event)
                _move_with_progress(_to_move2, self._episode_path, label=ep_label, cancel_event=cancel_event)

            if temp_full.exists():
                temp_full.unlink()
            return True

        def _dl_audio(cancel_event=None, process_ref=None):
            logger.debug("[DOWNLOADING] audio stream via yt-dlp (concurrent HLS)")
            # 1. Download full HLS stream with yt-dlp (fast parallel segments)
            _run_ytdlp_download(
                self.stream_url, raw_audio, headers=headers,
                label=ep_label + " [A]", cancel_event=cancel_event,
                impersonate=_impersonate, audio_lang=audio_code,
            )
            # 2. Extract audio + apply language tag via ffmpeg (local → fast copy)
            _enc_vcodec_a, _enc_acodec_a, _enc_vopts_a, _enc_global_a = _get_ffmpeg_codec_opts()
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
                self.stream_url, raw_video, headers=headers,
                label=ep_label + " [V]", cancel_event=cancel_event,
                impersonate=_impersonate,
            )
            # 2. Extract video + apply language tag via ffmpeg (local → fast copy)
            _enc_vcodec_v, _enc_acodec_v, _enc_vopts_v, _enc_global_v = _get_ffmpeg_codec_opts()
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
            [ffmpeg.input(str(self._episode_path))]
            if self._episode_path.exists()
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
        _move_with_progress(_to_move, self._episode_path, label=ep_label, cancel_event=cancel_event)

        for f in (temp_audio, temp_video):
            if f.exists():
                f.unlink()

        return True

    except Exception as e:
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

    headers = PROVIDER_HEADERS_W.get(self.selected_provider, {})
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

    headers = PROVIDER_HEADERS_W.get(self.selected_provider, {})

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
