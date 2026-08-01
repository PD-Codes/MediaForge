"""
Self-update support for **pip** and **pipx** installations.

Both self-update telemetry events are wired at finalize_after_restart(), i.e.
AFTER the restart: detail.self_update (success/failure of an update run, plus
the from/to version) and flag.self_update (pure usage counter, once per update
run that was actually carried out -- successful, interrupted or failed alike).
Neither is reported from start_update(): that process exits for the update
helper ~1.5s later and the telemetry worker is a daemon thread without a flush
at exit, so an event submitted there is usually cut off mid-POST. A pure
restart (start_restart()) is not an update and does not count as one.
A Docker code update (see below) reports the same two events plus its own
detail.docker_code_update, and it reports them from the same two places: the
success case after the in-place restart, the failed-install case immediately
(that path does not restart, so the worker thread is still alive to finish
the POST). A preflight that blocks the update is never reported -- that is
the check doing its job, not a fault. See telemetry/registry.py.

Capabilities by install type:

  - ``pip-release`` / ``pip-dev`` / ``pipx``  → self-update + channel switch
  - ``docker``                                → *code update* only (stable channel)
  - ``frozen`` / ``unknown``                  → not supported (UI shows a hint)

Two distinct capability flags exist and they are deliberately NOT the same
thing:

``can_self_update``
    "this install can replace itself, dependencies and all, through its own
    package manager and relaunch through a detached helper script". Stays
    ``False`` for Docker on purpose -- every existing caller of this flag
    (the auto-update worker in ``routes/update.py``, the channel switch and
    the auto-update card in ``static/settings.js``) means exactly that and
    must keep meaning it. In particular the auto-update worker must never
    touch a container unattended.

``can_code_update``
    "the mediaforge package inside this environment can be swapped for a
    newer *stable* release without touching its dependency tree". Only
    Docker sets this without ``can_self_update``; it powers the manual
    "Install update now" button in the container and nothing else.

The Docker code update is intentionally narrow. The container image is not
rebuilt -- only the ``mediaforge`` wheel inside ``/app/.venv`` is replaced,
with ``--no-deps``, after ``docker_update_preflight()`` has proven that every
requirement of the target version is already satisfied by what the image
ships. That keeps the operation to a few seconds and makes it impossible for
a dependency tree to be resolved (or compiled) inside a container that has no
build tooling. It also means the update is lost the moment the container is
recreated from the image, which the UI says out loud every single time.

Because a running Python process cannot reliably replace its own on-disk files
(especially on Windows) and cannot restart itself, the actual upgrade happens in
a small *detached helper script* (``.sh`` on POSIX, ``.bat`` on Windows):

    1. the app writes the helper, spawns it detached and exits
    2. the helper waits for the old PID to disappear
    3. the helper runs the pip/pipx upgrade (logging everything)
    4. the helper relaunches the app via ``python -m mediaforge <original args>``

A small set of state files in ``~/.mediaforge`` let the *new* process and the
frontend follow the progress across the restart.

Used by: ``routes/update.py`` (the ``/api/update/*`` endpoints call
``detect_install``, ``start_update``, ``read_status`` and ``ack_status``) and
``app.py``'s ``create_app()``, which calls ``finalize_after_restart()`` once at
startup to resolve the state left behind by a just-completed helper run.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..logger import get_logger
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PACKAGE = "mediaforge"
REPO_URL = "https://github.com/PD-Codes/MediaForge.git"
DEV_BRANCH = "main"
DEV_SPEC = f"git+{REPO_URL}@{DEV_BRANCH}"

CONFIG_DIR = Path.home() / ".mediaforge"
STATE_FILE = CONFIG_DIR / "update.state"        # idle|installing|restarting|success|failed
META_FILE = CONFIG_DIR / "update.meta.json"
LOG_FILE = CONFIG_DIR / "update.log"

# Captured at import time (early in startup) so a relaunch reuses the same flags
# (port / host / no-browser / …).  argparse never mutates sys.argv, but we copy
# it defensively all the same.
ORIGINAL_ARGV: list[str] = list(sys.argv[1:])

_VALID_STATES = {"idle", "installing", "restarting", "success", "failed"}

# ---------------------------------------------------------------------------
# Docker code update
# ---------------------------------------------------------------------------
# PyPI metadata endpoint for one specific version of one specific package.
PYPI_VERSION_JSON_URL = "https://pypi.org/pypi/{package}/{version}/json"

# A target version ends up inside a command line, so it is validated against
# this instead of being trusted. Only the shapes MediaForge actually publishes
# are accepted (PEP 440 release / pre-release / post / dev segments); anything
# with a space, a quote, a slash or a shell metacharacter cannot match. The
# version itself never comes from the client either -- routes/update.py takes
# it from the server-side update cache -- this is the second line of defence.
_TARGET_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+){0,3}(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$"
)

# Network timeout for the PyPI metadata lookup (connect, read).
_PREFLIGHT_TIMEOUT = (10, 15)

# Upper bound for the in-container `uv pip install --no-deps` run. With
# --no-deps this is a single wheel download plus an unpack, so anything past
# a few minutes means something is stuck rather than slow.
_DOCKER_INSTALL_TIMEOUT = 600

# Preflight result cache. The check goes out to PyPI, so repeated clicks (or a
# bored admin hammering the endpoint) must not turn into repeated outbound
# requests. Keyed by target version, short-lived on purpose: a 60s window is
# long enough to cover "preflight, read the dialog, confirm" and short enough
# that a genuinely changed answer is never stale for long.
_PREFLIGHT_CACHE_TTL = 60.0
_preflight_cache: dict = {"version": None, "at": 0.0, "result": None}
_preflight_lock = threading.Lock()


def is_valid_target_version(version) -> bool:
    """True when *version* is a plausible MediaForge release number.

    Guards every place a version string reaches a command line. See
    ``_TARGET_VERSION_RE`` above for why this is deliberately strict.
    """
    return bool(version) and bool(_TARGET_VERSION_RE.match(str(version).strip()))


# ---------------------------------------------------------------------------
# Install-type detection
# ---------------------------------------------------------------------------
def _in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("MEDIAFORGE_DOCKER") == "1"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _is_pipx() -> bool:
    """True when running from a pipx-managed virtualenv."""
    if os.environ.get("PIPX_HOME") or os.environ.get("PIPX_BIN_DIR"):
        # Presence of these env vars alone isn't proof (they can leak into a
        # subshell that isn't actually the pipx venv), so we don't branch on
        # them here -- just fall through to the prefix check below.
        pass
    probe = (sys.prefix + os.sep + (sys.executable or "")).replace("\\", "/").lower()
    parts = set(probe.split("/"))
    return "pipx" in parts or "/pipx/" in probe


def _dev_install_info() -> tuple[bool, str | None]:
    """
    Detect a git/branch (dev) install via pip's ``direct_url.json``.

    Returns (is_dev, commit_sha).  A version tag (``v2.1.7``) counts as a
    *release* install, a branch name (``models``) as a *dev* install.
    """
    try:
        import importlib.metadata as _meta
        import re as _re

        dist = _meta.distribution(PACKAGE)
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False, None
        data = json.loads(raw)
        vcs = data.get("vcs_info", {})
        if vcs.get("vcs") == "git":
            requested = vcs.get("requested_revision", "") or ""
            if _re.match(r"^v?\d+\.\d+", requested):
                return False, None
            return True, vcs.get("commit_id") or None
        return False, None
    except Exception:
        return False, None


def detect_install() -> dict:
    """
    Return a description of how this instance was installed.

    Keys:
      type              : pip-release | pip-dev | pipx | docker | frozen | unknown
      channel           : 'stable' | 'dev' | None
      manager           : 'pip' | 'pipx' | None    (how to perform the upgrade)
      can_self_update   : bool   -- full self-update incl. dependencies + channel switch
      can_code_update   : bool   -- swap just the mediaforge package (Docker)
      code_update_reason: str|None -- why can_code_update is False
      python            : sys.executable

    ``can_self_update`` keeps its original meaning exactly: Docker stays
    ``False`` there, so the auto-update worker (routes/update.py), the channel
    switch and the auto-update card (static/settings.js) behave as before and
    a container is never updated unattended. The Docker button rides on the
    separate ``can_code_update`` flag instead -- see the module docstring.
    """
    info = {
        "type": "unknown",
        "channel": None,
        "manager": None,
        "can_self_update": False,
        "can_code_update": False,
        "code_update_reason": "unsupported",
        "python": sys.executable or "",
    }

    # Order matters: frozen / docker take precedence — neither can self-update.
    if _is_frozen():
        info["type"] = "frozen"
        info["code_update_reason"] = "frozen"
        return info
    if _in_docker():
        info["type"] = "docker"
        # channel still meaningful for display
        is_dev, _ = _dev_install_info()
        info["channel"] = "dev" if is_dev else "stable"
        # Stable only: the dev channel installs straight from the git branch,
        # which means a source build inside the container -- that needs build
        # tooling the image does not carry, and its version string never
        # changes, so there is nothing to pin --no-deps against either.
        if info["channel"] == "dev":
            info["code_update_reason"] = "docker_dev_channel"
        else:
            info["can_code_update"] = True
            info["code_update_reason"] = None
        return info

    is_dev, _ = _dev_install_info()
    info["channel"] = "dev" if is_dev else "stable"

    if _is_pipx():
        info["type"] = "pipx"
        info["manager"] = "pipx"
        info["can_self_update"] = True
        info["can_code_update"] = True
        info["code_update_reason"] = None
        return info

    info["type"] = "pip-dev" if is_dev else "pip-release"
    info["manager"] = "pip"
    info["can_self_update"] = True
    info["can_code_update"] = True
    info["code_update_reason"] = None
    return info


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def _package_dir_writable() -> tuple[bool, str | None]:
    """Check whether the installed package directory is writable (pip only)."""
    try:
        import importlib.util

        spec = importlib.util.find_spec("mediaforge")
        if not spec or not spec.origin:
            return True, None  # can't tell — let pip try
        pkg_dir = Path(spec.origin).resolve().parent
        target = pkg_dir.parent  # site-packages
        if os.access(target, os.W_OK):
            return True, None
        return False, str(target)
    except Exception:
        return True, None


def build_upgrade_cmd(manager: str, target_channel: str, force: bool) -> list[str]:
    """Build the package-manager command that performs the upgrade / switch."""
    spec = DEV_SPEC if target_channel == "dev" else PACKAGE
    py = sys.executable or "python3"

    if manager == "pipx":
        # pipx install --force cleanly overwrites and handles channel switches.
        return ["pipx", "install", "--force", spec]

    # pip
    cmd = [py, "-m", "pip", "install", "--upgrade", "--no-input"]
    if force:
        # Channel switch: force pip to actually replace the distribution even
        # when the version string does not increase. ``--no-deps`` keeps it fast
        # and reliable — it reinstalls only the mediaforge package (dependencies
        # are already present from the previous channel), instead of cloning &
        # rebuilding the entire dependency tree, which could take many minutes
        # or stall.
        cmd += ["--force-reinstall", "--no-deps"]
    cmd.append(spec)
    return cmd


def relaunch_cmd() -> list[str]:
    """Command that restarts the app with the original CLI flags."""
    py = sys.executable or "python3"
    return [py, "-m", "mediaforge", *ORIGINAL_ARGV]


# ---------------------------------------------------------------------------
# State helpers (shared with the frontend through small files)
# ---------------------------------------------------------------------------
def _write_state(state: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(state.strip(), encoding="utf-8")
    except Exception:
        pass


def _read_state() -> str:
    try:
        s = STATE_FILE.read_text(encoding="utf-8").strip()
        return s if s in _VALID_STATES else "idle"
    except Exception:
        return "idle"


def _write_meta(meta: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        META_FILE.write_text(json.dumps(meta), encoding="utf-8")
    except Exception:
        pass


def _read_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log_tail(max_bytes: int = 8000) -> str:
    try:
        data = LOG_FILE.read_bytes()
        return data[-max_bytes:].decode("utf-8", "replace")
    except Exception:
        return ""


def read_status() -> dict:
    """Snapshot consumed by ``GET /api/update/status``."""
    meta = _read_meta()
    return {
        "state": _read_state(),
        "restart_only": meta.get("restart_only"),
        # Lets the overlay repeat the "this is lost on the next image pull"
        # warning at the moment the update actually finishes.
        "docker_code_update": bool(meta.get("docker_code_update")),
        "channel": meta.get("channel"),
        "target_channel": meta.get("target_channel"),
        "from_version": meta.get("from_version"),
        "to_version": meta.get("to_version"),
        "error": meta.get("error"),
        "started_at": meta.get("started_at"),
        "log": _log_tail(),
    }


def ack_status() -> None:
    """Reset the state back to idle (frontend dismissed the result)."""
    _write_state("idle")
    meta = _read_meta()
    for k in ("error", "to_version", "telemetry_reported", "docker_code_update"):
        meta.pop(k, None)
    _write_meta(meta)


# ---------------------------------------------------------------------------
# Helper-script generation
# ---------------------------------------------------------------------------
def _write_helper_script(upgrade_cmd: list[str], relaunch: list[str], pid: int) -> Path:
    """Write the detached updater script for the current platform."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    is_windows = os.name == "nt"

    state_path = str(STATE_FILE)
    log_path = str(LOG_FILE)

    if is_windows:
        # PowerShell helper. We deliberately avoid a .bat file: `timeout` and
        # `find` need an interactive console, which a detached process does not
        # have, so the old batch approach hung forever. `Wait-Process` blocks
        # cleanly until the old PID is gone — no busy-loop, no console needed.
        script = CONFIG_DIR / "updater.ps1"
        up_exe = _ps_quote(upgrade_cmd[0])
        up_args = _ps_array(upgrade_cmd[1:])
        rl_exe = _ps_quote(relaunch[0])
        rl_args = _ps_array(relaunch[1:])
        log_q = _ps_quote(log_path)
        state_q = _ps_quote(state_path)
        cwd_q = _ps_quote(os.getcwd())
        rl_argline = f" -ArgumentList {rl_args}" if relaunch[1:] else ""
        content = f"""$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {pid}
try {{ Wait-Process -Id $pidToWait -ErrorAction SilentlyContinue }} catch {{}}
while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 400 }}
$env:GIT_TERMINAL_PROMPT = '0'
$env:PIP_NO_INPUT = '1'
$log = {log_q}
$state = {state_q}
Add-Content -Path $log -Value ''
Add-Content -Path $log -Value ('[updater] starting upgrade ' + (Get-Date))
$out = & {up_exe} {up_args} 2>&1
$code = $LASTEXITCODE
$out | Out-File -FilePath $log -Append -Encoding utf8
if ($code -eq 0) {{
    Add-Content -Path $log -Value '[updater] upgrade OK'
    Set-Content -Path $state -Value 'restarting' -NoNewline
}} else {{
    Add-Content -Path $log -Value '[updater] upgrade FAILED'
    Set-Content -Path $state -Value 'failed' -NoNewline
}}
Add-Content -Path $log -Value '[updater] relaunching'
Start-Process -FilePath {rl_exe}{rl_argline} -WorkingDirectory {cwd_q}
"""
        script.write_text(content, encoding="utf-8")
        return script

    # POSIX (Linux / macOS)
    script = CONFIG_DIR / "updater.sh"
    up = " ".join(shlex.quote(c) for c in upgrade_cmd)
    rl = " ".join(shlex.quote(c) for c in relaunch)
    cwd = shlex.quote(os.getcwd())
    log_q = shlex.quote(log_path)
    state_q = shlex.quote(state_path)
    content = f"""#!/bin/sh
PID={pid}
while kill -0 "$PID" 2>/dev/null; do
    sleep 0.4
done
export GIT_TERMINAL_PROMPT=0
export PIP_NO_INPUT=1
{{
echo ""
echo "[updater] starting upgrade $(date)"
}} >> {log_q} 2>&1
if {up} >> {log_q} 2>&1; then
    echo "[updater] upgrade OK" >> {log_q} 2>&1
    printf 'restarting' > {state_q}
else
    echo "[updater] upgrade FAILED" >> {log_q} 2>&1
    printf 'failed' > {state_q}
fi
echo "[updater] relaunching" >> {log_q} 2>&1
cd {cwd} || true
if command -v setsid >/dev/null 2>&1; then
    setsid {rl} >> {log_q} 2>&1 &
else
    nohup {rl} >> {log_q} 2>&1 &
fi
"""
    script.write_text(content, encoding="utf-8")
    os.chmod(script, 0o755)
    return script


