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

import shutil
import subprocess
import sys
import threading
from pathlib import Path

from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

logger = get_logger(__name__)

# Where module dependencies are installed: ~/.mediaforge/thirdparty-deps/ — beside the
# database and the thirdparties/ folder, never inside a module folder. See this file's
# docstring for why that distinction is the whole point.
#
# The name says who they belong to. These are the dependencies of *third-party* modules, and
# keeping them in a directory whose name says so means nobody ever has to guess whether it is
# safe to delete (it is: worst case, the Install button gets clicked again).
MODULE_DEPS_DIR = Path(MEDIAFORGE_CONFIG_DIR) / "thirdparty-deps"

# The directory this used to be. Left here purely to move anything already installed under
# the old name across on first start — an admin who clicked Install last week should not have
# to click it again because we renamed a folder.
_LEGACY_DEPS_DIR = Path(MEDIAFORGE_CONFIG_DIR) / "module_deps"

# One install at a time. pip is not safe to run concurrently against the same
# --target directory (two installs can interleave their file writes), and two
# admins clicking Install at once is not an exotic scenario in a shared install.
_install_lock = threading.Lock()

# How long a single pip run may take before it is killed. Generous: a cold
# install of something like discord.py pulls half a dozen wheels, and a slow
# NAS on a slow line is a normal MediaForge host.
_PIP_TIMEOUT = 600


def deps_dir() -> Path:
    """The module dependency directory, created if missing.

    Also performs the one-time move from the old ``module_deps/`` name. Renaming a directory
    an admin has already populated and saying nothing would silently un-install every
    dependency they installed — which looks, from the Modulmanager, exactly like the feature
    breaking.
    """
    try:
        MODULE_DEPS_DIR.mkdir(parents=True, exist_ok=True)

        if _LEGACY_DEPS_DIR.is_dir() and _LEGACY_DEPS_DIR != MODULE_DEPS_DIR:
            moved = 0
            for entry in _LEGACY_DEPS_DIR.iterdir():
                target = MODULE_DEPS_DIR / entry.name
                if target.exists():
                    continue          # the new location already has it; leave both alone
                try:
                    shutil.move(str(entry), str(target))
                    moved += 1
                except Exception:
                    logger.exception("[ModuleDeps] Could not move %s to the new deps dir", entry)
            if moved:
                logger.info("[ModuleDeps] Moved %d entr%s from %s to %s",
                            moved, "y" if moved == 1 else "ies",
                            _LEGACY_DEPS_DIR, MODULE_DEPS_DIR)
            try:
                if not any(_LEGACY_DEPS_DIR.iterdir()):
                    _LEGACY_DEPS_DIR.rmdir()
            except Exception:
                pass
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


def _reject_unsafe(requirements) -> list:
    """Requirement strings that must never reach pip's argv, with the reason for each.

    A dependency is a *name* and a *version range*. Everything else a PEP 508 string can
    express — a URL to fetch the package from, a local path, or (through argv) a pip option —
    is an instruction about where code comes from, and module metadata does not get to give
    those instructions. The admin clicking Install is consenting to "get discord.py from
    PyPI", not to "run whatever this module's author wants run".
    """
    from packaging.requirements import InvalidRequirement, Requirement

    problems = []
    for raw in requirements:
        text = str(raw).strip()

        # pip reads argv. A string starting with a dash is not a dependency, it is a flag —
        # --index-url, --extra-index-url, --find-links, --pre, take your pick.
        if text.startswith("-"):
            problems.append(f"{text!r} looks like a pip option, not a package")
            continue

        try:
            req = Requirement(text)
        except InvalidRequirement as exc:
            problems.append(f"{text!r} is not a valid requirement ({exc})")
            continue

        # `pkg @ https://…` / `pkg @ file:///…`: pip fetches and builds from there, which
        # means running arbitrary code from an arbitrary host at install time.
        if req.url:
            problems.append(f"{text!r} points at {req.url} — only PyPI packages are installed")

    return problems


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

    # Every string is checked before it becomes an argv entry, and a rejected one stops the
    # whole install.
    #
    # Passing them straight through was safe from *shell* injection (no shell is involved) and
    # wide open to something better: pip reads argv, and argv is where pip's options live. A
    # module declaring MODULE_REQUIREMENTS = ("--index-url=http://mine/", "innocent-looking")
    # would have MediaForge fetch its dependencies from the author's own package index — with
    # an admin clicking a button labelled "Install dependencies", which is exactly the amount
    # of consent an attacker needs and no more.
    #
    # So: it must parse as PEP 508, it must not carry a direct reference (`pkg @ https://…`,
    # which pip would happily download and run setup.py from), and it must not start with a
    # dash. A dependency is a name and a version range. Anything else is an instruction, and
    # module metadata does not get to issue instructions.
    rejected = _reject_unsafe(requirements)
    if rejected:
        return {"ok": False, "installed": [], "still_missing": requirements, "output": "",
                "error": "refused to install: " + "; ".join(rejected)}

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
