"""Extended, best-effort system-info collection for the ``system_info`` event.

Everything in here is *diagnostic context* for crash/error analysis (see
``events.build_system_info_event``): what kind of machine an install runs on,
whether it is containerised, and — most useful for the transcoding/upscaling
error paths — what hardware-accelerated ffmpeg actually has available.

Design rules (all deliberate):

  * **Collected once, then cached.** ``collect()`` memoises its result in a
    module global, so the (potentially subprocess-touching) detection runs a
    single time per process — at startup, from ``build_system_info_event`` —
    never per telemetry batch. The per-batch envelope in ``client.py`` stays
    subprocess-free on purpose.
  * **Never raises.** Every individual probe is wrapped so a missing file, a
    denied ``/proc`` read, a subprocess that isn't installed or hangs, or an
    odd platform can only ever turn one field into ``None``/``[]`` — it can
    never bubble an exception up into startup or into a telemetry flush.
  * **No new dependency.** stdlib only (``platform``/``os``/``subprocess``/
    ``shutil``/``glob``). This package is meant to stay a lightweight leaf
    (see ``registry.py``'s module docstring) — no psutil, and no import of
    anything under ``mediaforge.web``.
  * **No PII.** Deliberately never collects the hostname, username, MAC/IP,
    or any absolute path. CPU/GPU model strings and the distro name are the
    most identifying things here and are hardware/OS descriptors, not
    personal data — the ``system_info`` consent text spells this out.

The subprocess probes (``nvidia-smi``, ``ffmpeg``) use a hard timeout and are
only run when the binary is actually resolvable on PATH / in MediaForge's
own dependency folder — they are never installed on demand from here (that is
``autodeps.fetch_binary``'s job, and it must not be triggered as a side effect
of telemetry).
"""

import glob
import importlib.metadata
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

# Per-probe hard timeout. Generous enough that a slow first ffmpeg/nvidia-smi
# spawn on an underpowered NAS still completes, small enough to bound the worst
# case — and it only ever runs on the background startup thread that emits the
# system_info event (see hooks.init_telemetry), never on the app's main thread
# or a telemetry flush, so a slow probe delays nothing the user waits on.
_PROBE_TIMEOUT = 5  # seconds

# ffmpeg hardware-acceleration methods worth reporting (the ``-hwaccels`` list
# also contains software/no-op entries on some builds; keep it to the ones that
# actually mean "this box can hardware-transcode, and how").
_MEANINGFUL_HWACCELS = {
    "cuda", "nvdec", "nvenc", "vaapi", "qsv", "dxva2", "d3d11va", "d3d12va",
    "videotoolbox", "vulkan", "vdpau", "amf", "mediacodec", "opencl", "drm",
}

# Substrings that mark an ffmpeg encoder as hardware-backed.
_HW_ENCODER_MARKERS = ("nvenc", "qsv", "vaapi", "amf", "videotoolbox", "v4l2m2m", "mediacodec")

_cache = None


def _run(cmd):
    """Run *cmd* (a list) with a hard timeout, returning stripped stdout or
    ``None``. Never raises: a missing binary, non-zero exit, or timeout all
    map to ``None``."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
            text=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    out = (result.stdout or "").strip()
    return out or None


# ---------------------------------------------------------------------------
# Container / virtualisation
# ---------------------------------------------------------------------------

def _detect_container():
    """Return a short container-runtime label ("docker", "podman", "lxc",
    "kubernetes") or ``None`` for bare metal. Cheap file/env checks only."""
    try:
        # MediaForge's own official image sets this in the Dockerfile — the most
        # reliable signal for the case that actually matters here.
        if os.environ.get("MEDIAFORGE_DOCKER") == "1":
            return "docker"
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            return "kubernetes"
        # Podman drops this marker file; Docker drops /.dockerenv.
        if os.path.exists("/run/.containerenv"):
            return "podman"
        if os.path.exists("/.dockerenv"):
            return "docker"
        # Fall back to the control-group hierarchy of PID 1 / self.
        for cgroup_path in ("/proc/1/cgroup", "/proc/self/cgroup"):
            try:
                with open(cgroup_path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read().lower()
            except OSError:
                continue
            if "kubepods" in content:
                return "kubernetes"
            if "docker" in content:
                return "docker"
            if "libpod" in content or "podman" in content:
                return "podman"
            if "/lxc" in content or "lxc/" in content:
                return "lxc"
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# OS detail
# ---------------------------------------------------------------------------

def _detect_distro():
    """Linux distro name + version (e.g. "Debian GNU/Linux 12"), or ``None``
    on non-Linux / when /etc/os-release isn't readable."""
    try:
        # Python 3.10+ (project requires >=3.10). Raises OSError off-Linux.
        info = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        return None
    pretty = info.get("PRETTY_NAME")
    if pretty:
        return pretty
    name = info.get("NAME")
    version = info.get("VERSION") or info.get("VERSION_ID")
    if name and version:
        return f"{name} {version}"
    return name or None


