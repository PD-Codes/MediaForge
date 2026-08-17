"""Generic shared helpers used across the MediaForge codebase.

Currently: fetching GitHub release metadata/asset URLs, and extracting
downloaded .zip/.7z archives. Not to be confused with
``mediaforge.models.common.common``, the unrelated download/encode pipeline
module.
"""

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

try:
    from ..config import GLOBAL_SESSION
except ImportError:
    from mediaforge.config import GLOBAL_SESSION


def get_latest_github_release(repo):
    """
    Fetch the latest release tag of a GitHub repository.

    Args:
        repo: GitHub repo in "owner/repo" format, e.g. "shinchiro/mpv-winbuild-cmake"

    Returns:
        The tag name of the latest release

    Used by: fetch_github_asset_urls() (below) and
    mediaforge.anime4k.anime4k.get_anime4k_urls().
    """
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    resp = GLOBAL_SESSION.get(api_url)
    resp.raise_for_status()
    release_data = resp.json()
    return release_data.get("tag_name")


def fetch_github_asset_urls(repo, asset_patterns, release="latest"):
    """
    Fetch all download URLs of GitHub release assets matching one or more regex patterns.

    Args:
        repo: GitHub repo in "owner/repo" format, e.g. "shinchiro/mpv-winbuild-cmake"
        asset_patterns: Regex pattern(s) to match asset file names
        release: Release tag or "latest" (default)

    Returns:
        List of URLs matching any of the patterns (empty list if none found)

    Used by: mediaforge.autodeps (fetching mpv/ffmpeg portable builds).
    """
    if isinstance(asset_patterns, str):
        asset_patterns = [asset_patterns]

    if release == "latest":
        release = get_latest_github_release(repo)

    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{release}"
    resp = GLOBAL_SESSION.get(api_url)
    resp.raise_for_status()
    assets = resp.json().get("assets", [])

    matched_urls = []

    for pattern_str in asset_patterns:
        pattern = re.compile(pattern_str, re.IGNORECASE)
        for asset in assets:
            url = asset.get("browser_download_url")
            if url and pattern.search(url):
                matched_urls.append(url)

    return matched_urls


def _safe_zip_members(archive: zipfile.ZipFile, target_dir: Path):
    """Yield members of *archive* that stay inside *target_dir*.

    Zip Slip guard. Every archive handled here comes off the network (GitHub
    releases, 7-zip.org), and a member named ``../../autoexec.bat`` or
    ``C:\\Windows\\System32\\x.dll`` would otherwise be written wherever it
    asks -- ``zipfile.extractall`` sanitises names, but the per-member
    extraction this function feeds does not, and neither does the system
    ``unzip``/``7z`` path below. Symlink members are dropped for the same
    reason: a link to ``/etc`` turns a later write into an escape.
    """
    resolved_root = target_dir.resolve()
    for info in archive.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        # 0xA000 == S_IFLNK in the high 16 bits of external_attr (Unix zips).
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise ValueError(f"Refusing symlink member in archive: {name}")
        if name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts:
            raise ValueError(f"Refusing unsafe path in archive: {name}")
        destination = (resolved_root / name).resolve()
        if destination != resolved_root and resolved_root not in destination.parents:
            raise ValueError(f"Refusing path traversal in archive: {name}")
        yield info, destination


def extract_archive(file_path, target_dir):
    """Extract a .zip or .7z archive into *target_dir*, on every platform.

    ZIPs go through the stdlib ``zipfile`` everywhere -- no external binary,
    identical behaviour on Windows, Linux and macOS. 7z still needs the
    system ``7z``/``7za`` because Python ships no decoder for it.

    Replaces the former Windows branches of :func:`unzip`, which were
    ``# TODO: implement`` + ``pass``: extraction silently did nothing and
    returned successfully, so the caller went on to use files that were never
    written (this is what left Anime4K shader extraction a no-op on Windows).
    """
    file_path = Path(file_path)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = file_path.suffix.lower()

    if suffix == ".zip":
        with zipfile.ZipFile(file_path) as archive:
            for info, destination in _safe_zip_members(archive, target_dir):
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return target_dir

    if suffix == ".7z":
        seven_zip = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if not seven_zip:
            raise RuntimeError(
                f"Cannot extract {file_path.name}: no 7z binary found on PATH."
            )
        subprocess.run(
            [seven_zip, "x", "-y", str(file_path), f"-o{target_dir}"],
            check=True, timeout=600,
        )
        return target_dir

    raise ValueError(f"Unsupported archive format: {file_path}")


def unzip(file_path, target_dir):
    """Backwards-compatible alias for :func:`extract_archive`.

    Used by: mediaforge.anime4k.anime4k.extract_anime4k().
    """
    return extract_archive(file_path, target_dir)
