"""Python dependencies for modules — checked, and (on an admin's say-so) installed.

A module declares what it needs as ``MODULE_REQUIREMENTS`` (PEP 508 strings,
e.g. ``("discord.py>=2.3",)``). MediaForge checks them before ``register(app)``
and, when something is missing, shows the module on the Modulmanager as
"needs a dependency" with an Install button rather than silently skipping it.
This file is what that button runs.

Why the core does this at all
-----------------------------
It used to only *check*. A module that needed a pure-Python package therefore
had no supported way to get one — so module authors built their own pip runner:
``pip install --target <the module's own folder>`` plus
``sys.path.insert(0, ...)``. Both halves of that are traps:

* ``--target`` into the module folder puts the packages inside the very
  directory the module's signature is computed over (``signing.content_hash``)
  and that the store deletes with ``rmtree`` on every upgrade. The module
  invalidates its own signature by running, and reinstalls its dependencies on
  every update.
* ``sys.path.insert(0, ...)`` puts the module's transitive dependencies
  (aiohttp, yarl, multidict, typing_extensions, attrs, ...) *ahead* of the ones
  MediaForge itself is running on, process-wide, from the moment that module is
  first enabled. That is a version conflict with a delayed fuse.

So the core owns it, exactly like it already owns binary dependencies
(``mediaforge/autodeps.py``):

* One shared directory, ``~/.mediaforge/module_deps/``, outside every module
  folder — nothing a module writes there can touch its own signature, and a
  store upgrade doesn't wipe it.
* Appended to ``sys.path``, never inserted at the front: whatever MediaForge
  itself ships always wins an import. A module that needs a *newer* version of
  a package the core already has doesn't get it — it gets a clear
  "missing dependency: x>=2 (have 1.4)" on its card instead of silently
  shadowing the core's copy and breaking something unrelated.
* Installation is an explicit admin action (a button on the Modulmanager),
  never an implicit side effect of a request. Downloading and executing code
  from PyPI is not something a module gets to do to an admin by being enabled.
"""

import subprocess
import sys
import threading
from pathlib import Path

from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

logger = get_logger(__name__)

# Where module dependencies are installed. Deliberately next to the database and
# the modules themselves, not inside any module folder -- see this file's
# docstring for why that distinction is the whole point.
MODULE_DEPS_DIR = Path(MEDIAFORGE_CONFIG_DIR) / "module_deps"

# One install at a time. pip is not safe to run concurrently against the same
# --target directory (two installs can interleave their file writes), and two
# admins clicking Install at once is not an exotic scenario in a shared install.
_install_lock = threading.Lock()

# How long a single pip run may take before it is killed. Generous: a cold
# install of something like discord.py pulls half a dozen wheels, and a slow
# NAS on a slow line is a normal MediaForge host.
_PIP_TIMEOUT = 600


def deps_dir() -> Path:
    """The module dependency directory, created if missing."""
    try:
        MODULE_DEPS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[ModuleDeps] Could not create %s", MODULE_DEPS_DIR)
    return MODULE_DEPS_DIR


def ensure_on_sys_path() -> None:
    """Make already-installed module dependencies importable.

    ``append``, never ``insert(0)`` -- see this file's docstring. Called once at
    import of the thirdparties package, i.e. before any module is imported, so a
    dependency installed in an earlier run is simply there.
    """
    path = str(deps_dir())
    if path not in sys.path:
        sys.path.append(path)


def _requirement_status(raw):
    """(requirement, reason) for one PEP 508 string -- reason is "" when it is
    satisfied, else a short human-readable why ("not installed", "have 1.4")."""
    from importlib.metadata import PackageNotFoundError, version as dist_version
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        req = Requirement(str(raw))
    except InvalidRequirement:
        return str(raw), "unparseable"
    try:
        have = dist_version(req.name)
    except PackageNotFoundError:
        return str(raw), "not installed"
    if req.specifier and not req.specifier.contains(have, prereleases=True):
        return str(raw), f"have {have}"
    return str(raw), ""


