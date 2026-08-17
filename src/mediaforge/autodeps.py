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
import threading
from pathlib import Path
from typing import Callable, List, Optional

PLATFORM = platform.system()

try:
    from .common import extract_archive, fetch_github_asset_urls
    from .config import GLOBAL_SESSION
    from .logger import get_logger

except ImportError:
    from mediaforge.common import extract_archive, fetch_github_asset_urls
    from mediaforge.config import GLOBAL_SESSION
    from mediaforge.logger import get_logger


# Connect/read timeout for every binary download below. Without it a hung
# mirror parks a queue worker forever -- the thread is inside a plain
# session.get(), so neither the job watchdog nor a cancel event can reach it.
_DOWNLOAD_TIMEOUT = (15, 120)


# -----------------------------
# Syncplay
# -----------------------------
def get_syncplay_release_url() -> List[str]:
    """Fetch the URLs for the latest Windows Syncplay portable ZIP release."""
    repo = "Syncplay/syncplay"
    portable_pattern = r"Syncplay[_-]\d+(?:\.\d+)*_Portable\.zip$"
    return fetch_github_asset_urls(repo, portable_pattern)


def get_syncplay_windows_url() -> Optional[str]:
    """Get Windows Syncplay URL (first match)."""
    urls = get_syncplay_release_url()
    return urls[0] if urls else None


# -----------------------------
# ffmpeg
# -----------------------------
def get_ffmpeg_windows_url() -> Optional[str]:
    """Resolve the download URL of a portable Windows ffmpeg build.

    BtbN/FFmpeg-Builds publishes static win64 builds under a rolling
    ``latest`` tag. The GPL archive carries both ffmpeg.exe and ffprobe.exe,
    and MediaForge needs both: ffmpeg-python shells out to them by bare name
    (``ffmpeg.probe()`` -> ffprobe), so a build with only one of the two is
    not enough.
    """
    urls = fetch_github_asset_urls(
        "BtbN/FFmpeg-Builds", r"ffmpeg-master-latest-win64-gpl\.zip$"
    )
    return urls[0] if urls else None


# -----------------------------
# Dependencies
# -----------------------------
# Per-platform entry keys:
#   package        -- id for the native package manager (winget/brew/apt/pacman)
#   url            -- static direct download URL
#   url_resolver   -- callable returning a URL, for assets whose file name
#                     carries a version and therefore cannot be hardcoded.
#                     Only consulted when ``url`` is absent, and only right
#                     before the download -- it costs a GitHub API call.
#   binary         -- executable file name expected in the install folder
#                     (defaults to the dependency name, + ".exe" on Windows)
#   extra_binaries -- further executables to keep out of a downloaded archive
deps = {
    "syncplay": {
        # url_resolver instead of the former ``"url": None``: the portable ZIP
        # is named after its version, so there is no static URL. The resolver
        # existed already but was never referenced, which left the download
        # fallback below with url=None.
        "Windows": {
            "package": "Syncplay.Syncplay",
            "url_resolver": get_syncplay_windows_url,
            "binary": "Syncplay.exe",
            # The portable build is a folder, not a single file -- see
            # DependencyManager._install_from_archive.
            "archive_dir": "syncplay",
        },
        "Linux": {"package": "syncplay"},
        "Darwin": {"package": "syncplay"},
    },
    "iina": {"Darwin": {"package": "iina"}},
    "7z": {"Windows": {"url": "https://7-zip.org/a/7zr.exe", "binary": "7zr.exe"}},
    "ffmpeg": {
        "Windows": {
            "package": "Gyan.FFmpeg",
            "url_resolver": get_ffmpeg_windows_url,
            "binary": "ffmpeg.exe",
            "extra_binaries": ["ffprobe.exe"],
        },
        "Linux": {"package": "ffmpeg"},
        "Darwin": {"package": "ffmpeg"},
    },
    "mpv": {
        "Linux": {"package": "mpv"},
        "Darwin": {"package": "mpv"},
    },
}

