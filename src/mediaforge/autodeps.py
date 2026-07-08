"""Auto-install and path-resolution helpers for external binaries.

Covers three things MediaForge needs but doesn't vendor as a Python
dependency: the mpv/iina video player, Syncplay, and a virtual display
(Xvfb) for the headless captcha browser on Linux. Each helper tries, in
order, a system-wide install, a previously downloaded copy in the user's
MediaForge folder, the OS package manager, and finally a direct download —
so the app works out of the box on a fresh machine without requiring the
user to install anything manually first.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

PLATFORM = platform.system()

try:
    from .common import fetch_github_asset_urls
    from .config import GLOBAL_SESSION
    from .logger import get_logger

except ImportError:
    from mediaforge.common import fetch_github_asset_urls
    from mediaforge.config import GLOBAL_SESSION
    from mediaforge.logger import get_logger


# -----------------------------
# Syncplay
# -----------------------------
def get_syncplay_release_url() -> List[str]:
    """Fetch the URLs for the latest Windows Syncplay portable ZIP release."""
    repo = "Syncplay/syncplay"
    portable_pattern = r"Syncplay[_-]\d+(?:\.\d+)*_Portable\.zip$"
    return fetch_github_asset_urls(repo, portable_pattern)


def get_syncplay_windows_url() -> str:
    """Get Windows Syncplay URL (first match)."""
    urls = get_syncplay_release_url()
    return urls[0] if urls else None


# -----------------------------
# Dependencies
# -----------------------------
deps = {
    "syncplay": {
        "Windows": {"package": "Syncplay.Syncplay", "url": None},
        "Linux": {"package": "syncplay"},
        "Darwin": {"package": "syncplay"},
    },
    "iina": {"Darwin": {"package": "iina"}},
    "7z": {"Windows": {"url": "https://7-zip.org/a/7zr.exe"}},
    "ffmpeg": {
        "Windows": {"package": "Gyan.FFmpeg", "url": None},
        "Linux": {"package": "ffmpeg"},
        "Darwin": {"package": "ffmpeg"},
    },
    "mpv": {
        "Linux": {"package": "mpv"},
        "Darwin": {"package": "mpv"},
    },
}


# -----------------------------
# Dependency Manager
# -----------------------------
class DependencyManager:
    """Resolve or install a named binary via system PATH, a cached local
    download, the OS package manager, or a direct download — in that order.

    Used by :func:`get_player_path` (mpv) and :func:`get_syncplay_path`
    (syncplay) as the fallback once a bundled/system binary isn't found.
    """

    def __init__(self, install_folder=None):
        self.deps = deps
        raw = install_folder or os.getenv("MEDIAFORGE_INSTALL_FOLDER", "")
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            self.install_folder = p
        else:
            self.install_folder = Path.home() / ".mediaforge"
        self.install_folder.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger(__name__)
        self.logger.debug(f"Dependency folder: {self.install_folder}")

    def fetch_binary(self, name: str) -> Path:
        """Return a usable path to binary *name*, installing it if necessary.

        Resolution order: system PATH, cached download in the install
        folder, OS package manager (winget/brew/apt/pacman), then a direct
        download from the URL configured in ``deps``.
        """
        dep_info = self.deps.get(name, {}).get(PLATFORM, {})

        # System-wide first
        sys_path = shutil.which(name)
        if sys_path:
            self.logger.debug(f"{name} found system-wide at {sys_path}")
            return Path(sys_path)

        url = dep_info.get("url")
        local_path = self.install_folder / Path(url).name if url else None

        # Local folder
        if local_path and local_path.exists():
            self.logger.debug(f"{name} found in {self.install_folder}")
            return local_path

        # Package manager
        if self._install_with_package_manager(name):
            if local_path and local_path.exists():
                return local_path
            sys_path_after = shutil.which(name)
            if sys_path_after:
                return Path(sys_path_after)

        # Download fallback
        self.logger.debug(f"Downloading {name} for {PLATFORM} from {url}...")
        resp = GLOBAL_SESSION.get(url, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        if PLATFORM != "Windows":
            local_path.chmod(0o755)

        self.logger.debug(f"{name} downloaded to {local_path}")
        return local_path

    def _install_with_package_manager(self, name: str) -> bool:
        """Try to install *name* via the platform's native package manager.

        Windows: winget. macOS: brew. Linux: apt (Debian/Ubuntu) or pacman
        (Arch), whichever is present. Returns False (never raises) if no
        package is configured for this platform or the install fails, so
        the caller can fall through to the direct-download path.
        """
        dep_info = self.deps.get(name, {}).get(PLATFORM, {})
        pkg_name = dep_info.get("package")
        if not pkg_name:
            return False

        try:
            if PLATFORM == "Windows":
                subprocess.run(
                    ["winget", "install", "-e", "--id", pkg_name, "-h"], check=True
                )
            elif PLATFORM == "Darwin":
                subprocess.run(["brew", "install", pkg_name], check=True)
            else:
                if shutil.which("apt"):
                    subprocess.run(["sudo", "apt", "update"], check=True)
                    subprocess.run(["sudo", "apt", "install", "-y", pkg_name], check=True)
                elif shutil.which("pacman"):
                    subprocess.run(["sudo", "pacman", "-Sy", pkg_name], check=True)
                else:
                    return False

            self.logger.debug(f"{name} installed via package manager on {PLATFORM}")
            return True

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.logger.debug(f"Package manager failed for {name} on {PLATFORM}: {e}")
            return False


# -----------------------------
# Player paths
# -----------------------------
_MPV_DOWNLOAD_URL = "https://softarchiv.com/download/mpv.exe"
_mpv_download_status: dict = {"state": "idle", "pct": 0, "error": ""}  # thread-safe enough for reads
_mpv_download_lock = __import__("threading").Lock()

logger = get_logger()


def _bundled_mpv() -> Path | None:
    """Return the path to the mpv binary bundled inside the package, if present.

    Only Windows is bundled — Linux uses system mpv (Docker apt),
    macOS uses system mpv (Homebrew).
    """
    if PLATFORM != "Windows":
        return None
    p = Path(__file__).parent / "bin" / "windows" / "mpv.exe"
    return p if p.exists() else None


def get_mpv_download_status() -> dict:
    """Return current mpv auto-download status dict.

    Used by: the upscale route (``web/routes/upscale.py``) to show download
    progress in the WebUI while ``_download_mpv_windows`` runs in the
    background.
    """
    return dict(_mpv_download_status)


def ensure_mpv_windows_async() -> None:
    """Start a background thread that downloads mpv.exe if missing on Windows.

    No-op on non-Windows platforms and when a bundled/cached mpv.exe already
    exists. Called once during WebUI startup (``web/app.py``) so the first
    playback request doesn't have to block on the download.
    """
    if PLATFORM != "Windows":
        return
    if _bundled_mpv():
        return  # already present
    import threading
    with _mpv_download_lock:
        if _mpv_download_status["state"] in ("downloading", "done"):
            return
        _mpv_download_status["state"] = "downloading"
        _mpv_download_status["pct"] = 0
        _mpv_download_status["error"] = ""
    t = threading.Thread(target=_download_mpv_windows, daemon=True, name="mpv-downloader")
    t.start()


def _download_mpv_windows() -> None:
    """Download mpv.exe from softarchiv.com into bin/windows/.

    Runs in a background thread started by :func:`ensure_mpv_windows_async`;
    reports progress via the module-level ``_mpv_download_status`` dict
    (read by :func:`get_mpv_download_status`). Downloads to a temp file first
    and renames on completion so a half-finished download is never mistaken
    for a valid binary.
    """
    import urllib.request
    dest = Path(__file__).parent / "bin" / "windows" / "mpv.exe"
    tmp  = dest.with_suffix(".download_tmp")
    try:
        logger.info("[mpv] mpv.exe nicht gefunden — starte Auto-Download von softarchiv.com …")
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _report(block_nr, block_size, total_size):
            if total_size > 0:
                pct = min(int(block_nr * block_size * 100 / total_size), 99)
                _mpv_download_status["pct"] = pct

        urllib.request.urlretrieve(_MPV_DOWNLOAD_URL, str(tmp), reporthook=_report)
        tmp.rename(dest)
        _mpv_download_status["state"] = "done"
        _mpv_download_status["pct"] = 100
        logger.info(f"[mpv] mpv.exe erfolgreich heruntergeladen: {dest}")
    except Exception as e:
        _mpv_download_status["state"] = "error"
        _mpv_download_status["error"] = str(e)
        logger.error(f"[mpv] Download fehlgeschlagen: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def get_player_path() -> Path:
    """Return the path to mpv (or iina on macOS).

    Priority:
      1. Bundled binary shipped with the package (src/mediaforge/bin/<platform>/mpv)
      2. System PATH (Docker, manual system install)

    Used by: ``models/common/common.py`` and ``anime4k/anime4k.py`` to build
    the mpv/iina launch command for watch/syncplay playback.
    """
    use_iina = os.getenv("MEDIAFORGE_USE_IINA") == "1"
    use_aniskip = os.getenv("MEDIAFORGE_ANISKIP") == "1"

    if PLATFORM == "Darwin" and use_iina and not use_aniskip:
        iina = shutil.which("iina")
        if iina:
            return Path(iina)
        bundle = Path("/Applications/IINA.app/Contents/MacOS/iina")
        if bundle.exists():
            return bundle
        raise RuntimeError(
            "iina nicht gefunden. Bitte installieren: brew install --cask iina"
        )

    # 1. Bundled binary (shipped with pip package)
    bundled = _bundled_mpv()
    if bundled:
        return bundled

    # 1b. Windows: if a download is in progress, wait up to 5 min
    if PLATFORM == "Windows":
        import time as _time
        st = _mpv_download_status.get("state", "idle")
        if st == "downloading":
            logger.info("[mpv] Warte auf laufenden mpv-Download …")
            for _ in range(300):  # max 5 min
                _time.sleep(1)
                if _mpv_download_status.get("state") != "downloading":
                    break
            bundled = _bundled_mpv()
            if bundled:
                return bundled

    # 2. System PATH (Docker / manual install)
    system = shutil.which("mpv")
    if system:
        return Path(system)

    # 3. Auto-install via package manager (Linux: apt/pacman, macOS: brew)
    if PLATFORM in ("Linux", "Darwin"):
        try:
            manager = DependencyManager()
            return manager.fetch_binary("mpv")
        except Exception as e:
            logger.debug(f"[mpv] Auto-install fehlgeschlagen: {e}")

    raise RuntimeError(
        "mpv nicht gefunden.\n"
        "Windows: mpv.exe wird beim Start automatisch heruntergeladen. "
        "Bitte kurz warten und es dann erneut versuchen.\n"
        "Linux: sudo apt install mpv  (oder pacman -S mpv)\n"
        "macOS: brew install mpv"
    )


def get_syncplay_path() -> Path:
    """Return the path to the Syncplay binary, installing it if necessary.

    Used by: ``models/common/common.py`` to launch synchronized playback
    sessions.
    """
    if PLATFORM == "Darwin":
        syncplay_path = Path("/Applications/Syncplay.app/Contents/MacOS/Syncplay")
        if syncplay_path.exists():
            return syncplay_path
    manager = DependencyManager()
    return manager.fetch_binary("syncplay")


# -----------------------------
# Ensure virtual display (Linux)
# -----------------------------
_xvfb_proc = None
_xvfb_lock = __import__("threading").Lock()


def _ensure_xvfb() -> None:
    """Start a background Xvfb on :99 if no DISPLAY is set (Linux only).

    In Docker the entrypoint already starts Xvfb and exports DISPLAY=:99, so
    this is a fast no-op.  On a bare Linux desktop/server with no display it
    spins up a virtual framebuffer so headless=False Chromium can run.

    Used by: ``playwright/captcha.py`` before launching the visible captcha
    browser on Linux.
    """
    global _xvfb_proc
    if platform.system() != "Linux":
        return
    if os.environ.get("DISPLAY"):
        return  # already set — Docker / host X11
    with _xvfb_lock:
        if os.environ.get("DISPLAY"):
            return
        if _xvfb_proc is not None and _xvfb_proc.poll() is None:
            os.environ.setdefault("DISPLAY", ":99")
            return
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            logger.warning("Xvfb not found — captcha browser may fail without a display")
            return
        try:
            _xvfb_proc = subprocess.Popen(
                [xvfb, ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":99"
            import time as _t
            _t.sleep(0.5)
            logger.debug("Xvfb started on :99")
        except Exception as e:
            logger.warning(f"Failed to start Xvfb: {e}")


# -----------------------------
# Ensure browser
# -----------------------------
def ensure_patchright_chromium():
    """Install the patchright Chromium browser if not already present.

    Skipped inside Docker (the image already bundles it). Best-effort: any
    failure is logged and swallowed so it never blocks startup.
    Used by: :func:`mediaforge.entry.mediaforge` during startup.
    """
    _log = get_logger(__name__)
    try:
        import patchright  # noqa: F401
    except ImportError:
        _log.debug("patchright not installed, skipping chromium check")
        return

    in_docker = os.path.exists("/.dockerenv") or os.environ.get("MEDIAFORGE_DOCKER") == "1"
    if in_docker:
        _log.debug("Running in Docker — skipping patchright chromium check (pre-installed in image)")
        return

    try:
        _log.debug("Ensuring patchright chromium is installed...")
        subprocess.run(
            [sys.executable, "-m", "patchright", "install", "chromium"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log.debug("patchright chromium is ready")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _log.debug(f"patchright chromium install failed (non-fatal): {e}")


if __name__ == "__main__":
    print(get_player_path())
    print(get_syncplay_path())