def missing_requirements(requirements) -> list:
    """The subset of `requirements` that isn't satisfied, as
    ``[{"requirement": "discord.py>=2.3", "reason": "not installed"}, ...]``.

    Empty list = the module can be registered. Note this is re-evaluated after
    an install (see :func:`install`), which is why it reads the live
    importlib.metadata state rather than caching anything.
    """
    out = []
    for raw in tuple(requirements or ()):
        requirement, reason = _requirement_status(raw)
        if reason:
            out.append({"requirement": requirement, "reason": reason})
    return out


def format_missing(missing) -> str:
    """The one-line reason string the Modulmanager card / the log shows."""
    return "missing dependency: " + ", ".join(
        f"{m['requirement']} ({m['reason']})" for m in missing
    )


def pip_available() -> tuple:
    """(ok, reason). False in a PyInstaller build, where there is no pip and no
    interpreter to run it with -- the admin gets told that instead of watching a
    button do nothing."""
    if getattr(sys, "frozen", False):
        return False, ("This is a packaged build with no Python environment to install "
                       "into. Install the dependency yourself, or use the Docker/pip "
                       "install of MediaForge.")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return False, f"pip is not runnable here: {exc}"
    if proc.returncode != 0:
        return False, "pip is not available in this Python environment"
    return True, ""


def install(requirements) -> dict:
    """pip-install `requirements` into the shared module dependency directory.

    Returns ``{"ok": bool, "installed": [...], "still_missing": [...],
    "error": str, "output": str}``. Never raises: a failed install is a result
    the Modulmanager shows, not an exception that takes a page down with it.

    ``--no-input`` and an explicit ``--target`` are the whole of the policy:
    nothing is installed into MediaForge's own environment, so a module can
    never upgrade a package the core depends on out from under it. Requirements
    are passed as separate argv entries (never through a shell), so a
    requirement string can't smuggle in a second command.
    """
    requirements = [str(r) for r in (requirements or ()) if str(r).strip()]
    if not requirements:
        return {"ok": True, "installed": [], "still_missing": [], "error": "", "output": ""}

    ok, reason = pip_available()
    if not ok:
        return {"ok": False, "installed": [], "error": reason, "output": "",
                "still_missing": missing_requirements(requirements)}

    target = deps_dir()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-input",
        "--disable-pip-version-check",
        "--target", str(target),
        # Without this pip refuses to touch a package already present in the
        # target dir, so an upgrade of a module dependency would silently do
        # nothing. It only ever affects THIS directory -- the core's own
        # site-packages is not a --target and is never written to.
        "--upgrade",
        *requirements,
    ]
    logger.info("[ModuleDeps] Installing %s into %s", ", ".join(requirements), target)

    with _install_lock:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_PIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "installed": [], "error": f"pip timed out after {_PIP_TIMEOUT}s",
                    "output": "", "still_missing": missing_requirements(requirements)}
        except Exception as exc:
            logger.exception("[ModuleDeps] pip run failed")
            return {"ok": False, "installed": [], "error": str(exc), "output": "",
                    "still_missing": missing_requirements(requirements)}

        # A freshly installed distribution is invisible to importlib.metadata
        # until the path caches are dropped -- without this, the very next
        # missing_requirements() call would still report what we just installed
        # as missing, and the module would stay blocked until a restart.
        ensure_on_sys_path()
        import importlib
        importlib.invalidate_caches()

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    still_missing = missing_requirements(requirements)

    if proc.returncode != 0:
        logger.warning("[ModuleDeps] pip failed (exit %s): %s", proc.returncode, output[-2000:])
        return {"ok": False, "installed": [], "error": f"pip failed (exit {proc.returncode})",
                "output": output[-4000:], "still_missing": still_missing}

    if still_missing:
        # pip said it worked but the requirement still isn't satisfied -- almost
        # always a version the resolver couldn't reach, or a package that shadows
        # a core one (see the sys.path.append policy above: the core's copy wins,
        # so installing a newer version here changes nothing).
        return {
            "ok": False,
            "installed": [],
            "error": format_missing(still_missing) + " — still unsatisfied after install",
            "output": output[-4000:],
            "still_missing": still_missing,
        }

    logger.info("[ModuleDeps] Installed %s", ", ".join(requirements))
    return {"ok": True, "installed": requirements, "still_missing": [],
            "error": "", "output": output[-4000:]}