# One lock and one result cache per dependency name, shared by every
# DependencyManager instance. The queue runs several download threads and each
# of them called fetch_binary("ffmpeg") independently: without this, N threads
# start N winget installs and N downloads into the same target file, and every
# episode of a job paid the full resolution cost again.
_fetch_locks: dict = {}
_fetch_locks_guard = threading.Lock()
_fetch_cache: dict = {}


def _lock_for(name: str) -> threading.Lock:
    """Return the process-wide lock guarding resolution of dependency *name*."""
    with _fetch_locks_guard:
        return _fetch_locks.setdefault(name, threading.Lock())


def _refresh_windows_path() -> None:
    """Re-read the persisted PATH from the registry into ``os.environ``.

    winget writes its shim directory (and Gyan.FFmpeg's own bin folder) into
    the *stored* environment, which only reaches processes started afterwards.
    A long-running MediaForge therefore still failed ``shutil.which("ffmpeg")``
    right after a successful install and fell through to the download branch.
    Best effort: any failure leaves the current PATH untouched.
    """
    if PLATFORM != "Windows":
        return
    try:
        import winreg
    except ImportError:
        return

    parts: List[str] = []
    for root, key_path in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if value:
            parts.extend(os.path.expandvars(value).split(os.pathsep))

    if not parts:
        return

    current = os.environ.get("PATH", "").split(os.pathsep)
    merged = list(current)
    seen = {p.lower() for p in current if p}
    for part in parts:
        if part and part.lower() not in seen:
            merged.append(part)
            seen.add(part.lower())
    os.environ["PATH"] = os.pathsep.join(p for p in merged if p)


