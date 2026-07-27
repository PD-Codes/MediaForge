"""HLS transcoding via ffmpeg: encoder detection, ffprobe metadata, and
session lifecycle (start / share / stop) backing the in-browser player.

Used by: ``web/routes/stream.py`` drives sessions via ``start_session`` /
``start_or_join_session`` / ``get_session`` / ``stop_session`` / ``active_count``
and calls ``probe_file`` / ``detect_available_encoders`` / ``get_best_encoder``
directly; ``web/routes/library.py`` also calls ``probe_file`` for media info.
"""

import hashlib
import os
import json
import re
import uuid
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

try:
    from ..logger import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging
    logger = logging.getLogger(__name__)

# ── Active sessions ────────────────────────────────────────────────────────
_sessions: dict = {}
_sessions_lock = threading.Lock()
SESSION_TIMEOUT = 1800  # 30 minutes inactivity
MAX_TRANSCODE_SESSIONS = 8  # max concurrent HLS transcode sessions

# Shared transcode sessions: viewers watching the same file at (nearly) the same
# position — e.g. everyone in a SyncPlay room — reuse ONE ffmpeg process and the
# same HLS segments instead of each spawning their own. Refcounted.
_shared: dict = {}              # share_key -> token
# Tokens whose session is being built right now. They count towards
# MAX_TRANSCODE_SESSIONS so the limit holds while ffmpeg is still starting.
_starting: set = set()
_share_locks: dict = {}         # share_key -> Lock (serialize creation per key)
_share_locks_guard = threading.Lock()
SHARE_EPSILON = 3.0             # seconds: positions within this reuse a session

# Subtitle formats that are pictures rather than text. They cannot become
# WebVTT, so selecting one makes ffmpeg draw it into the frame instead.
BITMAP_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle",
                          "dvdsub", "dvb_subtitle", "xsub"}

# Downscale rungs offered to the player. Only rungs BELOW the source height
# are shown -- upscaling in the transcoder would burn CPU for nothing.
QUALITY_LADDER = (1080, 720, 480, 360)


def _cache_dir(name: str) -> Path:
    """Return (and create) a subdirectory of the app temp dir.

    Falls back to the system temp dir when config cannot be imported --
    this module is also used by the CLI, which does not build the app.
    """
    try:
        from ..config import MEDIAFORGE_TEMP_DIR
        base = Path(MEDIAFORGE_TEMP_DIR)
    except Exception:
        base = Path(tempfile.gettempdir()) / "mediaforge"
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _share_lock(key: str):
    with _share_locks_guard:
        lk = _share_locks.get(key)
        if lk is None:
            lk = _share_locks[key] = threading.Lock()
        return lk


# ── Encoder detection ──────────────────────────────────────────────────────

def _ffmpeg_bin():
    import shutil as _s
    return _s.which("ffmpeg") or "ffmpeg"

def _ffprobe_bin():
    import shutil as _s
    fb = _s.which("ffprobe")
    if fb:
        return fb
    ff = _s.which("ffmpeg") or ""
    return ff.replace("ffmpeg", "ffprobe") if ff else "ffprobe"


_encoder_cache: dict | None = None
_encoder_cache_lock = threading.Lock()