def _detect_libc():
    """C library flavour + version, e.g. "glibc 2.36". Distinguishes a
    glibc image (Debian/Ubuntu) from a musl one (Alpine) — a common source of
    "works on my machine" binary-compatibility bugs. ``None`` off-Linux."""
    if platform.system() != "Linux":
        return None
    try:
        lib, version = platform.libc_ver()
    except Exception:
        lib, version = "", ""
    if lib:
        # platform.libc_ver() reports glibc as "glibc"; normalise the label.
        label = "glibc" if lib.lower() == "glibc" else lib
        return f"{label} {version}".strip()
    # libc_ver() comes up empty on musl — detect it by the loader's presence.
    try:
        if glob.glob("/lib/ld-musl-*") or glob.glob("/usr/lib/ld-musl-*"):
            return "musl"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def _detect_cpu_model():
    """A human-readable CPU model string, or ``None``. Per-platform, because
    ``platform.processor()`` is empty on most Linux builds."""
    system = platform.system()
    try:
        if system == "Linux":
            # Collect candidate keys, then prefer the descriptive one. Order in
            # /proc/cpuinfo matters: the numeric "model" line (e.g. "model : 85")
            # appears BEFORE "model name" on x86, so a naive first-match returns
            # "85". Prefer "model name" (x86 + most ARM), then "Hardware"/"Model"
            # (ARM boards, where "model name" may be absent), never the bare
            # numeric "model".
            candidates = {}
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if ":" not in line:
                            continue
                        key, value = line.split(":", 1)
                        key = key.strip().lower()
                        value = value.strip()
                        if not value:
                            continue
                        if key == "model name" and "model_name" not in candidates:
                            candidates["model_name"] = value
                        elif key == "hardware" and "hardware" not in candidates:
                            candidates["hardware"] = value
                        elif key == "model" and "model" not in candidates and not value.isdigit():
                            candidates["model"] = value
            except OSError:
                pass
            for key in ("model_name", "hardware", "model"):
                if candidates.get(key):
                    return candidates[key]
            return platform.processor() or None
        if system == "Darwin":
            return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if system == "Windows":
            return (platform.processor()
                    or os.environ.get("PROCESSOR_IDENTIFIER")
                    or None)
    except Exception:
        return None
    return platform.processor() or None


def _detect_cpu_cores():
    """Return (logical_total, available). *available* is affinity/cpuset-aware
    on Linux, so a CPU-limited container reports the cores it may actually use
    rather than the host's full count. Either may be ``None``."""
    logical = None
    available = None
    try:
        logical = os.cpu_count()
    except Exception:
        logical = None
    try:
        if hasattr(os, "sched_getaffinity"):
            available = len(os.sched_getaffinity(0))
        else:
            available = logical
    except Exception:
        available = logical
    return logical, available


# ---------------------------------------------------------------------------
# GPU / ffmpeg hardware acceleration
# ---------------------------------------------------------------------------