def _win_quote(arg: str) -> str:
    """Quote a single argument for ``cmd.exe``-style command lines.

    Note: no current caller uses this (the Windows helper script is generated
    with ``_ps_quote``/``_ps_array`` for PowerShell instead); kept as a small
    utility rather than removed since this is a comments-only pass.
    """
    if not arg:
        return '""'
    if any(c in arg for c in ' \t"&|<>^()'):
        return '"' + arg.replace('"', '""') + '"'
    return arg


def _ps_quote(arg: str) -> str:
    """Single-quote a string for PowerShell ('' escapes a literal quote)."""
    return "'" + str(arg).replace("'", "''") + "'"


def _ps_array(args) -> str:
    """Render a list as a PowerShell array literal: @('a','b')."""
    if not args:
        return "@()"
    return "@(" + ", ".join(_ps_quote(a) for a in args) + ")"


def _spawn_detached(script: Path) -> None:
    """Launch the helper script fully detached from this process."""
    devnull = open(os.devnull, "wb")
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", str(script),
            ],
            stdin=devnull, stdout=devnull, stderr=devnull,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            ["sh", str(script)],
            stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True, close_fds=True,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
class UpdateError(Exception):
    """Raised when an update cannot be started.

    ``blocking`` carries the unmet-requirement list of a failed Docker
    preflight (see ``docker_update_preflight``) so the route can hand the UI
    a structured reason instead of only a sentence; ``reason`` is a stable
    machine-readable code the frontend can branch on. Both are optional and
    default to empty, so the plain ``UpdateError("...")`` call sites elsewhere
    in this module are unaffected.
    """

    def __init__(self, message: str, *, blocking=None, reason: str | None = None):
        super().__init__(message)
        self.blocking = list(blocking or [])
        self.reason = reason


