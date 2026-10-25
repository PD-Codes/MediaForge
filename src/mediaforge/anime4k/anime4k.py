"""Anime4K GLSL shader pipeline: fetching/installing shader packs and
upscaling video files (via mpv encode-mode or ffmpeg's libplacebo filter).

Two independent code paths live here:
  - upscale_file() and friends: the current on-demand upscaling flow used by
    the web UI (mediaforge.web.routes.upscale, mediaforge.web.upscale_worker),
    which renders through bundled/downloaded GLSL shaders without touching
    the user's mpv config.
  - anime4k() and friends (get_anime4k_urls, download_anime4k,
    extract_anime4k, setup_anime4k, remove_anime4k_lines, detect_current_mode):
    a legacy mode that installs shaders and config snippets directly into the
    user's mpv config directory. download_anime4k()/extract_anime4k() are
    still reused by the web upscale route to fetch shader packs; anime4k()
    itself is not currently called from the web app.
"""

import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    from ..common import get_latest_github_release, unzip
    from ..config import MEDIAFORGE_CONFIG_DIR, GLOBAL_SESSION, MPV_CONFIG_DIR, logger
except ImportError:
    from mediaforge.common import get_latest_github_release, unzip
    from mediaforge.config import (
        MEDIAFORGE_CONFIG_DIR,
        GLOBAL_SESSION,
        MPV_CONFIG_DIR,
        logger,
    )

# ---------------------------------------------------------------------------
# Shader preset definitions (Anime4K standard modes A/B/C/D)
# Each entry lists shader basenames in order.  Files are resolved against the
# extracted shaders/ folder at runtime; missing files are skipped gracefully.
# ---------------------------------------------------------------------------

SHADER_PRESETS = {
    "A": {
        "label": "Mode A – Schnell",
        "desc":  "Gute Balance aus Geschwindigkeit und Qualität. Empfohlen für schwächere CPUs.",
        "shaders": [
            "Anime4K_Clamp_Highlights.glsl",
            "Anime4K_Restore_CNN_Soft_VL.glsl",
            "Anime4K_Upscale_CNN_x2_VL.glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Upscale_CNN_x2_M.glsl",
        ],
    },
    "B": {
        "label": "Mode B – Ausgewogen",
        "desc":  "Schärfer als Mode A. Guter Standard für die meisten Anime.",
        "shaders": [
            "Anime4K_Clamp_Highlights.glsl",
            "Anime4K_Restore_CNN_VL.glsl",
            "Anime4K_Upscale_CNN_x2_VL.glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Upscale_CNN_x2_M.glsl",
        ],
    },
    "C": {
        "label": "Mode C – Qualität",
        "desc":  "Hohe Qualität mit zweistufigem Upscaling. Langsamer.",
        "shaders": [
            "Anime4K_Clamp_Highlights.glsl",
            "Anime4K_Restore_CNN_Soft_VL.glsl",
            "Anime4K_Upscale_CNN_x2_VL.glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Restore_CNN_Soft_M.glsl",
            "Anime4K_Upscale_CNN_x2_M.glsl",
        ],
    },
    "D": {
        "label": "Mode D – Ultra",
        "desc":  "Beste Qualität. Sehr langsam – nur für leistungsstarke PCs.",
        "shaders": [
            "Anime4K_Clamp_Highlights.glsl",
            "Anime4K_Restore_CNN_VL.glsl",
            "Anime4K_Upscale_CNN_x2_VL.glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Restore_CNN_M.glsl",
            "Anime4K_Upscale_CNN_x2_M.glsl",
        ],
    },
}

RESOLUTION_MAP = {
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k":    (3840, 2160),
}

# ---------------------------------------------------------------------------
# Live progress tracking (mirrors _ffmpeg_progress in
# mediaforge.models.common.common, the download/encode pipeline module)
# ---------------------------------------------------------------------------

_upscale_progress_lock = threading.Lock()
_upscale_progress = {
    "active":   False,
    "percent":  0.0,
    "time":     "",
    "speed":    "",
    "eta_sec":  0,
    "phase":    "",   # "upscaling"
    "file":     "",
}