def detect_available_encoders() -> dict:
    """Test each H.264 encoder with a tiny null source. Returns {name: bool}.
    Results are cached for the lifetime of the process."""
    global _encoder_cache
    with _encoder_cache_lock:
        if _encoder_cache is not None:
            return _encoder_cache
        ffmpeg = _ffmpeg_bin()
        import platform
        is_windows = platform.system() == "Windows"

        # Fast test: 1-frame null source
        base_cmd = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1",
            "-vframes", "1",
        ]
        # Helper: check if encoder exists in ffmpeg build (cheap, no GPU needed)
        def _has_encoder_in_list(enc_name):
            try:
                r = subprocess.run(
                    [ffmpeg, "-encoders"], capture_output=True, text=True, timeout=5
                )
                return enc_name in r.stdout
            except Exception:
                return False

        # NVENC needs GPU init — try multiple test strategies
        def _test_nvenc(ff):
            """Try several NVENC invocations, return True if any succeeds."""
            strategies = [
                # NVENC minimum resolution is 145x145 — use 256x256 to be safe
                # Strategy 1: hwaccel cuda
                [ff, "-y", "-hwaccel", "cuda",
                 "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25",
                 "-t", "1.0", "-vf", "format=yuv420p",
                 "-c:v", "h264_nvenc", "-preset", "p1", "-f", "null", "-"],
                # Strategy 2: no hwaccel
                [ff, "-y",
                 "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25",
                 "-t", "1.0", "-vf", "format=yuv420p",
                 "-c:v", "h264_nvenc", "-preset", "p1", "-f", "null", "-"],
                # Strategy 3: legacy preset name
                [ff, "-y",
                 "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25",
                 "-t", "1.0", "-vf", "format=yuv420p",
                 "-c:v", "h264_nvenc", "-preset", "fast", "-f", "null", "-"],
            ]
            for s in strategies:
                try:
                    r = subprocess.run(s, capture_output=True, timeout=12)
                    if r.returncode == 0:
                        return True
                    logger.debug("[Transcoder] nvenc strategy %d failed (full):\n%s",
                                 strategies.index(s)+1,
                                 r.stderr.decode(errors="replace"))
                except Exception as exc:
                    logger.debug("[Transcoder] nvenc strategy %d exception: %s",
                                 strategies.index(s)+1, exc)
            return False

        nvenc_cmd = None  # handled by _test_nvenc above
        vaapi_base = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1",
            "-vframes", "1",
        ]
        sw_base = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25",
            "-t", "0.2",
        ]
        tests = {
            "h264_nvenc":        None,  # handled by _test_nvenc()
            "h264_vaapi":        vaapi_base + ["-vf", "format=nv12,hwupload",
                                               "-c:v", "h264_vaapi", "-f", "null", "-"],
            "h264_videotoolbox": sw_base + ["-c:v", "h264_videotoolbox", "-f", "null", "-"],
            "libx264":           sw_base + ["-c:v", "libx264", "-preset", "ultrafast",
                                            "-f", "null", "-"],
        }
        result = {}
        for name, cmd in tests.items():
            # Skip VAAPI on Windows (DRM-based, Linux only)
            if is_windows and name == "h264_vaapi":
                result[name] = False
                continue
            # Skip VideoToolbox on non-macOS
            if platform.system() != "Darwin" and name == "h264_videotoolbox":
                result[name] = False
                continue
            # Quick compile-time check before running a full test
            if not _has_encoder_in_list(name):
                result[name] = False
                continue
            if name == "h264_nvenc":
                result[name] = _test_nvenc(ffmpeg)
            else:
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=10)
                    result[name] = r.returncode == 0
                    if not result[name]:
                        logger.debug("[Transcoder] %s test failed: %s",
                                     name, r.stderr.decode(errors="replace")[-200:])
                except Exception as exc:
                    logger.debug("[Transcoder] %s test exception: %s", name, exc)
                    result[name] = False
        logger.info("[Transcoder] encoder detection: %s", result)
        _encoder_cache = result
        return result


def reset_encoder_cache():
    """Force re-detection on next call (e.g. after driver install)."""
    global _encoder_cache
    with _encoder_cache_lock:
        _encoder_cache = None


def get_best_encoder() -> tuple:
    """Return (encoder_name, is_hardware) for the best available H.264 encoder."""
    import shutil as _s
    if not _s.which("ffmpeg"):
        return None, False
    enc = detect_available_encoders()
    for name in ("h264_nvenc", "h264_vaapi", "h264_videotoolbox", "libx264"):
        if enc.get(name):
            return name, name != "libx264"
    return None, False


# ── ffprobe ────────────────────────────────────────────────────────────────