def start_update(target_channel: str | None = None) -> dict:
    """
    Begin a self-update (or channel switch).

    Writes the state files, spawns the detached helper and returns a small dict.
    The caller is responsible for exiting the process shortly afterwards so the
    helper can replace files and relaunch.

    Raises ``UpdateError`` when the install type does not support self-update,
    the package directory is not writable, or an update is already running.
    """
    info = detect_install()
    if not info["can_self_update"]:
        raise UpdateError(f"self-update not supported for install type '{info['type']}'")

    current_channel = info["channel"] or "stable"
    target = (target_channel or current_channel).lower()
    if target not in ("stable", "dev"):
        raise UpdateError(f"invalid channel '{target}'")
    # A dev/branch install (git+...@main) keeps a static version string
    # across commits, so plain --upgrade is a no-op: pip sees the same
    # version and never re-clones. Force a reinstall for dev so the newest
    # commit is actually pulled, not just on a channel switch.
    force = target != current_channel or target == "dev"

    if info["manager"] == "pip":
        ok, target_dir = _package_dir_writable()
        if not ok:
            raise UpdateError(
                f"installation directory is not writable ({target_dir}); "
                "run as the owning user or reinstall with --user / a virtualenv"
            )

    if _read_state() in ("installing", "restarting"):
        raise UpdateError("an update is already in progress")

    from_version = _current_version()
    meta = {
        "channel": current_channel,
        "target_channel": target,
        "from_version": from_version,
        "to_version": None,
        "error": None,
        "started_at": time.time(),
    }
    _write_meta(meta)
    _write_state("installing")

    # Telemetry: nothing is submitted here on purpose. This process exits for
    # the update helper a moment later and the telemetry worker is a daemon
    # thread, so a POST started here is cut off mid-flight. flag.self_update
    # is reported after the restart instead, in finalize_after_restart().

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        header = (
            f"=== AniWorld self-update ===\n"
            f"install type : {info['type']}\n"
            f"channel      : {current_channel} -> {target}\n"
            f"from version : {from_version}\n"
        )
        LOG_FILE.write_text(header, encoding="utf-8")
    except Exception:
        pass

    upgrade = build_upgrade_cmd(info["manager"], target, force)
    relaunch = relaunch_cmd()
    script = _write_helper_script(upgrade, relaunch, os.getpid())
    _spawn_detached(script)

    return {
        "ok": True,
        "type": info["type"],
        "channel": current_channel,
        "target_channel": target,
        "command": " ".join(upgrade),
    }


