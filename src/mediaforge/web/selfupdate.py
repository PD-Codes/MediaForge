"""
Self-update support for **pip** and **pipx** installations.

detail.self_update (success/failure of an update run) is wired at
finalize_after_restart() -- see _report_self_update() below. flag.self_update
(pure usage counter) is intentionally NOT wired -- out of scope for now, see
telemetry/registry.py.

Capabilities by install type:

  - ``pip-release`` / ``pip-dev`` / ``pipx``  → self-update + channel switch
  - ``docker`` / ``frozen`` / ``unknown``     → not supported (UI shows a hint)

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
import shlex
import subprocess
import sys
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
      type            : pip-release | pip-dev | pipx | docker | frozen | unknown
      channel         : 'stable' | 'dev' | None
      manager         : 'pip' | 'pipx' | None      (how to perform the upgrade)
      can_self_update : bool
      python          : sys.executable
    """
    info = {
        "type": "unknown",
        "channel": None,
        "manager": None,
        "can_self_update": False,
        "python": sys.executable or "",
    }

    # Order matters: frozen / docker take precedence — neither can self-update.
    if _is_frozen():
        info["type"] = "frozen"
        return info
    if _in_docker():
        info["type"] = "docker"
        # channel still meaningful for display
        is_dev, _ = _dev_install_info()
        info["channel"] = "dev" if is_dev else "stable"
        return info

    is_dev, _ = _dev_install_info()
    info["channel"] = "dev" if is_dev else "stable"

    if _is_pipx():
        info["type"] = "pipx"
        info["manager"] = "pipx"
        info["can_self_update"] = True
        return info

    info["type"] = "pip-dev" if is_dev else "pip-release"
    info["manager"] = "pip"
    info["can_self_update"] = True
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
    for k in ("error", "to_version", "telemetry_reported"):
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
    """Raised when an update cannot be started."""


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


def finalize_after_restart() -> None:
    """
    Called once on startup.  Resolves the state left behind by the helper:

      - ``restarting`` → upgrade succeeded, app came back → ``success``
      - ``failed``     → leave as failed (frontend will show the log)
      - ``installing`` → we never made it through the helper → ``failed``
    """
    state = _read_state()
    if state == "restarting":
        meta = _read_meta()
        meta["to_version"] = _current_version()
        _write_meta(meta)
        _write_state("success")
        _report_self_update(status="success")
    elif state == "installing":
        meta = _read_meta()
        meta["error"] = "Update did not complete (process restarted unexpectedly)."
        _write_meta(meta)
        _write_state("failed")
        _report_self_update(status="failed", error_type="interrupted")
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
            _report_self_update(status="failed", error_type="upgrade_command_failed")
    # 'success' / 'idle' are left untouched.


def _report_self_update(*, status, error_type=None):
    """Submit a detail.self_update telemetry event (see registry.py --
    "Ob ein Selbst-Update erfolgreich war oder fehlgeschlagen ist"). Only a
    coarse status/error classifier is sent, never the update log or the raw
    pip/pipx output (which can contain package index URLs). Wrapped in its
    own try/except so a telemetry bug can never affect the update flow.
    """
    try:
        metadata = {}
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


def _current_version() -> str:
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:
        return ""