def probe_file(file_path: str, headers: dict | None = None, timeout: int = 30) -> dict | None:
    """Return media info dict or None on failure.

    ``headers`` lets us probe a remote (provider) URL that needs Referer /
    User-Agent set. ``timeout`` is the per-call ffprobe budget: 30 s is right
    for a remote stream, but far too generous for a local library scan, where
    thousands of files on an unresponsive network share would otherwise add up
    to hours (see routes/library.py::_lib_scan_base).
    """
    try:
        cmd = [_ffprobe_bin(), "-v", "quiet", "-print_format", "json"]
        if headers:
            hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            if hdr_lines:
                cmd += ["-headers", hdr_lines]
            ua = headers.get("User-Agent") or headers.get("user-agent")
            if ua:
                cmd += ["-user_agent", ua]
        # -show_chapters costs nothing extra (ffprobe already read the
        # container index) and is what feeds the player's chapter marks.
        cmd += ["-show_format", "-show_streams", "-show_chapters", str(file_path)]
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except Exception as exc:
        logger.warning("[Transcoder] ffprobe failed: %s", exc)
        return None

    info = {
        "duration":    0.0,
        "video_codec": None,
        "audio_codec": None,
        "width":       0,
        "height":      0,
        "format":      Path(file_path).suffix.lstrip(".").upper(),
        # Pixel + display aspect ratio, e.g. "1:1" / "12:5". Kept separate from
        # width/height so callers can force the correct DAR back onto a
        # re-encode — some hardware encoders (VAAPI/NVENC via hwupload) drop
        # or reset SAR, which pillarboxes/stretches the picture even though
        # the coded width/height never changed.
        "sample_aspect_ratio":  None,
        "display_aspect_ratio": None,
        # Per-track lists for the player's audio/subtitle pickers. ``index``
        # is the position WITHIN its own kind (ffmpeg's 0:a:N / 0:s:N), not
        # the absolute stream index -- that is the number every -map needs.
        "audio_tracks":    [],
        "subtitle_tracks": [],
        "chapters":        [],
    }
    fmt = data.get("format", {})
    info["duration"] = float(fmt.get("duration") or 0)
    a_idx = s_idx = 0
    for s in data.get("streams", []):
        ct   = s.get("codec_type", "")
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        if ct == "video":
            if not info["video_codec"]:
                info["video_codec"] = s.get("codec_name", "unknown")
                info["width"]  = int(s.get("width",  0) or 0)
                info["height"] = int(s.get("height", 0) or 0)
                info["sample_aspect_ratio"]  = s.get("sample_aspect_ratio")
                info["display_aspect_ratio"] = s.get("display_aspect_ratio")
        elif ct == "audio":
            if not info["audio_codec"]:
                info["audio_codec"] = s.get("codec_name", "unknown")
            info["audio_tracks"].append({
                "index":    a_idx,
                "codec":    s.get("codec_name", ""),
                "language": (tags.get("language") or "").lower(),
                "title":    tags.get("title") or "",
                "channels": int(s.get("channels", 0) or 0),
                "layout":   s.get("channel_layout") or "",
                "default":  bool(disp.get("default")),
            })
            a_idx += 1
        elif ct == "subtitle":
            codec = (s.get("codec_name") or "").lower()
            info["subtitle_tracks"].append({
                "index":    s_idx,
                "codec":    codec,
                "language": (tags.get("language") or "").lower(),
                "title":    tags.get("title") or "",
                "forced":   bool(disp.get("forced")),
                "default":  bool(disp.get("default")),
                # Bitmap formats carry pictures, not text: they cannot be
                # turned into WebVTT and have to be drawn into the frame.
                "burn":     codec in BITMAP_SUBTITLE_CODECS,
            })
            s_idx += 1

    for ch in data.get("chapters", []) or []:
        try:
            start = float(ch.get("start_time") or 0)
            end   = float(ch.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        info["chapters"].append({
            "start": start,
            "end":   end,
            "title": (ch.get("tags", {}) or {}).get("title") or "",
        })
    return info


# ── Subtitle extraction ────────────────────────────────────────────────────

def extract_subtitle_vtt(file_path: str, track_index: int, timeout: int = 120) -> str | None:
    """Convert one text subtitle track to WebVTT and return the cache path.

    Cached per (path, mtime, track): re-running ffmpeg for every toggle of
    the subtitle button would be a full container read each time. Bitmap
    tracks are rejected here -- they have to be burned in instead.
    """
    src = Path(file_path)
    try:
        stat = src.stat()
    except OSError:
        return None

    key = hashlib.sha1(
        f"{src.resolve()}|{int(stat.st_mtime)}|{stat.st_size}|{track_index}".encode()
    ).hexdigest()[:20]
    out_dir = _cache_dir("subs")
    out = out_dir / f"{key}.vtt"
    if out.exists() and out.stat().st_size > 0:
        return str(out)

    tmp = out.with_suffix(f".{uuid.uuid4().hex[:8]}.part")
    cmd = [
        _ffmpeg_bin(), "-y", "-v", "error",
        "-i", str(src),
        "-map", f"0:s:{int(track_index)}",
        "-c:s", "webvtt", "-f", "webvtt",
        str(tmp),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            logger.warning("[Transcoder] subtitle extract failed (%s track %s): %s",
                           src.name, track_index,
                           r.stderr.decode(errors="replace")[-300:])
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(out)
        return str(out)
    except Exception as exc:
        logger.warning("[Transcoder] subtitle extract error: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ── Seek-preview thumbnails ────────────────────────────────────────────────

THUMB_INTERVAL = 10      # one frame every N seconds
THUMB_W        = 160
THUMB_H        = 90
THUMB_COLS     = 10
THUMB_ROWS     = 10
_thumb_jobs: dict = {}   # key -> "running" | float (time of failure)
THUMB_FAIL_TTL = 3600.0  # remember a failed build for an hour, then retry
_thumb_lock = threading.Lock()


def _thumb_key(src: Path) -> str:
    stat = src.stat()
    return hashlib.sha1(
        f"{src.resolve()}|{int(stat.st_mtime)}|{stat.st_size}|v1".encode()
    ).hexdigest()[:20]


def thumbs_status(file_path: str, duration: float = 0.0) -> dict:
    """Describe the seek-preview sprites for a file, generating them once.

    Building them decodes the whole file, so it happens in a background
    thread and callers get ``{"pending": True}`` until the sheets exist.
    The player simply falls back to a plain time bubble in the meantime.
    """
    src = Path(file_path)
    try:
        key = _thumb_key(src)
    except OSError:
        return {"ready": False}

    out_dir = _cache_dir("thumbs") / key
    done    = out_dir / "done.json"
    if done.exists():
        try:
            return {"ready": True, **json.loads(done.read_text("utf-8"))}
        except Exception:
            pass

    now = time.time()
    with _thumb_lock:
        state = _thumb_jobs.get(key)
        if isinstance(state, float):
            # A failure, remembered with its timestamp. Reporting it as
            # "not available" (rather than "pending") is what stops the
            # client from polling forever; the entry expires so a transient
            # cause -- a full temp dir, a busy machine -- gets another go.
            if now - state < THUMB_FAIL_TTL:
                return {"ready": False}
            _thumb_jobs.pop(key, None)
        elif state == "running":
            return {"ready": False, "pending": True}
        running = sum(1 for v in _thumb_jobs.values() if v == "running")
        if running >= 2:
            # Two full decodes at once already eat a machine that is also
            # transcoding; make the third caller come back later. Counting
            # only running jobs matters: counting failures too meant two bad
            # files disabled thumbnails for every other file as well.
            return {"ready": False, "pending": True}
        _thumb_jobs[key] = "running"

    threading.Thread(
        target=_build_thumbs, args=(src, key, out_dir, duration),
        daemon=True, name="mf-thumbs",
    ).start()
    return {"ready": False, "pending": True}


def _build_thumbs(src: Path, key: str, out_dir: Path, duration: float) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        per_sheet = THUMB_COLS * THUMB_ROWS
        cmd = [
            _ffmpeg_bin(), "-y", "-v", "error",
            "-skip_frame", "nokey",           # decode keyframes only: much cheaper
            "-i", str(src),
            "-an", "-sn", "-dn",
            "-vf", (f"fps=1/{THUMB_INTERVAL},scale={THUMB_W}:{THUMB_H}"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={THUMB_W}:{THUMB_H}:-1:-1:color=black,"
                    f"tile={THUMB_COLS}x{THUMB_ROWS}"),
            "-q:v", "6",
            # image2 numbers from 1 unless told otherwise, and the player
            # addresses sheet 0 first -- without this the very first (often
            # only) sheet is unreachable and the preview stays blank.
            "-start_number", "0",
            str(out_dir / "s%d.jpg"),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        sheets = sorted(out_dir.glob("s*.jpg"))
        if r.returncode != 0 or not sheets:
            logger.info("[Transcoder] thumbnail sheet build failed for %s: %s",
                        src.name, r.stderr.decode(errors="replace")[-200:])
            with _thumb_lock:
                _thumb_jobs[key] = time.time()
            return
        count = int((duration or 0) // THUMB_INTERVAL) or (len(sheets) * per_sheet)
        meta = {
            "key": key, "interval": THUMB_INTERVAL, "cols": THUMB_COLS,
            "rows": THUMB_ROWS, "w": THUMB_W, "h": THUMB_H,
            "sheets": len(sheets), "count": count,
        }
        (out_dir / "done.json").write_text(json.dumps(meta), "utf-8")
        logger.info("[Transcoder] thumbnails ready for %s (%d sheets)", src.name, len(sheets))
    except Exception as exc:
        logger.info("[Transcoder] thumbnail build error: %s", exc)
        with _thumb_lock:
            _thumb_jobs[key] = time.time()
            return
    finally:
        with _thumb_lock:
            if _thumb_jobs.get(key) == "running":
                _thumb_jobs.pop(key, None)


def thumb_sheet_path(key: str, sheet: int) -> str | None:
    """Resolve a sprite sheet, refusing anything outside the cache dir."""
    if not re.fullmatch(r"[0-9a-f]{8,40}", key or ""):
        return None
    base = (_cache_dir("thumbs") / key).resolve()
    root = _cache_dir("thumbs").resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return None
    p = (base / f"s{int(sheet)}.jpg").resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return str(p) if p.is_file() else None


# ── TranscodeSession ───────────────────────────────────────────────────────

class TranscodeSession:
    """One ffmpeg HLS transcode (or remux) process writing segments to a temp
    directory, plus the bookkeeping needed to share it between viewers
    (``refs`` / ``share_key``) and detect readiness/failure."""

    def __init__(self, token: str, file_path: str, encoder: str, start_pos: float = 0.0,
                 headers: dict | None = None, copy_video: bool = False,
                 copy_audio: bool = False, display_aspect_ratio: str | None = None,
                 audio_index: int = 0, height: int = 0, burn_sub: int = -1):
        self.token       = token
        self.file_path   = str(file_path)
        self.encoder     = encoder
        self.start_pos   = max(0.0, float(start_pos))
        # When set, the input is a remote URL (stream-from-source) and these
        # HTTP headers (Referer / User-Agent / …) are passed to ffmpeg.
        self.headers     = headers or None
        # Stream-copy instead of re-encode (huge CPU/stutter win when the
        # source is already browser-compatible H.264 / AAC).
        self.copy_video  = bool(copy_video)
        self.copy_audio  = bool(copy_audio)
        # Source DAR (e.g. "12:5"), from ffprobe. Only used when re-encoding
        # (copy_video is False) — forced back onto the output via -aspect so
        # hardware encoders (VAAPI/NVENC) can't silently reset a non-square
        # SAR to 1:1 and pillarbox/stretch the picture.
        self.display_aspect_ratio = display_aspect_ratio or None
        # Which audio track to map (0:a:N), which height to downscale to and
        # which bitmap subtitle to draw into the picture. All three can only
        # be changed by starting a new ffmpeg run, which is why the player
        # restarts the session when the user picks a different one.
        self.audio_index = max(0, int(audio_index or 0))
        self.height      = max(0, int(height or 0))
        self.burn_sub    = int(burn_sub if burn_sub is not None else -1)
        # A downscale or a burned-in subtitle both need pixels, so they
        # cancel any stream-copy that was requested.
        if self.height or self.burn_sub >= 0:
            self.copy_video = False
        self.tmp_dir     = None
        self.process     = None
        self.playlist_path = None
        self.ready       = False
        self.error: str | None = None
        self.last_access = time.time()
        self._playlist_ready = threading.Event()
        self.refs        = 1       # viewers sharing this session
        self.share_key   = None    # set when this is a shared (e.g. SyncPlay) session
        self._stderr_buf: deque = deque(maxlen=200)  # ring buffer for ffmpeg stderr

    # ------------------------------------------------------------------
    def _filter_graph(self) -> str | None:
        """Build the -filter_complex chain for downscale / burned-in subs.

        Returns ``None`` when neither is requested, so the plain (and much
        cheaper) copy/encode path stays untouched. The bitmap subtitle is
        overlaid from the SAME input, which avoids quoting a file path into
        a filter string -- a Windows path with a colon and backslashes is
        close to unquotable there.
        """
        steps = []
        label = "0:v:0"
        # Order matters: a bitmap subtitle is authored at the SOURCE
        # resolution (a 1080p PGS cue sits at y~900), so it has to be drawn
        # onto the full-size frame and scaled down together with it.
        # Overlaying after the scale pushed the cues off the picture.
        if self.burn_sub >= 0:
            steps.append(f"[{label}][0:s:{self.burn_sub}]overlay[vsub]")
            label = "vsub"
        if self.height:
            # -2 keeps the width even (H.264 needs it) and preserves the ratio.
            steps.append(f"[{label}]scale=-2:{self.height}[vscaled]")
            label = "vscaled"
        if not steps:
            return None
        if self.encoder == "h264_vaapi":
            # VAAPI encodes from GPU surfaces, so the uploaded frame has to
            # be the last stage of the graph -- the plain "-vf
            # format=nv12,hwupload" below cannot coexist with
            # -filter_complex on the same output.
            steps.append(f"[{label}]format=nv12,hwupload[vhw]")
            label = "vhw"
        # Give the last stage the name the -map above expects.
        steps[-1] = steps[-1].rsplit("[", 1)[0] + "[vout]"
        return ";".join(steps)

    # ------------------------------------------------------------------
    def _build_cmd(self) -> list:
        ffmpeg = _ffmpeg_bin()
        seg    = os.path.join(self.tmp_dir, "seg%06d.ts")
        cmd    = [ffmpeg]

        # ── Remote source (stream-from-provider): resilient HTTP input ──
        if self.headers:
            cmd += [
                # Regenerate presentation timestamps — provider HLS streams are
                # often variable-frame-rate / have irregular PTS, which makes the
                # video (not audio) stutter in the browser. genpts + CFR below
                # normalise this.
                "-fflags", "+genpts",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "60",
            ]
            hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
            if hdr_lines:
                cmd += ["-headers", hdr_lines]
            ua = self.headers.get("User-Agent") or self.headers.get("user-agent")
            if ua:
                cmd += ["-user_agent", ua]

        if self.start_pos > 1.0:
            cmd += ["-ss", str(self.start_pos)]

        cmd += ["-i", self.file_path]

        # ── Stream selection ──
        # Without an explicit -map, ffmpeg picks one video + one audio stream
        # by its own heuristics, which is why the old build could only ever
        # play the "first" language of a multi-audio file.
        filter_graph = self._filter_graph()
        if filter_graph:
            cmd += ["-filter_complex", filter_graph, "-map", "[vout]"]
        else:
            cmd += ["-map", "0:v:0?"]
        cmd += ["-map", f"0:a:{self.audio_index}?"]
        # Never let a subtitle stream reach the HLS muxer: it cannot carry
        # them and aborts the whole run with "could not find tag".
        cmd += ["-sn", "-dn"]

        # ── Video codec ──
        if self.copy_video:
            # Source is already browser-compatible H.264 → just remux (no CPU).
            cmd += ["-c:v", "copy"]
        elif self.encoder == "h264_vaapi":
            # The hwupload already sits in the filter graph when there is one.
            if not filter_graph:
                cmd += ["-vf", "format=nv12,hwupload"]
            cmd += ["-c:v", "h264_vaapi"]
        elif self.encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"]
        elif self.encoder == "h264_videotoolbox":
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", "4M"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

        # ── Remote-source normalisation (re-encode only) ──
        # Force a constant frame rate and regular keyframes so the browser gets
        # an evenly-paced, keyframe-aligned HLS stream. This fixes the "audio
        # fine, picture stutters" problem on variable-frame-rate provider feeds.
        seg_time = "4"
        if self.headers and not self.copy_video:
            cmd += [
                "-vsync", "cfr",
                "-force_key_frames", "expr:gte(t,n_forced*2)",
            ]
            # -pix_fmt names a SOFTWARE format. After a VAAPI hwupload the
            # frames live on the GPU, so asking for yuv420p there aborts the
            # run with "Impossible to convert between the formats". The
            # hwupload already pins nv12, which is what this was for.
            if self.encoder != "h264_vaapi":
                cmd += ["-pix_fmt", "yuv420p"]
            seg_time = "2"

        # ── Aspect ratio safety net (re-encode only) ──
        # Some hardware encoder paths (VAAPI's hwupload, NVENC) don't reliably
        # carry a non-square sample_aspect_ratio through to the output, which
        # silently changes the displayed shape even though width/height are
        # untouched. Forcing -aspect from the probed source DAR pins the
        # container-level display ratio regardless of what the encoder does
        # with SAR internally. Not needed (or safe) in copy mode.
        if not self.copy_video and self.display_aspect_ratio:
            cmd += ["-aspect", self.display_aspect_ratio]

        # ── Audio ──
        if self.copy_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]

        # ── HLS output ──
        cmd += [
            "-threads", "0",            # use all CPU cores
            "-avoid_negative_ts", "make_zero",
            "-f", "hls",
            "-hls_time", seg_time,
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", seg,
            self.playlist_path,
        ]
        return cmd

    def _drain_stderr(self) -> None:
        """Read ffmpeg stderr continuously so the pipe never fills and blocks."""
        try:
            for line in self.process.stderr:
                self._stderr_buf.append(line)
        except Exception:
            pass

    def start(self) -> bool:
        """Launch ffmpeg and return immediately — no blocking wait."""
        self.tmp_dir       = tempfile.mkdtemp(prefix=f"aw_stream_{self.token[:8]}_")
        self.playlist_path = os.path.join(self.tmp_dir, "index.m3u8")
        cmd = self._build_cmd()
        logger.info("[Transcoder] start %s  enc=%s  file=%s",
                    self.token[:8], self.encoder, self.file_path)
        logger.debug("[Transcoder] cmd: %s", " ".join(cmd))
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            self.ready = True
            # Drain stderr continuously to prevent pipe buffer deadlock
            drain = threading.Thread(target=self._drain_stderr, daemon=True)
            drain.start()
            # Signal when playlist is ready
            t = threading.Thread(target=self.wait_for_playlist, daemon=True)
            t.start()
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def wait_for_playlist(self, timeout: float = 45.0) -> bool:
        """Block (in a background thread) until the first .ts appears, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                self.error = "ffmpeg exited unexpectedly"
                try:
                    tail = b"".join(self._stderr_buf)
                    self.error += ": " + tail.decode(errors="replace")[-600:]
                except Exception:
                    pass
                self.ready = False
                self._playlist_ready.set()
                return False
            if os.path.exists(self.playlist_path):
                try:
                    with open(self.playlist_path) as _pf:
                        if ".ts" in _pf.read():
                            self._playlist_ready.set()
                            return True
                except Exception:
                    pass
            time.sleep(0.25)
        self.error = "Timeout: kein Segment innerhalb von 45 s generiert"
        self.ready = False
        self._playlist_ready.set()
        return False

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception as e:
                logger.debug("[Transcoder] terminate failed for %s: %s — trying kill", self.token[:8], e)
                try:
                    self.process.kill()
                except Exception as e2:
                    logger.warning("[Transcoder] kill failed for %s: %s", self.token[:8], e2)
            # Close the pipes explicitly instead of waiting for the garbage
            # collector: every session holds an ffmpeg stderr pipe, and on a
            # long-running server that is a file descriptor per stopped stream.
            for _pipe in (self.process.stdout, self.process.stderr, self.process.stdin):
                try:
                    if _pipe is not None:
                        _pipe.close()
                except Exception:
                    pass
            self.process = None
        if self.tmp_dir and os.path.exists(self.tmp_dir):
            try:
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning("[Transcoder] cleanup failed for %s: %s", self.token[:8], e)
        logger.info("[Transcoder] stopped %s", self.token[:8])

    def touch(self):
        self.last_access = time.time()

    def is_alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)


# ── Public API ─────────────────────────────────────────────────────────────

def start_session(file_path: str, start_pos: float = 0.0, headers: dict | None = None,
                  copy_video: bool = False, copy_audio: bool = False,
                  display_aspect_ratio: str | None = None,
                  audio_index: int = 0, height: int = 0, burn_sub: int = -1) -> tuple:
    """Create + start a session. Returns (token, session) or raises RuntimeError.

    ``headers`` marks the input as a remote URL (stream-from-source) and is
    forwarded to ffmpeg as HTTP request headers. ``copy_video`` / ``copy_audio``
    remux instead of re-encoding when the source is already compatible.
    ``display_aspect_ratio`` (from ffprobe) is forced back onto the output via
    -aspect when re-encoding, so hardware encoders can't reset a non-square
    SAR and change the displayed shape.
    """
    token = uuid.uuid4().hex
    # Reserve the slot in the SAME critical section that checks the limit.
    # Checking first and inserting only after get_best_encoder() (which shells
    # out to ffmpeg and takes a moment) let every concurrent starter pass the
    # check while _sessions was still empty -- so N viewers hitting play at the
    # same time produced N ffmpeg processes no matter what the limit said.
    with _sessions_lock:
        if len(_sessions) + len(_starting) >= MAX_TRANSCODE_SESSIONS:
            raise RuntimeError(
                f"Zu viele gleichzeitige Transcode-Sessions ({MAX_TRANSCODE_SESSIONS} max). "
                "Bitte warte, bis eine andere Session beendet ist."
            )
        _starting.add(token)
    try:
        encoder, _ = get_best_encoder()
        if not encoder:
            raise RuntimeError(
                "Kein H.264-Encoder verfügbar. "
                "Bitte ffmpeg mit NVENC/VAAPI/VideoToolbox oder libx264 installieren."
            )
        session = TranscodeSession(token, file_path, encoder, start_pos, headers=headers,
                                   copy_video=copy_video, copy_audio=copy_audio,
                                   display_aspect_ratio=display_aspect_ratio,
                                   audio_index=audio_index, height=height, burn_sub=burn_sub)
        with _sessions_lock:
            _sessions[token] = session
    finally:
        with _sessions_lock:
            _starting.discard(token)
    ok = session.start()
    if not ok:
        with _sessions_lock:
            _sessions.pop(token, None)
        session.stop()
        raise RuntimeError(session.error or "Transcoding fehlgeschlagen")
    return token, session


def start_or_join_session(file_path: str, start_pos: float = 0.0, share_key: str | None = None,
                          headers: dict | None = None, copy_video: bool = False,
                          copy_audio: bool = False, display_aspect_ratio: str | None = None,
                          audio_index: int = 0, height: int = 0, burn_sub: int = -1) -> tuple:
    """Like ``start_session`` but, when ``share_key`` is given, viewers asking
    for the same file at (nearly) the same position reuse ONE transcode session
    instead of each spawning ffmpeg. Refcounted; released via ``stop_session``."""
    if not share_key:
        return start_session(file_path, start_pos, headers=headers,
                             copy_video=copy_video, copy_audio=copy_audio,
                             display_aspect_ratio=display_aspect_ratio,
                             audio_index=audio_index, height=height, burn_sub=burn_sub)
    fp = str(file_path)
    sp = max(0.0, float(start_pos))
    with _share_lock(share_key):
        with _sessions_lock:
            tok = _shared.get(share_key)
            sess = _sessions.get(tok) if tok else None
            # Only join a session that produces the SAME picture and sound:
            # a viewer who picked another audio track or a downscale must not
            # be handed someone else's segments.
            if (sess is not None and sess.is_alive() and sess.file_path == fp
                    and abs(sp - sess.start_pos) <= SHARE_EPSILON
                    and sess.audio_index == max(0, int(audio_index or 0))
                    and sess.height == max(0, int(height or 0))
                    and sess.burn_sub == int(burn_sub if burn_sub is not None else -1)):
                sess.refs += 1
                sess.last_access = time.time()
                return tok, sess
        # No compatible shared session — create one. ffmpeg launch is slow, so it
        # runs under the per-key lock only (not the global _sessions_lock).
        token, session = start_session(fp, sp, headers=headers,
                                       copy_video=copy_video, copy_audio=copy_audio,
                                       display_aspect_ratio=display_aspect_ratio,
                                       audio_index=audio_index, height=height, burn_sub=burn_sub)
        session.share_key = share_key
        with _sessions_lock:
            _shared[share_key] = token
        return token, session


def get_session(token: str) -> "TranscodeSession | None":
    with _sessions_lock:
        sess = _sessions.get(token)
    if sess:
        sess.touch()
    return sess


def stop_session(token: str):
    sess = None
    with _sessions_lock:
        s = _sessions.get(token)
        if s is not None and getattr(s, "refs", 1) > 1:
            # Shared session still in use by other viewers — drop one reference.
            s.refs -= 1
            s.last_access = time.time()
            return
        sess = _sessions.pop(token, None)
        if sess is not None and getattr(sess, "share_key", None):
            if _shared.get(sess.share_key) == token:
                _shared.pop(sess.share_key, None)
    if sess:
        sess.stop()


def active_count() -> int:
    with _sessions_lock:
        return len(_sessions)


# ── Background cleanup ─────────────────────────────────────────────────────

def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        stale = []
        with _sessions_lock:
            for tok, sess in list(_sessions.items()):
                if now - sess.last_access > SESSION_TIMEOUT:
                    stale.append((tok, sess))
                    del _sessions[tok]
                    # stop_session() drops this mapping, the timeout path did
                    # not -- so an expired shared session left its share_key
                    # pointing at a token that no longer exists, and the next
                    # viewer of that file looked it up before falling through
                    # to a fresh session anyway.
                    share_key = getattr(sess, "share_key", None)
                    if share_key and _shared.get(share_key) == tok:
                        _shared.pop(share_key, None)
        for tok, sess in stale:
            logger.info("[Transcoder] stale session cleanup: %s", tok[:8])
            sess.stop()


threading.Thread(target=_cleanup_loop, daemon=True, name="transcoder-cleanup").start()