def _write_restart_script(relaunch: list[str], pid: int) -> Path:
    """Write a detached helper that waits for *pid* to exit, then relaunches the
    app with the same arguments (no upgrade -- pure restart)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    is_windows = os.name == "nt"
    log_path = str(LOG_FILE)

    if is_windows:
        script = CONFIG_DIR / "restart.ps1"
        rl_exe = _ps_quote(relaunch[0])
        rl_args = _ps_array(relaunch[1:])
        log_q = _ps_quote(log_path)
        cwd_q = _ps_quote(os.getcwd())
        rl_argline = f" -ArgumentList {rl_args}" if relaunch[1:] else ""
        content = f"""$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {pid}
try {{ Wait-Process -Id $pidToWait -ErrorAction SilentlyContinue }} catch {{}}
while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 400 }}
$log = {log_q}
Add-Content -Path $log -Value ('[restart] relaunching ' + (Get-Date))
Start-Process -FilePath {rl_exe}{rl_argline} -WorkingDirectory {cwd_q}
"""
        script.write_text(content, encoding="utf-8")
        return script

    # POSIX (Linux / macOS)
    script = CONFIG_DIR / "restart.sh"
    rl = " ".join(shlex.quote(c) for c in relaunch)
    cwd = shlex.quote(os.getcwd())
    log_q = shlex.quote(log_path)
    content = f"""#!/bin/sh