def _resolve_ffmpeg():
    """Best-effort ffmpeg path *without* triggering an install. Mirrors the
    resolution order of autodeps.fetch_binary (system PATH, then MediaForge's
    dependency folder, then the Windows bundle) but never downloads or
    package-installs anything — telemetry must have no such side effect."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        raw = os.getenv("MEDIAFORGE_INSTALL_FOLDER", "")
        base = Path(raw).expanduser() if raw else (Path.home() / ".mediaforge")
        for name in ("ffmpeg", "ffmpeg.exe"):
            candidate = base / name
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    try:
        bundled = Path(__file__).resolve().parent.parent / "bin" / "windows" / "ffmpeg.exe"
        if bundled.exists():
            return str(bundled)
    except Exception:
        pass
    return None


def _detect_ffmpeg_capabilities(ffmpeg):
    """Return (hwaccels, hw_encoders) from an ffmpeg binary — the single most
    useful pair of fields for transcoding/upscaling error analysis. Empty
    lists when ffmpeg isn't resolvable or the probe fails.

    Note these describe what this ffmpeg *build* was compiled with, not a live
    probe of the hardware: a full ffmpeg lists nvenc/qsv/vaapi encoders even on
    a box with no matching GPU. Cross-reference the ``gpu`` field (nvidia-smi /
    /dev/dri), which does reflect actually-present hardware, when it matters."""
    hwaccels = []
    hw_encoders = []
    if not ffmpeg:
        return hwaccels, hw_encoders

    accel_out = _run([ffmpeg, "-hide_banner", "-hwaccels"])
    if accel_out:
        for line in accel_out.splitlines():
            token = line.strip().lower()
            if token in _MEANINGFUL_HWACCELS:
                hwaccels.append(token)

    enc_out = _run([ffmpeg, "-hide_banner", "-encoders"])
    if enc_out:
        for line in enc_out.splitlines():
            parts = line.split()
            # Encoder lines look like " V....D h264_nvenc   NVIDIA NVENC ...".
            if len(parts) >= 2 and set(parts[0]) <= set("VASFXBD.") and parts[0] != "":
                name = parts[1]
                if any(marker in name for marker in _HW_ENCODER_MARKERS):
                    hw_encoders.append(name)

    # De-duplicate while preserving order.
    hwaccels = list(dict.fromkeys(hwaccels))
    hw_encoders = list(dict.fromkeys(hw_encoders))
    return hwaccels, hw_encoders


def _detect_gpus():
    """A list of detected GPU names (may be empty). NVIDIA via nvidia-smi;
    on Linux, note a passed-through DRM render node when no discrete card was
    named; on Windows a best-effort WMIC query. Never installs anything and
    never raises."""
    gpus = []

    smi = shutil.which("nvidia-smi")
    if smi:
        out = _run([smi, "--query-gpu=name", "--format=csv,noheader"])
        if out:
            for line in out.splitlines():
                name = line.strip()
                if name:
                    gpus.append(name)

    system = platform.system()
    try:
        if system == "Linux":
            # A /dev/dri render node means a GPU (iGPU or passthrough) is usable
            # for VAAPI even when we can't put a marketing name to it.
            if not gpus and glob.glob("/dev/dri/renderD*"):
                gpus.append("DRM render device (/dev/dri)")
        elif system == "Windows" and not gpus:
            wmic = shutil.which("wmic")
            if wmic:
                out = _run([wmic, "path", "win32_VideoController", "get", "name"])
                if out:
                    for line in out.splitlines()[1:]:  # skip the "Name" header
                        name = line.strip()
                        if name:
                            gpus.append(name)
    except Exception:
        pass

    return list(dict.fromkeys(gpus))


def _detect_gpu_driver():
    """NVIDIA driver version via nvidia-smi, or ``None``. (AMD/Intel have no
    equally-portable one-liner; left to the ``gpu`` field there.)"""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    out = _run([smi, "--query-gpu=driver_version", "--format=csv,noheader"])
    if out:
        first = out.splitlines()[0].strip()
        return first or None
    return None


_TESTABLE_HWACCELS = {"vaapi", "cuda", "qsv", "vdpau", "drm", "videotoolbox", "d3d11va", "dxva2"}


def _probe_hwaccel_working(ffmpeg, candidates):
    """The *real* hardware-acceleration test the build-level ``hwaccels`` list
    can't give: for each candidate method, actually ask ffmpeg to create the
    hardware device and run a trivial one-frame null pipeline. Returncode 0
    means the device genuinely initialised on this machine (driver present,
    /dev/dri accessible, ...); a compiled-in-but-unusable method fails here.

    Only methods already in ``candidates`` (the build's hwaccels) and in the
    testable set are probed, so we never spawn ffmpeg for something it can't
    do anyway. Each probe is bounded by the module timeout and can only ever
    add a working method or nothing."""
    working = []
    if not ffmpeg:
        return working
    for method in candidates:
        if method not in _TESTABLE_HWACCELS:
            continue
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-init_hw_device", method,
                 "-f", "lavfi", "-i", "nullsrc=s=64x64", "-frames:v", "1", "-f", "null", "-"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PROBE_TIMEOUT,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            working.append(method)
    return working


def _detect_ffmpeg_version(ffmpeg):
    """Version token from ``ffmpeg -version`` (e.g. "6.1.1-3ubuntu5"), or
    ``None``. The exact build matters a lot for transcoding bug repro."""
    if not ffmpeg:
        return None
    out = _run([ffmpeg, "-version"])
    if not out:
        return None
    first = out.splitlines()[0]
    # "ffmpeg version 6.1.1-3ubuntu5 Copyright ..." -> "6.1.1-3ubuntu5"
    parts = first.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        return parts[2][:48]
    return first[:48] or None


def _pkg_version(dist_name):
    """Installed version of a Python distribution via importlib.metadata, or
    ``None`` if it isn't installed / has no metadata (a frozen build may lack
    dist metadata). Cheap, no import of the package itself."""
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        return None


def _detect_mpv_version():
    """mpv version string, best-effort. mpv is a binary (not a pip dist), so
    this is a resolve-then-probe; often ``None`` on setups without mpv."""
    mpv = shutil.which("mpv")
    if not mpv:
        try:
            bundled = Path(__file__).resolve().parent.parent / "bin" / "windows" / "mpv.exe"
            if bundled.exists():
                mpv = str(bundled)
        except Exception:
            mpv = None
    if not mpv:
        return None
    out = _run([mpv, "--version"])
    if not out:
        return None
    first = out.splitlines()[0].strip()
    # "mpv 0.38.0 Copyright ..." -> "0.38.0"
    parts = first.split()
    if len(parts) >= 2 and parts[0].lower() == "mpv":
        return parts[1][:32]
    return first[:32] or None


# ---------------------------------------------------------------------------
# Install method / privileges / network / OS environment
# ---------------------------------------------------------------------------

def _detect_install_method():
    """How MediaForge itself was installed — useful for self-update and
    dependency issues: "docker" | "pyinstaller" | "pipx" | "pip" | "source"
    | None. Best-effort, from the running module's location."""
    try:
        import sys
        if getattr(sys, "frozen", False):
            return "pyinstaller"
        if os.environ.get("MEDIAFORGE_DOCKER") == "1":
            return "docker"
        here = str(Path(__file__).resolve()).replace("\\", "/").lower()
        if "/pipx/" in here or "/pipx/venvs/" in here:
            return "pipx"
        if "site-packages" in here or "dist-packages" in here:
            return "pip"
        return "source"
    except Exception:
        return None