def get_upscale_progress():
    """Return a snapshot dict of the current upscale progress state.

    Used by: mediaforge.web.routes.upscale (progress-poll endpoint),
    mediaforge.web.upscale_worker, and mediaforge.models.common.common
    (mirrored into the download-queue progress display during upscaling).
    """
    with _upscale_progress_lock:
        return dict(_upscale_progress)


# ---------------------------------------------------------------------------
# Shader helpers
# ---------------------------------------------------------------------------

def get_anime4k_folder_names():
    """Return platform-specific Anime4K folder names."""
    platform_folders = {
        "win":    {"low": "GLSL_Windows_Low-end",     "high": "GLSL_Windows_High-end"},
        "linux":  {"low": "GLSL_Mac_Linux_Low-end",   "high": "GLSL_Mac_Linux_High-end"},
        "darwin": {"low": "GLSL_Mac_Linux_Low-end",   "high": "GLSL_Mac_Linux_High-end"},
    }
    for key, folders in platform_folders.items():
        if sys.platform.startswith(key):
            return folders
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def get_shader_dir(quality="high"):
    """Return the shaders/ directory.

    Priority:
      1. Bundled shader/ folder shipped with the package (always available).
      2. Downloaded pack in ~/.mediaforge/Anime4K/ (legacy / manual download).
    """
    # 1. Bundled shaders (co-located with this file)
    bundled = Path(__file__).parent / "shader"
    if bundled.exists() and any(bundled.glob("*.glsl")):
        return bundled

    # 2. Fallback: downloaded pack
    try:
        folder_names = get_anime4k_folder_names()
        pack = folder_names.get(quality, folder_names["high"])
        shader_dir = Path(MEDIAFORGE_CONFIG_DIR) / "Anime4K" / pack / "shaders"
        if shader_dir.exists():
            return shader_dir
        base = Path(MEDIAFORGE_CONFIG_DIR) / "Anime4K"
        if base.exists():
            for d in base.iterdir():
                candidate = d / "shaders"
                if candidate.exists():
                    return candidate
    except Exception:
        pass
    return None


def get_shader_paths(preset_key, quality="high"):
    """Return absolute paths for the given preset, skipping missing files."""
    shader_dir = get_shader_dir(quality)
    if not shader_dir:
        return []

    preset = SHADER_PRESETS.get(preset_key, SHADER_PRESETS["B"])
    paths = []
    for name in preset["shaders"]:
        p = shader_dir / name
        if p.exists():
            paths.append(str(p))
        else:
            logger.debug(f"[Anime4K] Shader not found (skipped): {p}")
    return paths


def list_available_shaders(quality="high"):
    """List all .glsl files in the shaders directory.

    Used by: mediaforge.web.routes.upscale (populates the shader list shown
    in the upscale settings UI).
    """
    shader_dir = get_shader_dir(quality)
    if not shader_dir:
        return []
    return sorted(p.name for p in shader_dir.glob("*.glsl"))


# ---------------------------------------------------------------------------
# Engine availability checks
# ---------------------------------------------------------------------------