PID={pid}
while kill -0 "$PID" 2>/dev/null; do
    sleep 0.4
done
echo "[restart] relaunching $(date)" >> {log_q} 2>&1
cd {cwd} || true
if command -v setsid >/dev/null 2>&1; then
    setsid {rl} >> {log_q} 2>&1 &
else
    nohup {rl} >> {log_q} 2>&1 &
fi
"""
    script.write_text(content, encoding="utf-8")
    os.chmod(script, 0o755)
    return script


def start_restart() -> dict:
    """Restart the app with the same CLI args -- no update.

    Writes the same state files the self-update flow uses (so the existing
    restart overlay can follow along), spawns a detached wait+relaunch helper,
    and returns.  The caller must exit the process shortly afterwards so the
    helper can relaunch it.
    """
    info = detect_install()
    if info["type"] in ("frozen", "docker"):
        raise UpdateError(f"restart not supported for install type '{info['type']}'")
    if _read_state() in ("installing", "restarting"):
        raise UpdateError("an update or restart is already in progress")

    meta = {
        "channel": info.get("channel"),
        "target_channel": None,
        "from_version": _current_version(),
        "to_version": None,
        "error": None,
        "started_at": time.time(),
        "restart_only": True,
    }
    _write_meta(meta)
    _write_state("restarting")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("=== MediaForge restart ===\n", encoding="utf-8")
    except Exception:
        pass

    relaunch = relaunch_cmd()
    script = _write_restart_script(relaunch, os.getpid())
    _spawn_detached(script)
    return {"ok": True, "type": info["type"]}


# ---------------------------------------------------------------------------
# Docker code update — dependency preflight, in-container install, in-place
# restart.  See the module docstring for the "why only this narrow thing".
# ---------------------------------------------------------------------------
def _site_packages_dir() -> str:
    """site-packages of the interpreter that is currently running."""
    import sysconfig

    return sysconfig.get_paths().get("purelib") or ""


def _venv_writable() -> tuple[bool, str | None]:
    """Whether a package can be installed into the running environment.

    The recommended compose file runs the container with ``read_only: true``,
    which makes ``/app/.venv`` read-only as well -- an install would fail
    halfway through with a wall of pip output. Checking up front turns that
    into one clear sentence.
    """
    try:
        target = _site_packages_dir()
        if not target:
            return True, None  # can't tell — let the installer try
        if os.access(target, os.W_OK):
            return True, None
        return False, target
    except Exception:
        return True, None


def _installed_requirements() -> list[str]:
    """``Requires-Dist`` of the *installed* mediaforge distribution."""
    from importlib.metadata import metadata

    return list(metadata(PACKAGE).get_all("Requires-Dist") or [])


def _fetch_target_requirements(target_version: str) -> list[str]:
    """``requires_dist`` of *target_version* straight from the PyPI JSON API.

    Uses the project's own niquests session (``config.GLOBAL_SESSION``), so
    this request goes through the configured DoH resolver and the OS trust
    store like every other outbound call, instead of urllib's plain system
    DNS. Imported lazily to keep this module a light leaf import.
    """
    from ..config import GLOBAL_SESSION

    url = PYPI_VERSION_JSON_URL.format(package=PACKAGE, version=target_version)
    resp = GLOBAL_SESSION.get(
        url, timeout=_PREFLIGHT_TIMEOUT, headers={"Accept": "application/json"}
    )
    resp.raise_for_status()
    data = resp.json()
    return list((data.get("info") or {}).get("requires_dist") or [])


def _docker_preflight_uncached(target_version: str) -> dict:
    """Do the actual work of ``docker_update_preflight`` (no caching)."""
    result: dict = {"ok": False, "blocking": [], "checked": 0, "error": None}

    if not is_valid_target_version(target_version):
        result["error"] = f"invalid target version '{target_version}'"
        return result

    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _dist_version

        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.utils import canonicalize_name
    except Exception as exc:  # pragma: no cover - packaging is a hard dependency
        result["error"] = f"dependency check unavailable ({exc})"
        return result

    writable, ro_dir = _venv_writable()
    if not writable:
        result["error"] = (
            f"the package directory inside the container is read-only ({ro_dir})"
        )
        return result

    try:
        current_raw = _installed_requirements()
    except Exception as exc:
        result["error"] = f"could not read the installed package metadata ({exc})"
        return result

    # Only used to mark a blocking entry as "brand new" vs "tightened" — the
    # actual gate below is always against what is really installed.
    current_names: set[str] = set()
    for raw in current_raw:
        try:
            current_names.add(canonicalize_name(Requirement(raw).name))
        except InvalidRequirement:
            continue

    try:
        target_raw = _fetch_target_requirements(target_version)
    except Exception as exc:
        result["error"] = (
            f"could not read the requirements of {target_version} from PyPI ({exc})"
        )
        return result
    if not target_raw:
        # MediaForge always declares dependencies. An empty list means the
        # metadata is unusable, not that the release has none — fail closed.
        result["error"] = f"PyPI returned no requirement list for {target_version}"
        return result

    blocking: list[dict] = []
    checked = 0
    for raw in target_raw:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            # Something we cannot verify must never be waved through.
            result["error"] = f"unparseable requirement in {target_version}: {raw!r}"
            return result

        if req.marker is not None:
            try:
                # extra="" makes every `extra == "..."` marker evaluate to
                # False: MediaForge is installed without extras, so optional
                # dependency groups do not apply. Plain environment markers
                # (python_version, sys_platform, …) are evaluated against the
                # interpreter that is actually running this container.
                if not req.marker.evaluate({"extra": ""}):
                    continue
            except Exception:
                result["error"] = f"could not evaluate requirement marker: {raw!r}"
                return result

        checked += 1
        try:
            installed = _dist_version(req.name)
        except PackageNotFoundError:
            installed = None
        except Exception:
            installed = None

        entry = {
            "name": req.name,
            "required": str(req.specifier) or "*",
            "installed": installed,
            "new": canonicalize_name(req.name) not in current_names,
        }
        if installed is None:
            blocking.append(entry)
            continue
        try:
            satisfied = (not req.specifier) or req.specifier.contains(
                installed, prereleases=True
            )
        except Exception:
            satisfied = False
        if not satisfied:
            blocking.append(entry)

    result["checked"] = checked
    result["blocking"] = blocking
    # STRICT on purpose: a single unmet or missing requirement blocks. The
    # whole point of --no-deps is that nothing has to be resolved inside the
    # container, and that only holds if *everything* is already in place.
    result["ok"] = not blocking
    return result


def docker_update_preflight(target_version: str, *, max_age: float | None = None) -> dict:
    """Can *target_version* be installed into this container with ``--no-deps``?

    Compares the ``requires_dist`` of the target release on PyPI against the
    packages actually present in the running virtualenv. Environment markers
    are evaluated against this interpreter; requirements that only apply to an
    extra are skipped (MediaForge is installed without extras).

    Returns ``{"ok": bool, "blocking": [{name, required, installed, new}, …],
    "checked": int, "error": str|None}``.

    Fail-closed in every direction: if the check itself cannot be carried out
    (PyPI unreachable, metadata unreadable, a requirement that will not parse)
    the result is ``ok=False`` with an ``error`` — never an optimistic "looks
    fine". Results are cached per target version for
    ``_PREFLIGHT_CACHE_TTL`` seconds so the endpoint cannot be turned into an
    outbound request amplifier.
    """
    ttl = _PREFLIGHT_CACHE_TTL if max_age is None else max_age
    key = str(target_version or "").strip()
    now = time.time()
    with _preflight_lock:
        cached = _preflight_cache
        if (
            cached.get("result") is not None
            and cached.get("version") == key
            and (now - float(cached.get("at") or 0.0)) < ttl
        ):
            return json.loads(json.dumps(cached["result"]))  # defensive copy

    result = _docker_preflight_uncached(key)

    with _preflight_lock:
        _preflight_cache["version"] = key
        _preflight_cache["at"] = time.time()
        _preflight_cache["result"] = result
    return json.loads(json.dumps(result))


def build_docker_upgrade_cmd(target_version: str) -> list[str]:
    """Command that swaps the mediaforge package inside the container venv.

    ``--no-deps`` is not a shortcut here, it is the safety property:
    ``docker_update_preflight()`` has already proven every requirement of the
    target release is satisfied by what the image ships, so nothing has to be
    resolved, downloaded or compiled — which a slim container could not do
    anyway. ``uv`` is what the image builds with and is on PATH there; the
    pip fallback exists for custom images that dropped it.
    """
    if not is_valid_target_version(target_version):
        raise UpdateError(f"invalid target version '{target_version}'")
    py = sys.executable or "python3"
    spec = f"{PACKAGE}=={str(target_version).strip()}"
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", py, "--upgrade", "--no-deps", spec]
    return [py, "-m", "pip", "install", "--upgrade", "--no-input", "--no-deps", spec]


def _requeue_running_downloads() -> None:
    """Put in-flight downloads back into the queue so they resume after the
    restart. Mirrors what routes/update.py does around ``start_update()``."""
    try:
        from .db import get_db

        conn = get_db()
        try:
            conn.execute(
                "UPDATE download_queue SET status = 'queued' WHERE status = 'running'"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("[DockerUpdate] could not requeue download queue", exc_info=True)


def _inplace_restart() -> None:
    """Replace this process with a fresh interpreter — the Docker restart path.

    The detached-helper trick the pip/pipx flow uses cannot work in a
    container: the app is PID 1 there, and the moment PID 1 exits the whole
    container (helper script included) is torn down. ``os.execv`` keeps PID 1
    alive and merely swaps the process image, so the freshly installed code is
    picked up without depending on a ``restart:`` policy that may not exist.
    Sockets and files Python opened are non-inheritable (PEP 446), so the
    listening port is released by the exec itself.
    """
    cmd = relaunch_cmd()
    logger.info("[DockerUpdate] restarting in place: %s", " ".join(cmd))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(cmd[0], cmd)
    except Exception:
        # Last resort: exit and let the container restart policy take over.
        logger.exception("[DockerUpdate] execv failed, exiting instead")
        os._exit(0)


def _docker_update_worker(target_version: str, cmd: list[str], meta: dict) -> None:
    """Run the in-container install, then restart in place (background thread).

    Runs off the request thread so the HTTP response returns immediately and
    the existing update overlay can follow along through
    ``GET /api/update/status`` exactly like a normal self-update.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DOCKER_INSTALL_TIMEOUT,
            env={**os.environ, "PIP_NO_INPUT": "1"},
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        output = f"[updater] install timed out after {_DOCKER_INSTALL_TIMEOUT}s"
        rc = -1
    except Exception as exc:
        output = f"[updater] install could not be started: {exc}"
        rc = -1

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(output)
            fh.write(f"\n[updater] exit code {rc}\n")
    except Exception:
        pass

    if rc != 0:
        meta["error"] = (
            "The package could not be installed inside the container. "
            "See the log below."
        )
        # No restart follows, so this process stays around long enough for the
        # telemetry worker to finish its POST — report right here.
        meta["telemetry_reported"] = True
        _write_meta(meta)
        _write_state("failed")
        _report_self_update_flag()
        _report_docker_code_update(
            status="failed",
            error_type="install_command_failed",
            from_version=meta.get("from_version"),
            to_version=target_version,
        )
        logger.error("[DockerUpdate] install failed (exit %s)", rc)
        return

    _write_state("restarting")
    logger.info("[DockerUpdate] install OK, restarting into %s", target_version)
    _requeue_running_downloads()
    time.sleep(1.0)  # let the /api/update/status poll observe "restarting"
    _inplace_restart()