def _prepend_to_path(folder: Path) -> None:
    """Put *folder* at the front of ``os.environ["PATH"]`` (idempotent).

    Downloading ffmpeg into ~/.mediaforge used to be pointless on its own:
    every call site invokes it as the bare name ``ffmpeg`` (ffmpeg-python
    builds the argv, anime4k runs ``["ffmpeg", "-filters"]``), so a binary
    that is not on PATH is a binary nothing can find.
    """
    entry = str(folder)
    current = os.environ.get("PATH", "")
    if entry.lower() in {p.lower() for p in current.split(os.pathsep) if p}:
        return
    os.environ["PATH"] = entry + os.pathsep + current if current else entry


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

    def _binary_name(self, name: str, dep_info: dict) -> str:
        """File name the resolved executable is expected to carry on disk."""
        configured = dep_info.get("binary")
        if configured:
            return configured
        return f"{name}.exe" if PLATFORM == "Windows" else name

    def _resolve_url(self, dep_info: dict) -> Optional[str]:
        """Return the download URL for *dep_info*, or None if there is none.

        A ``url_resolver`` hits the network (GitHub releases API), so it runs
        only here -- at the point the download is actually about to happen --
        and never at import time.
        """
        url = dep_info.get("url")
        if url:
            return url
        resolver: Optional[Callable[[], Optional[str]]] = dep_info.get("url_resolver")
        if not resolver:
            return None
        try:
            return resolver()
        except Exception as exc:
            self.logger.warning("Could not resolve a download URL: %s", exc)
            return None

    def fetch_binary(self, name: str) -> Path:
        """Return a usable path to binary *name*, installing it if necessary.

        Resolution order: system PATH, cached download in the install
        folder, OS package manager (winget/brew/apt/pacman), then a direct
        download from the URL configured in ``deps``.

        Raises ``RuntimeError`` when none of those produce a binary. It used
        to fall into the download branch with ``url=None`` instead, which
        surfaced as ``AssertionError: Missing URL in PreparedRequest`` from
        deep inside niquests -- a message that names neither the dependency
        nor anything the user can act on, and that web.error_explain could
        only classify as ``unknown``.
        """
        cached = _fetch_cache.get(name)
        if cached is not None and cached.exists():
            return cached

        with _lock_for(name):
            # Re-check: another thread may have resolved it while we waited.
            cached = _fetch_cache.get(name)
            if cached is not None and cached.exists():
                return cached
            resolved = self._fetch_binary_locked(name)
            _fetch_cache[name] = resolved
            return resolved

    def _local_candidate(self, dep_info: dict, binary_name: str) -> Path:
        """Where an already-provisioned copy of the binary would sit.

        ``archive_dir`` dependencies keep their whole extracted tree in a
        subfolder, everything else lands flat in the install folder.
        """
        archive_dir = dep_info.get("archive_dir")
        if archive_dir:
            return self.install_folder / archive_dir / binary_name
        return self.install_folder / binary_name

    def _fetch_binary_locked(self, name: str) -> Path:
        """Body of :meth:`fetch_binary`; runs under the per-dependency lock."""
        dep_info = self.deps.get(name, {}).get(PLATFORM, {})
        binary_name = self._binary_name(name, dep_info)

        # System-wide first
        sys_path = shutil.which(name)
        if sys_path:
            self.logger.debug(f"{name} found system-wide at {sys_path}")
            return Path(sys_path)

        # Local folder. Keyed on the expected binary name rather than on the
        # URL's file name: with an archive download those differ, and the old
        # form additionally needed the URL just to look in a local directory.
        local_path = self._local_candidate(dep_info, binary_name)
        if local_path.exists():
            self.logger.debug(f"{name} found in {local_path.parent}")
            _prepend_to_path(local_path.parent)
            return local_path

        # Package manager
        if self._install_with_package_manager(name):
            # The install updated the stored PATH, not ours -- pull it in
            # before deciding the install did not work.
            _refresh_windows_path()
            if local_path.exists():
                return local_path
            sys_path_after = shutil.which(name)
            if sys_path_after:
                return Path(sys_path_after)
            self.logger.debug(
                f"{name} reported as installed but not on PATH -- "
                f"falling back to the direct download"
            )

        # Download fallback
        url = self._resolve_url(dep_info)
        if not url:
            raise RuntimeError(
                f"{name} could not be provisioned automatically on {PLATFORM}: "
                f"it is not on PATH, the package manager did not supply it and "
                f"no download source is configured. Please install {name} "
                f"manually and make sure it is reachable via PATH."
            )

        self.logger.debug(f"Downloading {name} for {PLATFORM} from {url}...")
        downloaded = self._download(url, Path(url.split("?", 1)[0]).name)

        if downloaded.suffix.lower() in (".zip", ".7z"):
            local_path = self._install_from_archive(
                downloaded, dep_info, binary_name
            ) or local_path
        elif downloaded != local_path:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(downloaded, local_path)

        if not local_path.exists():
            raise RuntimeError(
                f"{name} was downloaded from {url} but no {binary_name} ended "
                f"up in {self.install_folder}. Please install {name} manually."
            )

        if PLATFORM != "Windows":
            local_path.chmod(0o755)

        _prepend_to_path(local_path.parent)
        self.logger.debug(f"{name} downloaded to {local_path}")
        return local_path

    def _download(self, url: str, file_name: str) -> Path:
        """Stream *url* into the install folder and return the finished path.

        Written to a ``.part`` sibling and moved into place only after the
        last chunk. The previous version streamed straight onto the target:
        an aborted download (network drop, process kill) left a truncated
        file there, and every later run took that stub for a valid binary --
        the same failure mode the episode copy path in models/common was
        fixed for.
        """
        target = self.install_folder / file_name
        part = target.with_name(target.name + ".part")
        try:
            resp = GLOBAL_SESSION.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
            os.replace(part, target)
        except BaseException:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target

    def _install_from_archive(self, archive: Path, dep_info: dict,
                              binary_name: str) -> Optional[Path]:
        """Extract *archive* and put the wanted executables in place.

        Two layouts, because the two archives MediaForge pulls need different
        things:

        * default (ffmpeg) -- lift only the named executables into the install
          folder, flat. Flat on purpose: telemetry.sysinfo._resolve_ffmpeg and
          the local-folder branch above both look for
          ``<install folder>/<binary>`` directly, and the release archive
          buries them at ``ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe``.
          Safe here because BtbN's builds are statically linked.
        * ``archive_dir`` (Syncplay) -- keep the whole tree under
          ``<install folder>/<archive_dir>/``. The portable ZIP ships Qt DLLs
          and a resources folder next to Syncplay.exe; lifting out the .exe on
          its own produces a binary that cannot start.

        Returns the resolved binary path, or None when it was not in there.
        """
        staging = self.install_folder / f".extract-{archive.stem}"
        archive_dir = dep_info.get("archive_dir")
        try:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            extract_archive(archive, staging)

            if archive_dir:
                match = next(
                    (p for p in staging.rglob(binary_name) if p.is_file()), None
                )
                if match is None:
                    self.logger.warning(
                        "%s not found inside %s", binary_name, archive.name
                    )
                    return None
                # Some archives wrap everything in a single top-level folder,
                # some do not -- root the tree at whatever actually holds the
                # binary so the sibling DLLs come along either way.
                target = self.install_folder / archive_dir
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(match.parent), str(target))
                resolved = target / binary_name
                if PLATFORM != "Windows" and resolved.exists():
                    resolved.chmod(0o755)
                return resolved

            resolved = None
            for want in [binary_name, *dep_info.get("extra_binaries", [])]:
                match = next(
                    (p for p in staging.rglob(want) if p.is_file()), None
                )
                if match is None:
                    self.logger.warning(
                        "%s not found inside %s", want, archive.name
                    )
                    continue
                dest = self.install_folder / want
                os.replace(match, dest)
                if PLATFORM != "Windows":
                    dest.chmod(0o755)
                if want == binary_name:
                    resolved = dest
            return resolved
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                pass

    def _install_with_package_manager(self, name: str) -> bool:
        """Try to install *name* via the platform's native package manager.

        Windows: winget. macOS: brew. Linux: apt (Debian/Ubuntu) or pacman
        (Arch), whichever is present. Returns False (never raises) if no
        package is configured for this platform or the install fails, so
        the caller can fall through to the direct-download path.

        Every call is non-interactive and time-boxed. MediaForge normally runs
        as a background service with no console attached: a package manager
        that stops to ask for a licence agreement or a [Y/n] gets no answer,
        and the queue worker that triggered it blocks for as long as the
        process lives.
        """
        dep_info = self.deps.get(name, {}).get(PLATFORM, {})
        pkg_name = dep_info.get("package")
        if not pkg_name:
            return False

        # 15 minutes: enough for a slow ffmpeg/Syncplay install on a thin
        # connection, short enough that a stuck prompt eventually gives up.
        timeout = 900

        try:
            if PLATFORM == "Windows":
                subprocess.run(
                    [
                        "winget", "install", "-e", "--id", pkg_name, "-h",
                        # Without these, winget waits on a prompt that nothing
                        # can answer when there is no attached console.
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--disable-interactivity",
                    ],
                    check=True, timeout=timeout,
                )
            elif PLATFORM == "Darwin":
                subprocess.run(["brew", "install", pkg_name],
                               check=True, timeout=timeout)
            else:
                if shutil.which("apt"):
                    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
                    subprocess.run(["sudo", "-n", "apt", "update"],
                                   check=True, timeout=timeout, env=env)
                    subprocess.run(
                        ["sudo", "-n", "apt", "install", "-y", pkg_name],
                        check=True, timeout=timeout, env=env,
                    )
                elif shutil.which("pacman"):
                    subprocess.run(
                        ["sudo", "-n", "pacman", "-Sy", "--noconfirm", pkg_name],
                        check=True, timeout=timeout,
                    )
                else:
                    return False

            self.logger.debug(f"{name} installed via package manager on {PLATFORM}")
            return True

        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired, OSError) as e:
            self.logger.debug(f"Package manager failed for {name} on {PLATFORM}: {e}")
            return False