def _detect_is_admin():
    """Whether the process runs with elevated privileges (root / Administrator),
    or ``None`` if it can't be determined. Explains permission-denied errors."""
    try:
        if hasattr(os, "geteuid"):
            return os.geteuid() == 0
        # Windows
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _detect_readonly_rootfs():
    """Whether the root filesystem is mounted read-only (a common container
    hardening: docker-compose ``read_only: true``), or ``None`` off-POSIX.
    A read-only rootfs is behind a whole class of "can't write" failures."""
    try:
        if not hasattr(os, "statvfs") or not hasattr(os, "ST_RDONLY"):
            return None
        return bool(os.statvfs("/").f_flag & os.ST_RDONLY)
    except Exception:
        return None


def _detect_vpn():
    """Whether a WireGuard/OpenVPN interface is present in this network
    namespace (wg*/tun*), mirroring entrypoint.sh's Gluetun detection. ``None``
    off-Linux. Explains "WebUI/source unreachable" behind a VPN sidecar."""
    try:
        if platform.system() != "Linux":
            return None
        for pattern in ("/sys/class/net/wg*", "/sys/class/net/tun*"):
            if glob.glob(pattern):
                return True
        return False
    except Exception:
        return None


def _detect_timezone():
    """Return (tz_name, utc_offset) e.g. ("Europe/Berlin", "+02:00"). Either
    may be ``None``. Helps line telemetry timestamps up with a user's local
    time when reading a report."""
    tz_name = None
    offset = None
    try:
        tz_name = os.environ.get("TZ") or None
        if not tz_name:
            etc_tz = Path("/etc/timezone")
            if etc_tz.exists():
                tz_name = etc_tz.read_text(encoding="utf-8", errors="ignore").strip() or None
        if not tz_name:
            names = getattr(time, "tzname", None)
            if names:
                tz_name = names[0] or None
    except Exception:
        tz_name = None
    try:
        # localtime offset in seconds east of UTC, DST-aware.
        if time.localtime().tm_isdst and time.daylight:
            off_sec = -time.altzone
        else:
            off_sec = -time.timezone
        sign = "+" if off_sec >= 0 else "-"
        off_sec = abs(off_sec)
        offset = f"{sign}{off_sec // 3600:02d}:{(off_sec % 3600) // 60:02d}"
    except Exception:
        offset = None
    return tz_name, offset