def start_docker_code_update(target_version: str) -> dict:
    """Replace the mediaforge package inside this container and restart.

    Only ever reached for ``type == "docker"`` on the stable channel, and only
    after ``docker_update_preflight()`` says every requirement is already
    satisfied — that check is repeated here (from the shared cache, so it
    costs nothing) so a caller cannot skip it.

    Returns immediately; the install and the in-place restart happen in a
    background thread and are followed through the usual update state files.
    """
    if not is_valid_target_version(target_version):
        raise UpdateError(
            f"invalid target version '{target_version}'", reason="invalid_version"
        )
    target_version = str(target_version).strip()

    info = detect_install()
    if info["type"] != "docker":
        raise UpdateError(
            f"code update is Docker-only (install type '{info['type']}')",
            reason="not_docker",
        )
    if not info.get("can_code_update"):
        reason = info.get("code_update_reason") or "unsupported"
        if reason == "docker_dev_channel":
            raise UpdateError(
                "the dev channel cannot be updated inside a container; "
                "pull a new image instead",
                reason=reason,
            )
        raise UpdateError("code update not available for this container", reason=reason)

    if _read_state() in ("installing", "restarting"):
        raise UpdateError("an update is already in progress", reason="busy")

    pre = docker_update_preflight(target_version)
    if pre.get("error"):
        raise UpdateError(
            f"dependency check failed: {pre['error']}", reason="preflight_error"
        )
    if not pre.get("ok"):
        raise UpdateError(
            "the target version needs dependencies this image does not provide",
            blocking=pre.get("blocking"),
            reason="preflight_blocked",
        )

    from_version = _current_version()
    meta = {
        "channel": "stable",
        "target_channel": "stable",
        "from_version": from_version,
        "to_version": target_version,
        "error": None,
        "started_at": time.time(),
        "docker_code_update": True,
    }
    _write_meta(meta)
    _write_state("installing")

    cmd = build_docker_upgrade_cmd(target_version)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(
            "=== MediaForge Docker code update ===\n"
            f"from version : {from_version}\n"
            f"to version   : {target_version}\n"
            f"checked deps : {pre.get('checked')}\n"
            f"command      : {' '.join(cmd)}\n\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    threading.Thread(
        target=_docker_update_worker,
        args=(target_version, cmd, meta),
        daemon=True,
        name="docker-code-update",
    ).start()

    return {
        "ok": True,
        "type": "docker",
        "channel": "stable",
        "target_channel": "stable",
        "to_version": target_version,
        "checked": pre.get("checked", 0),
        "command": " ".join(cmd),
    }


