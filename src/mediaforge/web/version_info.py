"""Version detection and GitHub-based update checker."""


def _get_version():
    """Return the base version string from package metadata (e.g. '2.1.6')."""
    try:
        from importlib.metadata import version

        return version("mediaforge")
    except Exception:
        return ""


def _get_dev_install_info():
    """
    Detect whether mediaforge was installed from a Git branch (dev install).

    pip writes a ``direct_url.json`` file into the dist-info directory whenever
    a package is installed via ``git+https://...``.  We read that file to get
    the exact commit SHA and the requested revision.

    A git install is only considered a *dev* install when the requested revision
    is a branch name (e.g. ``models``) rather than a version tag (e.g. ``v2.1.7``).

    Returns:
        (is_dev: bool, full_commit_sha: str | None)
    """
    try:
        import importlib.metadata as _meta
        import json as _json
        import re as _re

        dist = _meta.distribution("mediaforge")
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            return False, None
        data = _json.loads(direct_url_text)
        vcs_info = data.get("vcs_info", {})
        if vcs_info.get("vcs") == "git":
            commit_id = vcs_info.get("commit_id", "")
            requested_revision = vcs_info.get("requested_revision", "")
            # Version tags like v2.1.7 or 2.1.7 are release installs, not dev
            if _re.match(r"^v?\d+\.\d+", requested_revision):
                return False, None
            return True, commit_id if commit_id else None
        return False, None
    except Exception:
        return False, None


def _is_source_build():
    """
    True when mediaforge was installed from a local directory that is
    itself a git checkout -- covers both an editable install (``uv sync`` /
    ``pip install -e .`` run directly against a cloned repo, e.g. this
    project's own dev workflow) *and* a plain, non-editable
    ``pip install /path/to/checkout`` of a freshly ``git clone``d repo
    (e.g. a custom Docker/Portainer stack that clones ``main`` and pip
    installs it into the container on every start, instead of using the
    official ghcr.io image or a PyPI release).

    pip writes a ``direct_url.json`` with a ``file://`` URL for *any* local
    directory install, editable or not -- that alone doesn't distinguish
    "source checkout" from "some other local folder", so this additionally
    resolves the URL back to a path and checks for a ``.git`` subdirectory,
    which only a real repo clone has.

    Deliberately does NOT shell out to ``git rev-parse`` for a commit hash --
    the git binary isn't guaranteed to be present in every environment this
    might run in (e.g. a minimal container image), and the UI only needs a
    plain "this is a source build, not a release" flag here. (See
    ``_get_dev_install_info()`` above for the git-branch-VCS-install case,
    which already provides a real commit hash when pip itself did the clone.)

    The official Docker image is unaffected by this check: its Dockerfile
    ``COPY``s only ``src/`` into the image, and ``.dockerignore`` excludes
    ``.git`` from the build context -- so ``/app`` has no ``.git`` directory
    there even though the package is, under the hood, still installed via
    ``uv sync`` (editable, from a local ``file://`` path) just like a dev
    checkout is.
    """
    try:
        import importlib.metadata as _meta
        import json as _json
        from pathlib import Path as _Path
        from urllib.parse import urlparse as _urlparse
        from urllib.request import url2pathname as _url2pathname

        dist = _meta.distribution("mediaforge")
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            return False
        data = _json.loads(direct_url_text)
        url = data.get("url", "")
        if not url.startswith("file://"):
            return False
        source_dir = _Path(_url2pathname(_urlparse(url).path))
        return (source_dir / ".git").is_dir()
    except Exception:
        return False


def _get_display_version():
    """
    Return the version string shown in the UI.

    - Release install (``@v2.1.6``):        ``"2.1.6"``
    - Dev install    (``@main``):            ``"2.1.6-dev+abc1234"``
    - Source build (local git checkout, editable or not -- e.g. this
      project's own dev setup, or a Docker/Portainer stack that clones the
      repo and pip installs it at container start): ``"2.1.6 DEV"``
    """
    base = _get_version()
    if not base:
        return ""
    is_dev, commit_hash = _get_dev_install_info()
    if is_dev and commit_hash:
        return f"{base}-dev+{commit_hash[:7]}"
    if _is_source_build():
        return f"{base} DEV"
    return base