# ---------------------------------------------------------------------------
# Memory (used both statically and in the runtime snapshot)
# ---------------------------------------------------------------------------

def _read_meminfo():
    """Parse /proc/meminfo into a {key: kB int} dict (Linux only). {} on
    failure / off-Linux."""
    info = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                value = parts[1].strip().split()
                if value and value[0].isdigit():
                    info[key] = int(value[0])  # kB
    except OSError:
        pass
    return info


def _win_memstatus():
    """Return (total_bytes, avail_bytes) via GlobalMemoryStatusEx on Windows,
    or (None, None). Keeps RAM detection dependency-free on Windows too."""
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys, stat.ullAvailPhys
    except Exception:
        pass
    return None, None


def _detect_ram_total_mb():
    """Total physical RAM in MB, or ``None``. A small-RAM NAS is behind many
    OOM-killed transcode/upscale failures."""
    system = platform.system()
    try:
        if system == "Linux":
            info = _read_meminfo()
            if "MemTotal" in info:
                return info["MemTotal"] // 1024
        elif system == "Windows":
            total, _ = _win_memstatus()
            if total:
                return total // (1024 * 1024)
        elif system == "Darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            if out and out.isdigit():
                return int(out) // (1024 * 1024)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect(force=False):
    """Return the extended system-info dict, computing it once and caching it.

    Keys (any of which may be ``None``/empty on a platform where the probe
    doesn't apply):

        container        "docker" | "podman" | "lxc" | "kubernetes" | None
        distro           e.g. "Debian GNU/Linux 12"
        libc             e.g. "glibc 2.36" | "musl"
        kernel           platform.release() — the (host, under Docker) kernel
        python_impl      "CPython" | "PyPy" | ...
        frozen           True if running from a PyInstaller/frozen bundle
        install_method   "docker" | "pyinstaller" | "pipx" | "pip" | "source"
        is_admin         running as root / Administrator?
        readonly_rootfs  is / mounted read-only? (container hardening)
        vpn              a wg*/tun* interface present in this netns?
        tz               timezone name, e.g. "Europe/Berlin"
        utc_offset       e.g. "+02:00"
        cpu_model        e.g. "Intel(R) Celeron(R) J4125"
        cpu_cores        logical core count (host)
        cpu_cores_avail  cores actually usable (cpuset/affinity-aware)
        ram_total_mb     total physical RAM in MB
        gpu              list[str] of GPU names (may be [])
        gpu_driver       NVIDIA driver version (if any)
        hwaccels         list[str] of ffmpeg hardware-accel methods (build-level)
        hw_encoders      list[str] of ffmpeg hardware encoders (build-level)
        hwaccel_working  list[str] of hwaccel methods that ACTUALLY initialise
        ffmpeg_version   e.g. "6.1.1"
        ytdlp_version    installed yt-dlp version
        mpv_version      installed mpv version (if resolvable)
        patchright_version  installed patchright (captcha browser) version

    Safe to call repeatedly; only the first call does any real work.
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    data = {}
    try:
        data["container"] = _detect_container()
        data["distro"] = _detect_distro()
        data["libc"] = _detect_libc()
        try:
            data["kernel"] = platform.release() or None
        except Exception:
            data["kernel"] = None
        try:
            data["python_impl"] = platform.python_implementation()
        except Exception:
            data["python_impl"] = None
        # A frozen (PyInstaller) build behaves differently enough — bundled
        # binaries, no site-packages — that it's worth distinguishing in a report.
        import sys
        data["frozen"] = bool(getattr(sys, "frozen", False))

        data["install_method"] = _detect_install_method()
        data["is_admin"] = _detect_is_admin()
        data["readonly_rootfs"] = _detect_readonly_rootfs()
        data["vpn"] = _detect_vpn()
        tz_name, utc_offset = _detect_timezone()
        data["tz"] = tz_name
        data["utc_offset"] = utc_offset

        data["cpu_model"] = _detect_cpu_model()
        logical, available = _detect_cpu_cores()
        data["cpu_cores"] = logical
        data["cpu_cores_avail"] = available
        data["ram_total_mb"] = _detect_ram_total_mb()

        data["gpu"] = _detect_gpus()
        data["gpu_driver"] = _detect_gpu_driver()

        ffmpeg = _resolve_ffmpeg()
        hwaccels, hw_encoders = _detect_ffmpeg_capabilities(ffmpeg)
        data["hwaccels"] = hwaccels
        data["hw_encoders"] = hw_encoders
        data["hwaccel_working"] = _probe_hwaccel_working(ffmpeg, hwaccels)
        data["ffmpeg_version"] = _detect_ffmpeg_version(ffmpeg)

        # Component versions -- yt-dlp/patchright are Python dists (importlib),
        # mpv is a binary (resolve-then-probe).
        data["ytdlp_version"] = _pkg_version("yt-dlp")
        data["patchright_version"] = _pkg_version("patchright")
        data["mpv_version"] = _detect_mpv_version()
    except Exception:
        # Belt and braces: whatever we managed to fill in is still worth sending;
        # a total failure just yields a mostly-empty dict, never an exception.
        pass

    _cache = data
    return data


# ---------------------------------------------------------------------------
# Runtime snapshot -- a *fresh* (never cached) capture of volatile machine
# state, meant to ride along with an ERROR event (crash/transcoding/download)
# so a report can answer "what state was the box in when it broke": was RAM
# exhausted (OOM), the disk full, the load pegged? Kept deliberately tiny and
# stdlib-only. Every field is best-effort and may be absent.
# ---------------------------------------------------------------------------

def runtime_snapshot(download_path=None):
    """Return a small dict of volatile state at call time. NOT cached -- each
    call re-reads live values, since the whole point is the state *now*.

    Keys (any may be absent):
        ram_available_mb   free RAM in MB
        ram_percent        RAM used, percent (0-100)
        disk_free_mb       free space on the download volume, MB
        load1              1-minute load average (POSIX)
        threads            live Python thread count
        fds                open file descriptors (Linux)
    """
    snap = {}
    system = platform.system()

    # ---- Memory ----
    try:
        if system == "Linux":
            info = _read_meminfo()
            total = info.get("MemTotal")
            avail = info.get("MemAvailable")
            if avail is not None:
                snap["ram_available_mb"] = avail // 1024
            if total and avail is not None and total > 0:
                snap["ram_percent"] = round((total - avail) * 100.0 / total, 1)
        elif system == "Windows":
            total, avail = _win_memstatus()
            if avail:
                snap["ram_available_mb"] = avail // (1024 * 1024)
            if total and avail:
                snap["ram_percent"] = round((total - avail) * 100.0 / total, 1)
    except Exception:
        pass

    # ---- Disk (download volume) ----
    try:
        target = download_path or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH") or str(Path.home())
        usage = shutil.disk_usage(target)
        snap["disk_free_mb"] = usage.free // (1024 * 1024)
    except Exception:
        pass

    # ---- Load ----
    try:
        if hasattr(os, "getloadavg"):
            snap["load1"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass

    # ---- Threads / file descriptors ----
    try:
        import threading
        snap["threads"] = threading.active_count()
    except Exception:
        pass
    try:
        if system == "Linux":
            snap["fds"] = len(os.listdir("/proc/self/fd"))
    except Exception:
        pass

    return snap