def finalize_after_restart() -> None:
    """
    Called once on startup.  Resolves the state left behind by the helper:

      - ``restarting`` → upgrade succeeded, app came back → ``success``
      - ``failed``     → leave as failed (frontend will show the log)
      - ``installing`` → we never made it through the helper → ``failed``

    This is also where both self-update telemetry events are reported (see the
    module docstring): the update run is over and this process is here to stay,
    so the daemon telemetry worker can actually finish the POST. Each of the
    three branches below reports flag.self_update exactly once -- the flag
    means "an update was carried out", not "an update succeeded".
    """
    state = _read_state()
    if state == "restarting":
        meta = _read_meta()
        meta["to_version"] = _current_version()
        _write_meta(meta)
        _write_state("success")
        # A pure start_restart() ends up in the same "restarting" state but is
        # not an update, so it must not count as one.
        if not meta.get("restart_only"):
            _report_self_update_flag()
        _report_self_update(status="success",
                            from_version=meta.get("from_version"),
                            to_version=meta.get("to_version"))
        # A Docker code update also lands here (its in-place restart leaves
        # the same "restarting" state behind), and it gets its own detail
        # event on top -- it is a materially different operation from a pip
        # self-update and is worth telling apart on the server.
        if meta.get("docker_code_update"):
            _report_docker_code_update(status="success",
                                       from_version=meta.get("from_version"),
                                       to_version=meta.get("to_version"))
    elif state == "installing":
        meta = _read_meta()
        meta["error"] = "Update did not complete (process restarted unexpectedly)."
        # This branch leaves the state at "failed", which is exactly what the
        # branch below reacts to, so mark the run as reported here as well --
        # otherwise a second start before the user dismisses the result would
        # report the very same update run again.
        meta["telemetry_reported"] = True
        _write_meta(meta)
        _write_state("failed")
        _report_self_update_flag()
        _report_self_update(status="failed", error_type="interrupted",
                            from_version=meta.get("from_version"))
        if meta.get("docker_code_update"):
            _report_docker_code_update(status="failed", error_type="interrupted",
                                       from_version=meta.get("from_version"),
                                       to_version=meta.get("to_version"))
    elif state == "failed":
        # The helper script itself already wrote "failed" (the pip/pipx
        # upgrade command exited non-zero) before relaunching the app -- this
        # is the first boot after that. Report it once via a meta flag: this
        # function runs on EVERY app start, and the state stays "failed"
        # until the user opens the UI and dismisses it (ack_status()), so
        # without the flag a crash-loop before that dismissal would
        # re-report the same old failure on every restart.
        meta = _read_meta()
        if not meta.get("telemetry_reported"):
            meta["telemetry_reported"] = True
            _write_meta(meta)
            _report_self_update_flag()
            _report_self_update(status="failed", error_type="upgrade_command_failed",
                                from_version=meta.get("from_version"))
    # 'success' / 'idle' are left untouched.