def ensure_ffmpeg() -> Optional[Path]:
    """Make sure ffmpeg/ffprobe are callable by bare name, and return the path.

    Wraps :meth:`DependencyManager.fetch_binary` because the download pipeline
    calls this once per episode: constructing a DependencyManager per call
    re-ran ``mkdir`` and, before the cache in fetch_binary, a full winget
    attempt for every single episode of a job.

    Returns None on platforms MediaForge does not auto-provision ffmpeg for
    (Linux/macOS ship it through the image or Homebrew); raises RuntimeError
    with an actionable message when provisioning was attempted and failed.
    """
    if PLATFORM != "Windows":
        return None
    return DependencyManager().fetch_binary("ffmpeg")


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
    from .config import ssl_context_for

    dest = Path(__file__).parent / "bin" / "windows" / "mpv.exe"
    tmp  = dest.with_suffix(".download_tmp")
    try:
        logger.info("[mpv] mpv.exe nicht gefunden — starte Auto-Download von softarchiv.com …")
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Streamed by hand rather than via urlretrieve(): only urlopen() takes
        # an SSL context, and ssl_context_for() hands us the OS trust store so
        # the chain is validated against the same roots the browser uses. This
        # payload is an executable that get_player_path() later launches, so a
        # failed certificate check has to stay fatal.
        req = urllib.request.Request(
            _MPV_DOWNLOAD_URL, headers={"User-Agent": "MediaForge/1.0"}
        )
        _ctx = ssl_context_for(_MPV_DOWNLOAD_URL)
        try:
            _resp = urllib.request.urlopen(req, timeout=60, context=_ctx)
        except RecursionError:
            # truststore recursing inside the handshake -- see config.py's
            # _truststore_is_safe(). Retry once on certifi rather than leaving
            # the user without a player; verification stays on either way.
            if _ctx is None:
                raise
            logger.warning("[mpv] TLS-Aufbau über truststore rekursiv — "
                           "wiederhole mit dem Standard-Zertifikatsspeicher")
            _resp = urllib.request.urlopen(req, timeout=60, context=None)
        with _resp as resp, open(tmp, "wb") as fh:
            total_size = int(resp.headers.get("Content-Length") or 0)
            read_bytes = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                read_bytes += len(chunk)
                if total_size > 0:
                    _mpv_download_status["pct"] = min(
                        int(read_bytes * 100 / total_size), 99
                    )

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