def check_mpv_encode_support():
    """Return True if mpv is available and supports --o (encode mode)."""
    try:
        from ..autodeps import get_player_path
    except ImportError:
        try:
            from mediaforge.autodeps import get_player_path
        except ImportError:
            return False
    try:
        mpv = get_player_path()
        r = subprocess.run(
            [str(mpv), "--version"],
            capture_output=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False


def check_libplacebo_support():
    """Return True if ffmpeg has a working libplacebo filter (Vulkan-capable build)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, timeout=15
        )
        if "libplacebo" not in r.stdout:
            return False
        # Filter listed — now verify it actually initialises (needs Vulkan/GPU support)
        test = subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
             "-vf", "libplacebo", "-f", "null", "-"],
            capture_output=True, timeout=15
        )
        return test.returncode == 0
    except Exception:
        return False


_ENGINES = {"value": None}
_ENGINES_LOCK = threading.Lock()


def get_available_engines(refresh=False):
    """Return dict: {engine_name: bool}.

    Used by: upscale_file() to auto-select an engine, and
    mediaforge.web.routes.upscale (reports available engines to the UI).

    Detected once per process: it spawns mpv and ffmpeg and, for libplacebo, a
    Vulkan init test with a 15 s timeout -- and it ran again before every
    single file of a season job. *refresh* (the "check engines" button)
    detects again, e.g. after mpv was installed.
    """
    with _ENGINES_LOCK:
        if refresh or _ENGINES["value"] is None:
            _ENGINES["value"] = {
                "mpv":        check_mpv_encode_support(),
                "libplacebo": check_libplacebo_support(),
            }
        return dict(_ENGINES["value"])


# ---------------------------------------------------------------------------
# Progress parsing helpers
# ---------------------------------------------------------------------------

_MPV_TIME_RE = re.compile(
    r"(?:AV|Enc|ENC|Encoding)[:\s]+(\d+:\d+:\d+(?:\.\d+)?)\s*/\s*(\d+:\d+:\d+(?:\.\d+)?)\s*\((\d+)%\)"
)
_MPV_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)x")


def _parse_time_to_seconds(t):
    """'01:23:45.67' → seconds float."""
    try:
        parts = t.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Core upscaling function
# ---------------------------------------------------------------------------

def upscale_file(
    input_path,
    output_path,
    settings=None,
    cancel_event=None,
    label="",
):
    """
    Upscale *input_path* → *output_path* using Anime4K GLSL shaders.

    settings dict keys:
        preset      : "A" | "B" | "C" | "D"   (default "B")
        quality     : "high" | "low"            (shader pack)
        resolution  : "1080p" | "1440p" | "4k" | None (keep source)
        engine      : "auto" | "mpv" | "libplacebo"
        out_vcodec  : "libx264" | "libx265" | "copy"  (default "libx264")
        out_crf     : int  (default 18)
        out_preset  : str  (default "medium")

    Used by: mediaforge.web.upscale_worker and
    mediaforge.models.common.common (post-download upscale step).
    """
    if settings is None:
        settings = {}

    preset_key   = settings.get("preset", "B")
    quality      = settings.get("quality", "high")
    resolution   = settings.get("resolution", "1080p")
    engine_pref  = settings.get("engine", "auto")
    out_vcodec   = settings.get("out_vcodec", "libx264")
    out_crf      = int(settings.get("out_crf", 18))
    out_preset   = settings.get("out_preset", "medium")

    shader_paths = get_shader_paths(preset_key, quality)
    if not shader_paths:
        raise RuntimeError(
            "Keine Anime4K-Shader gefunden. Bitte zuerst die Shader herunterladen."
        )

    # Determine target resolution
    res_wh = RESOLUTION_MAP.get(resolution) if resolution else None

    # Choose engine
    engines = get_available_engines()
    if engine_pref == "libplacebo" and engines["libplacebo"]:
        use_engine = "libplacebo"
    elif engine_pref == "mpv" and engines["mpv"]:
        use_engine = "mpv"
    elif engine_pref == "auto":
        # Prefer mpv — it's bundled and reliable; libplacebo needs a special Vulkan build
        if engines["mpv"]:
            use_engine = "mpv"
        elif engines["libplacebo"]:
            use_engine = "libplacebo"
        else:
            raise RuntimeError(
                "Kein Upscaling-Engine verfügbar. "
                "mpv oder ffmpeg mit libplacebo wird benötigt."
            )
    else:
        # Fallback
        if engines["mpv"]:
            use_engine = "mpv"
        elif engines["libplacebo"]:
            use_engine = "libplacebo"
        else:
            raise RuntimeError("Kein Upscaling-Engine verfügbar.")

    logger.info(f"[Anime4K] Engine: {use_engine} | Preset: {preset_key} | "
                f"Auflösung: {resolution} | Datei: {Path(input_path).name}")

    with _upscale_progress_lock:
        _upscale_progress.update(
            active=True, percent=0.0, time="", speed="",
            phase="upscaling", file=Path(input_path).name
        )

    try:
        if use_engine == "mpv":
            _upscale_with_mpv(
                input_path, output_path, shader_paths, res_wh,
                out_vcodec, out_crf, out_preset, cancel_event, label
            )
        else:
            _upscale_with_libplacebo(
                input_path, output_path, shader_paths, res_wh,
                out_vcodec, out_crf, out_preset, cancel_event, label
            )
    finally:
        with _upscale_progress_lock:
            _upscale_progress.update(
                active=False, percent=0.0, time="", speed="", phase="", file=""
            )


def _upscale_with_mpv(
    input_path, output_path, shader_paths, res_wh,
    out_vcodec, out_crf, out_preset, cancel_event, label
):
    """Run mpv in encode mode with Anime4K shaders and live progress."""
    try:
        from ..autodeps import get_player_path
    except ImportError:
        from mediaforge.autodeps import get_player_path

    mpv = str(get_player_path())

    # Build shader string (colon-separated on Linux/Mac, semicolon on Windows)
    sep = ";" if sys.platform.startswith("win") else ":"
    shader_str = sep.join(shader_paths)

    # Codec flags for mpv ovc
    ovc_map = {
        "libx264": "libx264",
        "libx265": "libx265",
        "copy":    "copy",
    }
    ovc = ovc_map.get(out_vcodec, "libx264")

    cmd = [
        mpv, str(input_path),
        f"--o={output_path}",
        "--of=matroska",
        f"--ovc={ovc}",
        f"--glsl-shaders={shader_str}",
        "--no-audio-display",
        # Force terminal OSD output even when stdout is piped (no TTY)
        "--term-osd=force",
        # mpv property expansion uses ${prop} syntax, NOT Python %(prop)s
        "--term-status-msg=ENC ${time-pos} / ${duration} (${percent-pos}%)\n",
    ]

    if ovc != "copy":
        cmd += [f"--ovcopts=crf={out_crf},preset={out_preset}"]

    if res_wh:
        w, h = res_wh
        cmd.append(f"--vf=scale={w}:{h}")

    logger.debug(f"[Anime4K/mpv] {' '.join(cmd)}")

    # Use binary + chunk reading — mpv uses \r for in-place updates,
    # which Python's text-mode line iteration never flushes.
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    import time as _time

    _buf = b""
    _split = re.compile(rb"[\r\n]+")
    # Wall-clock ETA tracking
    _eta_wall_start: float = 0.0   # time.time() when first mpv progress line seen
    _eta_vid_start: float  = 0.0   # video position (seconds) at that moment
    _eta_spd_hist: list    = []    # recent (real_speed,) samples for smoothing

    while True:
        chunk = process.stdout.read(256)
        if not chunk:
            break
        _buf += chunk
        parts = _split.split(_buf)
        _buf = parts[-1]           # incomplete tail
        for raw in parts[:-1]:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            logger.debug(f"[mpv] {line}")

            # Primary pattern: "ENC HH:MM:SS / HH:MM:SS DD%"
            m = _MPV_TIME_RE.search(line)
            if m:
                cur_t, tot_t, pct = m.group(1), m.group(2), int(m.group(3))
                sm = _MPV_SPEED_RE.search(line)
                speed_label = sm.group(1) + "x" if sm else ""

                try:
                    _cur_s = _parse_time_to_seconds(cur_t)
                    _tot_s = _parse_time_to_seconds(tot_t)
                    _now   = _time.monotonic()

                    # Initialise wall-clock anchor on first real progress line
                    if _eta_wall_start == 0.0 and _cur_s > 0:
                        _eta_wall_start = _now
                        _eta_vid_start  = _cur_s

                    # Real encoding speed = video-seconds processed / wall-seconds elapsed
                    _elapsed_wall = _now - _eta_wall_start
                    _vid_done     = _cur_s - _eta_vid_start
                    if _elapsed_wall > 1.0 and _vid_done > 0:
                        _real_spd = _vid_done / _elapsed_wall   # e.g. 2.3 → 2.3× realtime
                        # Keep a rolling window of the last 10 samples for smoothing
                        _eta_spd_hist.append(_real_spd)
                        if len(_eta_spd_hist) > 10:
                            _eta_spd_hist.pop(0)
                        _smoothed_spd = sum(_eta_spd_hist) / len(_eta_spd_hist)
                        _eta = int((_tot_s - _cur_s) / _smoothed_spd) if _smoothed_spd > 0 else 0
                        # Use measured speed as label if mpv didn't report one
                        if not speed_label:
                            speed_label = f"{_smoothed_spd:.1f}x"
                    else:
                        _eta = 0
                except Exception:
                    _eta = 0

                with _upscale_progress_lock:
                    _upscale_progress.update(
                        percent=float(pct),
                        time=cur_t,
                        speed=speed_label,
                        eta_sec=max(0, _eta),
                    )
                continue

            # Fallback: any "(DD%)" pattern
            pm = re.search(r"\((\d+)%\)", line)
            if pm:
                with _upscale_progress_lock:
                    _upscale_progress["percent"] = float(pm.group(1))

        if cancel_event and cancel_event.is_set():
            process.kill()
            raise RuntimeError("Upscaling abgebrochen.")

    process.wait()
    if process.returncode not in (0, None) and not (cancel_event and cancel_event.is_set()):
        raise RuntimeError(f"mpv Encode-Fehler (rc={process.returncode})")


def _upscale_with_libplacebo(
    input_path, output_path, shader_paths, res_wh,
    out_vcodec, out_crf, out_preset, cancel_event, label
):
    """Run ffmpeg with libplacebo filter for Anime4K upscaling."""
    # Build libplacebo filter
    lp_args = []
    if res_wh:
        w, h = res_wh
        lp_args += [f"w={w}", f"h={h}"]
    # Pass each shader separately (multiple libplacebo filters chained)
    # Or use custom_shader_path for single shader — use multiple filter instances
    def _esc(p):
        # ffmpeg filter values: escape backslashes and colons, no shell quoting here
        return str(p).replace("\\", "/").replace(":", "\\:")

    if len(shader_paths) == 1:
        lp_args.append(f"custom_shader_path={_esc(shader_paths[0])}")
        vf_chain = "libplacebo=" + ":".join(lp_args)
    else:
        # Chain multiple libplacebo instances for multi-shader presets
        parts = []
        for i, sp in enumerate(shader_paths):
            args_i = list(lp_args) if i == 0 else []
            args_i.append(f"custom_shader_path={_esc(sp)}")
            parts.append("libplacebo=" + ":".join(args_i))
        vf_chain = ",".join(parts)

    cmd = ["ffmpeg", "-i", str(input_path), "-vf", vf_chain]

    if out_vcodec == "copy":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", out_vcodec, "-crf", str(out_crf), "-preset", out_preset]

    cmd += ["-c:a", "copy", "-y", str(output_path)]

    logger.debug(f"[Anime4K/libplacebo] {' '.join(cmd)}")

    # Probe duration for percentage calculation
    total_duration = None
    try:
        import ffmpeg as ffmpeg_mod
        probe = ffmpeg_mod.probe(str(input_path))
        total_duration = float(probe["format"].get("duration", 0))
    except Exception:
        pass

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    _TIME_RE = re.compile(r"time=(\d+:\d+:\d+\.\d+)")

    for line in process.stdout:
        line = line.rstrip()
        if not line:
            continue
        logger.debug(f"[ffmpeg/lp] {line}")

        tm = _TIME_RE.search(line)
        if tm and total_duration:
            secs = _parse_time_to_seconds(tm.group(1))
            pct = min(100.0, secs / total_duration * 100) if total_duration > 0 else 0.0
            speed_m = re.search(r"speed=\s*(\S+)", line)
            speed = speed_m.group(1) if speed_m else ""
            with _upscale_progress_lock:
                _upscale_progress.update(
                    percent=round(pct, 1),
                    time=tm.group(1),
                    speed=speed,
                )

        if cancel_event and cancel_event.is_set():
            process.kill()
            raise RuntimeError("Upscaling abgebrochen.")

    process.wait()
    if process.returncode not in (0, None) and not (cancel_event and cancel_event.is_set()):
        raise RuntimeError(f"ffmpeg libplacebo Fehler (rc={process.returncode})")


# ---------------------------------------------------------------------------
# Original mpv-config functions (kept for backward compat)
# ---------------------------------------------------------------------------

def get_anime4k_urls():
    """Return platform-specific Anime4K GLSL URLs."""
    repo = "Tama47/Anime4K"
    release = get_latest_github_release(repo)
    folder_names = get_anime4k_folder_names()
    base = f"https://github.com/{repo}/releases/download/{release}/"
    return {
        "low":  base + folder_names["low"]  + ".zip",
        "high": base + folder_names["high"] + ".zip",
    }


def download_anime4k(target_dir=None, mode="high"):
    """Download Anime4K GLSL assets only if not already extracted.

    Used by: mediaforge.web.routes.upscale (fetches a shader pack on demand)
    and anime4k() below (legacy mpv-config setup).
    """
    target_dir = Path(target_dir or MEDIAFORGE_CONFIG_DIR) / "Anime4K"
    target_dir.mkdir(parents=True, exist_ok=True)

    if mode == "remove":
        if target_dir.exists():
            shutil.rmtree(target_dir)
            logger.debug(f"[REMOVED] Anime4K directory: {target_dir}")
        return []

    urls = get_anime4k_urls()
    if mode not in urls:
        raise ValueError(f"Invalid mode '{mode}'. Use 'high', 'low', or 'remove'.")

    downloaded_files = []
    url = urls[mode]
    filename = Path(url).name
    extracted_dir = target_dir / Path(filename).stem

    if extracted_dir.exists():
        logger.debug(f"{extracted_dir} exists, skipping download of {filename}")
        downloaded_files.append(target_dir / filename)
    else:
        filepath = target_dir / filename
        logger.debug(f"Downloading {filename}...")
        with GLOBAL_SESSION.get(url, stream=True) as response:
            response.raise_for_status()
            with open(filepath, "wb") as f:
                shutil.copyfileobj(response.raw, f)
        logger.debug(f"Downloaded {filename} to {target_dir}")
        downloaded_files.append(filepath)

    return downloaded_files


def extract_anime4k(files, target_dir=None):
    """Extract downloaded zip files and clean up.

    Used by: mediaforge.web.routes.upscale and anime4k() below.
    """
    target_dir = Path(target_dir or MEDIAFORGE_CONFIG_DIR) / "Anime4K"
    extracted_dirs = []

    for filepath in files:
        extracted_dir = target_dir / filepath.stem
        if extracted_dir.exists():
            logger.debug(f"{extracted_dir} exists, skipping extraction.")
        else:
            unzip(filepath, extracted_dir)
            macosx_dir = extracted_dir / "__MACOSX"
            if macosx_dir.exists():
                shutil.rmtree(macosx_dir)
            logger.debug(f"Extracted {filepath.name} -> {extracted_dir}")
        filepath.unlink(missing_ok=True)
        extracted_dirs.append(extracted_dir)

    return extracted_dirs


def detect_current_mode():
    """Detect the currently installed Anime4K mode from input.conf."""
    input_conf = MPV_CONFIG_DIR / "input.conf"
    if not input_conf.exists():
        return None
    with open(input_conf, "r", encoding="utf-8") as f:
        content = f.read()
    if "# Optimized shaders for lower-end GPU:" in content:
        return "low"
    if "# Optimized shaders for higher-end GPU:" in content:
        return "high"
    return None


def copy_with_markers(src_file, dst_file):
    with open(src_file, "r", encoding="utf-8") as f:
        content = f.read()
    content = f"# BEGIN Anime4K CONFIG\n{content}\n# END Anime4K CONFIG\n"
    with open(dst_file, "w", encoding="utf-8") as f:
        f.write(content)
    logger.debug(f"Copied {src_file} -> {dst_file} with markers")


def setup_anime4k(mode="low"):
    """Copy shaders and config files to MPV directory."""
    mpv_shaders_dir = MPV_CONFIG_DIR / "shaders"
    mpv_shaders_dir.mkdir(parents=True, exist_ok=True)
    mode_folders = get_anime4k_folder_names()
    if mode not in mode_folders:
        logger.error(f"Unknown mode: {mode}. Valid modes: {list(mode_folders.keys())}")
        return
    source_dir = Path(MEDIAFORGE_CONFIG_DIR) / "Anime4K" / mode_folders[mode]
    if not source_dir.exists():
        logger.warning(f"{source_dir} does not exist. Nothing to set up.")
        return
    folder = source_dir
    shaders_dir = folder / "shaders"
    if shaders_dir.exists():
        for shader in shaders_dir.iterdir():
            dst_file = mpv_shaders_dir / shader.name
            if not dst_file.exists():
                shutil.copy(shader, dst_file)
                logger.debug(f"Copied shader {shader} -> {dst_file}")
    for conf_name in ("mpv.conf", "input.conf"):
        src_conf = folder / conf_name
        dst_conf = MPV_CONFIG_DIR / conf_name
        if src_conf.exists() and not dst_conf.exists():
            copy_with_markers(src_conf, dst_conf)


def remove_anime4k_lines(file_path):
    if not file_path.exists():
        return
    start_marker = "# BEGIN Anime4K CONFIG"
    end_marker   = "# END Anime4K CONFIG"
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    final_lines = []
    in_block = False
    for line in lines:
        if line.strip() == start_marker:
            in_block = True
            continue
        if line.strip() == end_marker:
            in_block = False
            continue
        if not in_block:
            final_lines.append(line)
    if not final_lines:
        file_path.unlink()
        logger.debug(f"[REMOVED] {file_path} (empty after removing Anime4K block)")
    else:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(final_lines)
        logger.debug(f"[REMOVED] Anime4K lines from {file_path}")


def anime4k(mode="high"):
    """Legacy entry point: install/remove Anime4K shaders + config snippets
    directly in the user's mpv config directory (mode "high"/"low"/"remove").

    Not currently called from the web app, which instead uses upscale_file()
    to render through shaders on demand without touching the mpv config.
    Kept for CLI/manual use (see the __main__ block below).
    """
    mpv_shaders_dir = MPV_CONFIG_DIR / "shaders"
    if mode not in ("high", "low", "remove"):
        raise ValueError(f"Invalid mode '{mode}'. Use 'high', 'low', or 'remove'.")
    if mode == "remove":
        if mpv_shaders_dir.exists():
            for shader in mpv_shaders_dir.iterdir():
                if shader.is_file() and shader.name.startswith("Anime4K_"):
                    shader.unlink()
                    logger.debug(f"[REMOVED] {shader}")
            if not any(mpv_shaders_dir.iterdir()):
                mpv_shaders_dir.rmdir()
                logger.debug(f"[REMOVED] Empty shaders folder: {mpv_shaders_dir}")
        for conf_name in ("mpv.conf", "input.conf"):
            remove_anime4k_lines(MPV_CONFIG_DIR / conf_name)
        logger.debug("Anime4K assets, shaders, and configs removed successfully.")
        return
    current_mode = detect_current_mode()
    if current_mode == mode:
        logger.debug(f"Anime4K already installed in '{mode}' mode. Skipping setup.")
        return
    elif current_mode is not None and current_mode != mode:
        logger.debug(f"Switching Anime4K from '{current_mode}' to '{mode}' mode...")
        anime4k(mode="remove")
    downloaded = download_anime4k(mode=mode)
    extract_anime4k(downloaded)
    setup_anime4k(mode=mode)
    logger.debug(f"Anime4K setup complete in '{mode}' mode.")


if __name__ == "__main__":
    anime4k(mode="high")
