"""HLS transcoding via ffmpeg: encoder detection, ffprobe metadata, and
session lifecycle (start / share / stop) backing the in-browser player.

Used by: ``web/routes/stream.py`` drives sessions via ``start_session`` /
``start_or_join_session`` / ``get_session`` / ``stop_session`` / ``active_count``
and calls ``probe_file`` / ``detect_available_encoders`` / ``get_best_encoder``
directly; ``web/routes/library.py`` also calls ``probe_file`` for media info.
"""

import os
import json
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
_share_locks: dict = {}         # share_key -> Lock (serialize creation per key)
_share_locks_guard = threading.Lock()
SHARE_EPSILON = 3.0             # seconds: positions within this reuse a session


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

def probe_file(file_path: str, headers: dict | None = None) -> dict | None:
    """Return media info dict or None on failure.

    ``headers`` lets us probe a remote (provider) URL that needs Referer /
    User-Agent set.
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
        cmd += ["-show_format", "-show_streams", str(file_path)]
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
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
    }
    fmt = data.get("format", {})
    info["duration"] = float(fmt.get("duration") or 0)
    for s in data.get("streams", []):
        ct = s.get("codec_type", "")
        if ct == "video" and not info["video_codec"]:
            info["video_codec"] = s.get("codec_name", "unknown")
            info["width"]  = int(s.get("width",  0) or 0)
            info["height"] = int(s.get("height", 0) or 0)
            info["sample_aspect_ratio"]  = s.get("sample_aspect_ratio")
            info["display_aspect_ratio"] = s.get("display_aspect_ratio")
        elif ct == "audio" and not info["audio_codec"]:
            info["audio_codec"] = s.get("codec_name", "unknown")
    return info


# ── TranscodeSession ───────────────────────────────────────────────────────

class TranscodeSession:
    """One ffmpeg HLS transcode (or remux) process writing segments to a temp
    directory, plus the bookkeeping needed to share it between viewers
    (``refs`` / ``share_key``) and detect readiness/failure."""

    def __init__(self, token: str, file_path: str, encoder: str, start_pos: float = 0.0,
                 headers: dict | None = None, copy_video: bool = False,
                 copy_audio: bool = False, display_aspect_ratio: str | None = None):
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

        # ── Video codec ──
        if self.copy_video:
            # Source is already browser-compatible H.264 → just remux (no CPU).
            cmd += ["-c:v", "copy"]
        elif self.encoder == "h264_vaapi":
            cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"]
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
                "-pix_fmt", "yuv420p",
            ]
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
                  display_aspect_ratio: str | None = None) -> tuple:
    """Create + start a session. Returns (token, session) or raises RuntimeError.

    ``headers`` marks the input as a remote URL (stream-from-source) and is
    forwarded to ffmpeg as HTTP request headers. ``copy_video`` / ``copy_audio``
    remux instead of re-encoding when the source is already compatible.
    ``display_aspect_ratio`` (from ffprobe) is forced back onto the output via
    -aspect when re-encoding, so hardware encoders can't reset a non-square
    SAR and change the displayed shape.
    """
    with _sessions_lock:
        if len(_sessions) >= MAX_TRANSCODE_SESSIONS:
            raise RuntimeError(
                f"Zu viele gleichzeitige Transcode-Sessions ({MAX_TRANSCODE_SESSIONS} max). "
                "Bitte warte, bis eine andere Session beendet ist."
            )
    encoder, _ = get_best_encoder()
    if not encoder:
        raise RuntimeError(
            "Kein H.264-Encoder verfügbar. "
            "Bitte ffmpeg mit NVENC/VAAPI/VideoToolbox oder libx264 installieren."
        )
    token   = uuid.uuid4().hex
    session = TranscodeSession(token, file_path, encoder, start_pos, headers=headers,
                               copy_video=copy_video, copy_audio=copy_audio,
                               display_aspect_ratio=display_aspect_ratio)
    with _sessions_lock:
        _sessions[token] = session
    ok = session.start()
    if not ok:
        with _sessions_lock:
            _sessions.pop(token, None)
        session.stop()
        raise RuntimeError(session.error or "Transcoding fehlgeschlagen")
    return token, session


def start_or_join_session(file_path: str, start_pos: float = 0.0, share_key: str | None = None,
                          headers: dict | None = None, copy_video: bool = False,
                          copy_audio: bool = False, display_aspect_ratio: str | None = None) -> tuple:
    """Like ``start_session`` but, when ``share_key`` is given, viewers asking
    for the same file at (nearly) the same position reuse ONE transcode session
    instead of each spawning ffmpeg. Refcounted; released via ``stop_session``."""
    if not share_key:
        return start_session(file_path, start_pos, headers=headers,
                             copy_video=copy_video, copy_audio=copy_audio,
                             display_aspect_ratio=display_aspect_ratio)
    fp = str(file_path)
    sp = max(0.0, float(start_pos))
    with _share_lock(share_key):
        with _sessions_lock:
            tok = _shared.get(share_key)
            sess = _sessions.get(tok) if tok else None
            if (sess is not None and sess.is_alive() and sess.file_path == fp
                    and abs(sp - sess.start_pos) <= SHARE_EPSILON):
                sess.refs += 1
                sess.last_access = time.time()
                return tok, sess
        # No compatible shared session — create one. ffmpeg launch is slow, so it
        # runs under the per-key lock only (not the global _sessions_lock).
        token, session = start_session(fp, sp, headers=headers,
                                       copy_video=copy_video, copy_audio=copy_audio,
                                       display_aspect_ratio=display_aspect_ratio)
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
        for tok, sess in stale:
            logger.info("[Transcoder] stale session cleanup: %s", tok[:8])
            sess.stop()


threading.Thread(target=_cleanup_loop, daemon=True, name="transcoder-cleanup").start()