def _ensure_xvfb() -> bool:
    """Start a background Xvfb on :99 if no DISPLAY is set (Linux only).

    In Docker the entrypoint already starts Xvfb and exports DISPLAY=:99, so
    this is a fast no-op.  On a bare Linux desktop/server with no display it
    spins up a virtual framebuffer so headless=False Chromium can run.

    Returns True when a display is available afterwards, False when this is a
    Linux host with no DISPLAY and no way to create one.  The return value
    matters: the captcha solvers launch Chromium with headless=False (required
    for Cloudflare/Turnstile), and without a display that launch does not fail
    with anything readable -- it dies as

        TargetClosedError: BrowserType.launch: Target page, context or browser
        has been closed

    with the entire Chromium command line attached.  Callers use the result to
    stop with an actionable message instead, and to keep a missing system
    package out of the crash channel: see playwright/captcha.py's
    _require_display().

    Used by: ``playwright/captcha.py`` before launching the visible captcha
    browser on Linux, and ``models/hanime_tv/browser.py``.
    """
    global _xvfb_proc
    if platform.system() != "Linux":
        return True  # Windows/macOS always have a usable display server
    if os.environ.get("DISPLAY"):
        return True  # already set — Docker / host X11
    with _xvfb_lock:
        if os.environ.get("DISPLAY"):
            return True
        if _xvfb_proc is not None and _xvfb_proc.poll() is None:
            os.environ.setdefault("DISPLAY", ":99")
            return True
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            logger.warning("Xvfb not found — captcha browser cannot run without a display")
            return False
        try:
            _xvfb_proc = subprocess.Popen(
                [xvfb, ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":99"
            import time as _t
            _t.sleep(0.5)
            # Popen succeeds even when Xvfb dies immediately (display :99 already
            # taken, missing font path, ...). Confirm it is actually alive before
            # promising the caller a display.
            if _xvfb_proc.poll() is not None:
                os.environ.pop("DISPLAY", None)
                logger.warning("Xvfb exited immediately — captcha browser has no display")
                return False
            logger.debug("Xvfb started on :99")
            return True
        except Exception as e:
            logger.warning(f"Failed to start Xvfb: {e}")
            return False


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