# ---------------------------------------------------------------------------
# Update checker
# ---------------------------------------------------------------------------
_GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/PD-Codes/MediaForge/releases/latest"
)
_GITHUB_COMMITS_URL = (
    "https://api.github.com/repos/PD-Codes/MediaForge/commits/main"
)
_UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours

# Process-lifetime cache of the last update check. Populated by
# _do_update_check() and read directly by routes/update.py (status endpoint)
# and routes/v1_api.py (version field) instead of re-querying GitHub per request.
_update_cache: dict = {
    "latest_version": None,
    "update_available": False,
    "release_url": None,
    "release_notes": None,
    "checked_at": 0.0,
    "error": None,
    "is_dev_install": False,
}


def _fetch_latest_release():
    """Return (version, release_url, release_notes) from the GitHub Releases API."""
    import json
    import urllib.request as _ureq

    try:
        req = _ureq.Request(
            _GITHUB_RELEASES_URL,
            headers={
                "User-Agent": "mediaforge-update-checker/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with _ureq.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        tag = data.get("tag_name", "")
        version = tag.lstrip("v")
        return version, data.get("html_url"), data.get("body") or ""
    except Exception:
        return None, None, None


def _fetch_latest_commit_sha():
    """Return the full SHA of the latest commit on the main branch."""
    import json
    import urllib.request as _ureq

    try:
        req = _ureq.Request(
            _GITHUB_COMMITS_URL,
            headers={
                "User-Agent": "mediaforge-update-checker/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with _ureq.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return data.get("sha", None)
    except Exception:
        return None


def _do_update_check():
    """Refresh ``_update_cache`` in place with the latest known version/commit.

    Dev installs compare the local commit SHA against the tip of ``main``;
    release installs compare semantic versions against the latest GitHub
    Release. Used by: routes/update.py's status endpoint (throttled to once
    per ``_UPDATE_CHECK_INTERVAL``).
    """
    import time
    from packaging.version import InvalidVersion, Version

    is_dev, full_commit_hash = _get_dev_install_info()
    local_base = _get_version()

    _update_cache["checked_at"] = time.time()
    _update_cache["is_dev_install"] = is_dev

    if is_dev:
        # Dev install: compare our commit SHA against the latest on main branch
        latest_sha = _fetch_latest_commit_sha()
        if latest_sha and full_commit_hash:
            update_available = not latest_sha.startswith(full_commit_hash[:7]) and latest_sha != full_commit_hash
            _update_cache["update_available"] = update_available
            _update_cache["latest_version"] = latest_sha[:7]
            _update_cache["release_url"] = (
                "https://github.com/PD-Codes/MediaForge/commits/main"
            )
            _update_cache["release_notes"] = None
            _update_cache["error"] = None
        else:
            _update_cache["update_available"] = False
            _update_cache["latest_version"] = None
            _update_cache["error"] = "GitHub nicht erreichbar"
    else:
        # Release install: compare version numbers against latest GitHub Release
        latest, release_url, release_notes = _fetch_latest_release()
        _update_cache["latest_version"] = latest
        _update_cache["release_url"] = release_url
        _update_cache["release_notes"] = release_notes

        if latest and local_base:
            try:
                _update_cache["update_available"] = Version(latest) > Version(local_base)
                _update_cache["error"] = None
            except InvalidVersion:
                _update_cache["update_available"] = False
                _update_cache["error"] = "Versionsformat unbekannt"
        else:
            _update_cache["update_available"] = False
            _update_cache["error"] = "GitHub nicht erreichbar" if not latest else None