def _report_self_update_flag():
    """Submit the flag.self_update stage-2 usage counter for one update run.

    Called from finalize_after_restart() only, i.e. after the update helper
    has done its work and the app is running again -- submitting it in
    start_update() lost the event to the process exit that follows there.

    A pure counter -- build_feature_flag_event() takes no metadata at all, the
    version/result context belongs to detail.self_update below. Wrapped in its
    own try/except so a telemetry bug can never affect the update flow.
    """
    try:
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.self_update"))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.self_update event", exc_info=True)


def _report_self_update(*, status, error_type=None, from_version=None, to_version=None):
    """Submit a detail.self_update telemetry event (see registry.py --
    "Ob ein Selbst-Update erfolgreich war oder fehlgeschlagen ist"). Only a
    coarse status/error classifier plus the from/to version string is sent,
    never the update log or the raw pip/pipx output (which can contain package
    index URLs). Wrapped in its own try/except so a telemetry bug can never
    affect the update flow.
    """
    try:
        metadata = {}
        if from_version:
            metadata["from_version"] = str(from_version)[:40]
        if to_version:
            metadata["to_version"] = str(to_version)[:40]
        if error_type:
            metadata["error_type"] = error_type
        event = telemetry_events.build_feature_detail_event(
            "detail.self_update", action="update", status=status,
            metadata=metadata or None,
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.self_update event", exc_info=True)


def _report_docker_code_update(*, status, error_type=None, from_version=None,
                               to_version=None):
    """Submit a detail.docker_code_update telemetry event.

    Separate from detail.self_update because the two answer different
    questions: whether a package swap inside a container works at all (and
    how often it is even attempted) says something about the Docker image,
    not about the pip self-updater. Only genuine outcomes are reported --
    success after the restart, or a failed install command. A preflight that
    *blocks* the update is deliberately NOT reported: that is the feature
    working as designed, not an error, and the same goes for an admin who
    reads the confirmation dialog and clicks away. Sends nothing but the
    coarse status, an error classifier and the from/to version -- never the
    install log or the raw uv/pip output. Wrapped in its own try/except so a
    telemetry bug can never affect the update flow.
    """
    try:
        metadata = {}
        if from_version:
            metadata["from_version"] = str(from_version)[:40]
        if to_version:
            metadata["to_version"] = str(to_version)[:40]
        if error_type:
            metadata["error_type"] = error_type
        event = telemetry_events.build_feature_detail_event(
            "detail.docker_code_update", action="update", status=status,
            metadata=metadata or None,
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.docker_code_update event",
                     exc_info=True)


def _current_version() -> str:
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:
        return ""
