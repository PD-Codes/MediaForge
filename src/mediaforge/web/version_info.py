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


def _is_editable_install():
    """
    True when mediaforge is installed in "editable" mode from a local
    checkout (``pip install -e .`` / ``uv sync`` against a cloned repo),
    as opposed to a normal wheel/sdist install from PyPI, a git ref, or a
    PyInstaller build.

    This is intentionally separate from ``_get_dev_install_info()`` above:
    that one only fires for ``pip install git+...@branch`` installs and
    drives the self-update "dev channel" logic (it needs a real commit SHA
    to compare against). A local editable install has no ``vcs_info`` at
    all -- pip's ``direct_url.json`` instead carries a ``dir_info.editable``
    flag -- so it's invisible to that check even though it's obviously "not
    the packaged release". Kept as its own helper, used only for the
    version string shown in the UI, so it can't affect update-check behavior.
    """
    try:
        import importlib.metadata as _meta
        import json as _json

        dist = _meta.distribution("mediaforge")
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            return False
        data = _json.loads(direct_url_text)
        return bool(data.get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _get_display_version():
    """
    Return the version string shown in the UI.

    - Release install (``@v2.1.6``):        ``"2.1.6"``
    - Dev install    (``@main``):            ``"2.1.6-dev+abc1234"``
    - Local editable install (``uv sync`` / ``pip install -e .`` on a
      checked-out repo, e.g. this project's own dev setup): ``"2.1.6 DEV"``
    """
    base = _get_version()
    if not base:
        return ""
    is_dev, commit_hash = _get_dev_install_info()
    if is_dev and commit_hash:
        return f"{base}-dev+{commit_hash[:7]}"
    if _is_editable_install():
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
