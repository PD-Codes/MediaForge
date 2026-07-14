"""Auto-discovery for third-party integrations.

Every subfolder here that exposes a ``register(app)`` callable in its
``__init__.py`` is imported and registered automatically by
:func:`discover_and_register`, called once from app.py's ``create_app()``.

Adding a new integration means creating a new subfolder — nothing in
app.py, base.html, integrations.html or notifications.html needs to
change. See ``web/thirdparties/anime_seasons/`` for a full worked example
(its own Blueprint with its own templates/static, its own service module,
its own translations/ catalog, and one ``register_thirdparty(...)`` call
into ``registry.py`` for the sidebar entry + settings card), and
``web/thirdparties/registry.py`` for the shared sidebar/settings-card hook
every integration plugs into — including *where* it plugs in: which
sidebar category (``section=``) and which existing (or brand-new)
settings tab/pill (``settings_host=`` / ``settings_tab=``), see
``register_thirdparty``'s docstring for the full parameter list. A
from-scratch, heavily-commented template lives outside the installed
package at ``.examples/thirdparties/`` in the repo root.

Translations are modular too: :func:`discover_translation_dirs` is a plain
filesystem scan (no imports) so it can run very early — before Flask-Babel
is initialized in app.py — and feed ``BABEL_TRANSLATION_DIRECTORIES``. Any
subfolder with its own ``translations/<locale>/LC_MESSAGES/messages.mo``
gets merged into the app's translation catalog automatically; an
integration that adds no new strings simply has no translations/ folder.

Integrations can also depend on each other: a subfolder's ``__init__.py``
may define a module-level ``DEPENDS_ON = ("other_folder_name",)`` tuple
(plain folder names, same as this package's subfolder names). See
:func:`discover_and_register` for what that guarantees.

A subfolder can also declare plain module-level constants describing
itself for the admin Modulmanager page (``/extensions``):

    MODULE_NAME = "My Integration"
    MODULE_DESCRIPTION = "What it does, in one sentence."
    MODULE_DESCRIPTION_DE = "Was es macht, in einem Satz."  # optional
    MODULE_DESCRIPTION_EN = "What it does, in one sentence."  # optional
    MODULE_AUTHOR = "Your Name"
    MODULE_ENABLED_DEFAULT = False

    MODULE_VERSION = "1.0.0"              # this module's own version
    MODULE_API_VERSION = 1                # registry contract it was written for
    MODULE_MIN_APP_VERSION = "1.1.0"      # optional compatibility range
    MODULE_MAX_APP_VERSION = ""           # optional; "" = no upper bound
    MODULE_REQUIREMENTS = ("icalendar>=6.0",)   # pip deps, checked not installed
    MODULE_ID = "my_integration"          # stable id for the module store
    MODULE_HOMEPAGE = "https://example.com/my-integration"
    MODULE_LICENSE = "MIT"

    MODULE_SENSITIVE_SETTINGS = (         # settings stored encrypted at rest
        registry.module_setting_key("my_integration", "refresh_token"),
    )

All are optional and read with ``getattr(module, ..., <fallback>)`` --
``MODULE_NAME`` falls back to the folder name, ``MODULE_DESCRIPTION``/
``MODULE_DESCRIPTION_DE``/``MODULE_DESCRIPTION_EN``/``MODULE_AUTHOR`` to
``""``, ``MODULE_ENABLED_DEFAULT`` to ``False``, ``MODULE_VERSION`` to
``"0.0.0"``, ``MODULE_ID`` to the folder name, and the rest to ``""``, so a
module that skips them entirely (or only declares some) keeps working
exactly as before.

``MODULE_VERSION`` is displayed as a badge next to the module's name on the
Modulmanager page, drives the on_install/on_upgrade hooks below, and is what
the module store compares an installed module against to decide whether it's
out of date -- so bump it on every change you ship.

``MODULE_API_VERSION`` is the version of *this contract*
(``registry.py``'s ``REGISTRY_API_VERSION``) the module was written against
-- see :func:`registry.check_api_compatibility`. It is the number a module
should really be pinning: MediaForge's own version can move for reasons that
have nothing to do with modules, while this one only ever changes when the
module contract itself breaks. A module asking for a *newer* API than the
running MediaForge provides is skipped; an older one keeps working.

``MODULE_MIN_APP_VERSION`` / ``MODULE_MAX_APP_VERSION`` declare which
MediaForge versions the module works on (inclusive bounds, either or both
omittable), checked against the running app's own version by
:func:`registry.check_app_compatibility` before ``register(app)`` is called.
A module the running MediaForge falls outside the range of is skipped with
that reason shown on the Modulmanager page, exactly like an unmet
``DEPENDS_ON`` -- better than letting it register and fail in some less
obvious way against an API it wasn't written for.

``MODULE_REQUIREMENTS`` is a tuple of PEP 508 requirement strings naming pip
distributions the module needs. MediaForge *checks* them before
``register(app)`` and skips the module with "missing dependency: ..." if one
isn't installed or is too old -- it never installs anything itself (see
:func:`_check_requirements` for why).

``MODULE_ID`` / ``MODULE_HOMEPAGE`` / ``MODULE_LICENSE`` are for the module
store and are not used for anything at runtime (the folder name is still
what identifies a module to discovery, ``DEPENDS_ON``, and everything else
here). ``MODULE_ID`` exists so a module keeps a stable store identity even
if its folder gets renamed on disk; the other two are shown on the
Modulmanager card. ``MODULE_ID`` is also the namespace every setting the
module owns should live under (:func:`registry.module_setting_key`), which
is what makes a clean uninstall possible -- an un-namespaced key can't be
told apart from a core one and is deliberately left behind.

``MODULE_SENSITIVE_SETTINGS`` names app_settings keys whose values must be
stored encrypted rather than as plaintext -- a bot token, an API key, an OAuth
refresh token. MediaForge registers them with the settings layer before
``register(app)`` runs (:func:`_register_sensitive_settings` →
:func:`db.register_sensitive_keys`), which encrypts whatever is already stored
in plaintext and makes every later ``set_setting()`` write ciphertext, using
the same encryption as the core's own secrets (``db.SENSITIVE_KEYS``). The
module keeps calling plain ``get_setting()``/``set_setting()`` -- decryption is
transparent.

Only needed for a secret the module manages *itself*. A secret the user types
into the module's settings card should simply be declared as an
``extra_settings`` field of type ``"secret"``: ``register_thirdparty()``
registers those as sensitive automatically, and the generic settings API never
sends their value back to the browser (see ``registry.SECRET_MASK``).

Where a module may write
------------------------
Nowhere in its own folder. That folder is what the module's signature is
computed over (``signing.content_hash``) and what the store deletes on every
upgrade -- a cache, a log or a vendored package written into it means the module
invalidates its own signature by running and loses the data on the next update.

The one writable place is :func:`registry.module_data_dir`::

    from ..registry import module_data_dir

    path = module_data_dir(MODULE_ID) / "cache.json"   # ~/.mediaforge/module_data/<id>/

It survives upgrades (it is the module's data, not its code) and is removed
only on uninstall. ``_vendor``/``_data`` are additionally excluded from the
signature hash as a safety net, but that is a net -- not a place to put things.

Python dependencies
-------------------
Declare them in ``MODULE_REQUIREMENTS`` and stop there. A module needing a
package MediaForge doesn't ship is no longer a dead end: the Modulmanager shows
it as "needs a dependency" with an Install button, which installs into
``~/.mediaforge/module_deps/`` and registers the module live (see ``deps.py``).
Never pip-install from inside a module, and never put a directory of your own on
``sys.path`` -- the core appends that directory, so its own versions of aiohttp,
niquests, packaging and friends always win an import, and a module that jumps
the queue breaks the app it lives in.

Lifecycle hooks
---------------
Besides ``register(app)`` (the only required one), a module may define any of
five optional module-level functions -- see :func:`fire_module_hook`:

    def on_install(app): ...                            # first ever start
    def on_upgrade(app, from_version, to_version): ...  # MODULE_VERSION changed
    def on_enable(app): ...                             # master toggle switched on
    def on_disable(app): ...                            # master toggle switched off
    def on_settings_changed(app, keys): ...             # a module:<id>:* setting was saved

``on_install``/``on_upgrade`` are driven by comparing ``MODULE_VERSION``
against the version last recorded for this install
(:func:`registry.installed_version`), so a module gets a real migration point
without hand-rolling its own schema-version tracking -- see
:func:`_run_lifecycle_hooks`. ``on_enable``/``on_disable`` fire on the *edge*
only (registry.py's generic settings route), so they can be treated as
start/stop rather than "re-check on every save".

``on_settings_changed(app, keys)`` fires whenever a setting the module owns
(``module:<MODULE_ID>:*``) is written -- from the module's settings card, from
its own routes, from anywhere. It replaces the config-polling loop every module
with a worker used to grow ("re-read the settings every 20 seconds and restart
myself if they changed"), which is where their deadlocks lived. It runs on a
thread of its own, never on the request that saved the setting.

Background workers
------------------
A module with a bot, a poller or a sync loop hands MediaForge its start/stop
instead of building a thread, a lock and a restart path by hand::

    def register(app):
        register_thirdparty(item_id="my_bot", ...)
        register_background_worker("my_bot", start=_start, stop=_stop)

MediaForge starts it when the module is enabled, stops it when it is disabled or
uninstalled, restarts it when a setting the module owns changes, and stops it on
shutdown -- one lock per worker, held only around that worker's own start/stop.
See :func:`registry.register_background_worker`.

Installing, updating, uninstalling
----------------------------------
Dropping a folder into ``web/thirdparties/`` is still all it takes to install
a module by hand. The store client (``store.py``) instead stages downloads
into ``_pending/`` and uninstalls into ``_pending/_remove.txt``, which
:func:`apply_pending_changes` applies at the next start, before anything has
been imported or registered -- because Flask can add a Blueprint to a running
app (that's what :func:`rescan_new_modules` exploits) but cannot remove or
replace one. See that function's docstring.

``MODULE_DESCRIPTION_DE``/``MODULE_DESCRIPTION_EN`` are optional overrides
of ``MODULE_DESCRIPTION`` for one specific UI language -- the Modulmanager
page picks whichever matches the admin's current language at render time
(:func:`registry._localized_module_description`), falling back to plain
``MODULE_DESCRIPTION`` when the current language has no override declared.
A module that only sets ``MODULE_DESCRIPTION`` shows that same text in
every language, exactly as before; this is purely additive.

``MODULE_ENABLED_DEFAULT = True`` only ever takes effect once, the very
first time each of the module's registered items is seen -- see
:func:`registry.seed_default_enabled` -- it never overrides a value a user
(or a previous run) already set.
"""

import ast
import atexit
import importlib
import pkgutil
import shutil
import threading
from pathlib import Path

from . import deps as module_deps
from .registry import (
    register_generic_settings_routes, record_module_status, item_ids, seed_default_enabled,
    known_module_names, registered_module_names, check_app_compatibility,
    check_api_compatibility, installed_version, record_installed_version,
    purge_module_settings, purge_module_data, get_thirdparty, module_entry,
    unregister_module, module_entries as registry_module_entries,
    start_workers, stop_workers, sync_workers,
)
from .signing import verify_module
from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Where modules live: NOT in here.
#
# This package (web/thirdparties/) is core code — registry.py, store.py,
# signing.py, trusted_keys.py — and it ships inside MediaForge, wherever that
# happens to be installed: a virtualenv's site-packages, a PyInstaller bundle, a
# git checkout. Installed modules have no business in any of those. They are the
# user's data, like image_cache/ and the database, and they belong where the rest
# of the user's data already is:
#
#     ~/.mediaforge/thirdparties/<module>/
#     ~/.mediaforge/thirdparties/_pending/     (staged installs, see below)
#
# Which means: a module survives reinstalling MediaForge, a developer can drop a
# folder in there without touching the source tree, and pip never has an opinion
# about it.
#
# The trick that makes this cost nothing: the modules stay *members of this
# package*. Appending the data directory to __path__ means
# `mediaforge.web.thirdparties.mediacalendar` is found there, while the module's
# own `from ..registry import register_thirdparty` and `from ....logger import
# get_logger` keep resolving exactly as before — because its package is still
# this one. Not a single existing module needs a line changed.
#
# Core stays FIRST in __path__ on purpose: a folder in the data directory called
# "registry" or "store" must never be able to shadow the real thing. Discovery
# refuses those names outright (RESERVED_NAMES) rather than relying on the
# ordering alone, but defence in depth is cheap here.
# ---------------------------------------------------------------------------
MODULES_DIR = Path(MEDIAFORGE_CONFIG_DIR) / "thirdparties"

# Names a module folder may not have: they are this package's own submodules, and
# a module allowed to take one would either shadow core code or be shadowed by it.
# Both outcomes are silent and awful, so an install under one of these names is
# refused (see store.py) and a folder already sitting there is skipped, loudly.
RESERVED_NAMES = frozenset({"registry", "store", "signing", "trusted_keys", "deps"})

# Dependencies modules declare in MODULE_REQUIREMENTS are installed into
# ~/.mediaforge/module_deps/ and made importable from there -- appended to
# sys.path, never prepended, so the core's own versions always win. Done at
# import of this package, before any module is imported, so a dependency
# installed in an earlier run simply exists as far as the module is concerned.
# See deps.py.
module_deps.ensure_on_sys_path()


def modules_dir() -> Path:
    """The directory installed modules live in, created if missing.

    Every path in this file goes through here rather than Path(__file__).parent —
    that was the old home, and it was the wrong one.
    """
    try:
        MODULES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[Thirdparties] Could not create %s", MODULES_DIR)
    return MODULES_DIR


# Make it part of this package's search path. Import time, before any submodule is
# imported, or an already-imported package would keep the old path.
if str(MODULES_DIR) not in __path__:
    __path__.append(str(MODULES_DIR))


def discovered_module_names(package_dir=None) -> list:
    """Folder names in the modules directory that are candidates for import.

    The one place that decides what counts as a module folder, so every scan in
    this file agrees. Skips ``_pending``/``_remove.txt`` (anything starting with an
    underscore) and refuses RESERVED_NAMES loudly — a module folder called
    "registry" would be a genuinely baffling thing to debug, so it gets a log line
    rather than a silent skip.

    Note what is NOT scanned: this package's own directory in the source tree. A
    module folder dropped in there is ignored by design — modules live in the data
    directory, one place, whether they were installed by the store or copied in by
    hand.
    """
    package_dir = Path(package_dir or modules_dir())
    names = []
    for _finder, name, is_pkg in pkgutil.iter_modules([str(package_dir)]):
        if not is_pkg or name.startswith("_"):
            continue
        if name in RESERVED_NAMES:
            logger.error(
                "[Thirdparties] Ignoring '%s': that name belongs to MediaForge's own "
                "module system (%s). Rename the folder.",
                name, ", ".join(sorted(RESERVED_NAMES)))
            continue
        names.append(name)
    return sorted(names)

# Successfully imported module objects, keyed by folder name -- kept so the
# lifecycle hooks (on_enable/on_disable, fired from registry.py's generic
# settings route long after discovery) can be looked up by name. Only ever
# holds modules that imported cleanly; a folder that failed to import has
# nothing to call a hook on.
_LOADED: dict = {}

# Folder name of the staging area the module store downloads into -- see
# apply_pending_changes(). Leading underscore so every scan in this file
# (which all skip names starting with "_") ignores it as a module folder.
PENDING_DIR = "_pending"

# One folder name per line: modules to remove on the next start. Written by the
# store client's uninstall (see store.py), applied by apply_pending_changes().
REMOVE_MANIFEST = "_remove.txt"

# Module ids whose folders apply_pending_changes() removed on this start, so
# discover_and_register() can purge their settings once the DB is available --
# the filesystem work happens before Flask (and therefore the DB) is up, so
# these two halves of "uninstall" can't be done in the same place.
_PENDING_SETTING_PURGES: list = []

# Blueprint names of modules uninstalled LIVE in this process (see
# uninstall_module_live()). Flask can add a blueprint to a running app but never
# remove one, so the routes of an uninstalled module stay in the URL map until
# the process restarts -- pointing at a package whose files are gone. app.py
# installs one before_request guard that 404s anything belonging to a blueprint
# in here, which turns "500, TemplateNotFound" into a plain "not found".
_UNINSTALLED_BLUEPRINTS: set = set()


def uninstalled_blueprints() -> set:
    """Blueprint names whose module was uninstalled live -- their leftover
    routes must 404. Read by app.py's guard on every request."""
    return set(_UNINSTALLED_BLUEPRINTS)


def apply_pending_changes() -> dict:
    """Apply anything the module store staged for the next start: install or
    upgrade every folder sitting in ``_pending/``, and remove every folder
    named in ``_pending/_remove.txt``. Returns
    ``{"installed": [...], "removed": [...], "failed": [...]}``.

    Must be called *before* anything reads web/thirdparties/ -- i.e. before
    discover_translation_dirs() in app.py's create_app(), which is the first
    thing to touch these folders and (worse) feeds Flask-Babel's
    BABEL_TRANSLATION_DIRECTORIES, which is read once at init_app() and can't
    be changed afterwards. A module installed after that point would have its
    routes but not its translations.

    Doing it here, at startup, rather than live at download time, is the whole
    design: Flask can register a Blueprint on a running app (rescan_new_modules()
    does exactly that), but it has no supported way to *un*register one, replace
    one, or re-run an already-imported module's top-level code. An upgrade and
    an uninstall are therefore both "swap the folder while nothing is looking",
    which on a running process only exists between "process started" and "first
    request" -- exactly here. The store client downloads into _pending/ and
    tells the admin a restart is needed; this is the other half of that.
    """
    package_dir = modules_dir()
    pending_dir = package_dir / PENDING_DIR
    result = {"installed": [], "removed": [], "failed": []}
    if not pending_dir.is_dir():
        return result

    # ---- removals first: uninstall-then-reinstall of the same name in one
    # go must not delete the folder that was just installed.
    manifest = pending_dir / REMOVE_MANIFEST
    if manifest.is_file():
        try:
            names = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()]
        except Exception as exc:
            logger.exception("[Thirdparties] Could not read %s", manifest)
            names = []
            result["failed"].append(f"{REMOVE_MANIFEST}: {exc}")
        for name in names:
            if not name or name.startswith("_") or "/" in name or "\\" in name or name == "..":
                continue
            target = package_dir / name
            module_id = _module_id_on_disk(target)
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                    logger.info("[Thirdparties] Removed module folder '%s'", name)
                result["removed"].append(name)
                # The settings can only be purged once the DB is up -- see
                # _PENDING_SETTING_PURGES / discover_and_register().
                _PENDING_SETTING_PURGES.append(module_id or name)
            except Exception as exc:
                logger.exception("[Thirdparties] Failed to remove '%s'", name)
                result["failed"].append(f"{name}: {exc}")
        try:
            manifest.unlink()
        except Exception:
            logger.exception("[Thirdparties] Could not clear %s", manifest)

    # ---- then installs/upgrades: each staged folder replaces the live one.
    for staged in sorted(pending_dir.iterdir()):
        if not staged.is_dir() or staged.name.startswith("_"):
            continue
        target = package_dir / staged.name
        try:
            if not (staged / "__init__.py").is_file():
                raise ValueError("staged folder has no __init__.py")
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staged), str(target))
            logger.info("[Thirdparties] Installed/updated module folder '%s'", staged.name)
            result["installed"].append(staged.name)
        except Exception as exc:
            logger.exception("[Thirdparties] Failed to apply staged module '%s'", staged.name)
            result["failed"].append(f"{staged.name}: {exc}")

    return result


def _module_id_on_disk(folder: Path):
    """Best-effort MODULE_ID of a module folder we're about to delete, read
    straight out of its __init__.py *without importing it* -- importing a
    module purely to find out how to erase it would run its top-level code
    (and, for a module being removed because it's broken, quite possibly
    fail). Returns None if the folder is gone or declares no MODULE_ID, in
    which case the caller falls back to the folder name -- which is what
    MODULE_ID defaults to anyway.
    """
    init = folder / "__init__.py"
    if not init.is_file():
        return None
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except Exception:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "MODULE_ID":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    return None


def stage_removal(name: str) -> None:
    """Queue thirdparties/<name>/ for removal on the next start by appending it
    to ``_pending/_remove.txt`` -- the store client's uninstall (see store.py's
    uninstall()). Deliberately does not touch the live folder: the module is
    imported and its Blueprint is registered on the running app, and pulling
    its files out from under it mid-request is a good way to produce a very
    confusing traceback.
    """
    package_dir = modules_dir()
    pending_dir = package_dir / PENDING_DIR
    pending_dir.mkdir(exist_ok=True)
    manifest = pending_dir / REMOVE_MANIFEST
    existing = []
    if manifest.is_file():
        existing = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    if name not in existing:
        existing.append(name)
    manifest.write_text("\n".join(existing) + "\n", encoding="utf-8")


def pending_changes() -> dict:
    """What's currently staged and waiting for a restart:
    ``{"install": [folder names], "remove": [folder names]}``. Powers the
    Modulmanager's "restart required" banner -- and, since it's a plain
    filesystem read, it stays correct across an admin refreshing the page,
    a second admin looking at it, or the process having been restarted since
    (in which case _pending/ is empty again and the banner disappears on its
    own).
    """
    package_dir = modules_dir()
    pending_dir = package_dir / PENDING_DIR
    out = {"install": [], "remove": []}
    if not pending_dir.is_dir():
        return out
    out["install"] = sorted(
        entry.name for entry in pending_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    )
    manifest = pending_dir / REMOVE_MANIFEST
    if manifest.is_file():
        out["remove"] = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
    return out


def _check_requirements(module) -> str:
    """Return "" if every distribution in the module's ``MODULE_REQUIREMENTS``
    (a tuple of PEP 508 requirement strings, e.g. ``("discord.py>=2.3",)``) is
    installed and satisfies its version specifier, or a human-readable reason
    if not.

    MediaForge doesn't install anything *implicitly* -- a module must not be
    able to pull code from PyPI onto an admin's machine merely by being
    discovered. But an admin can now say yes: a module blocked here is recorded
    with its missing requirements (see record_module_status(...,
    missing_requirements=...)), the Modulmanager shows it as "needs a
    dependency" with an Install button, and that button runs
    deps.install() -> install_module_requirements(), which registers the module
    live once the install succeeds. See deps.py for where the packages go and
    why they are appended to sys.path rather than prepended.

    The reason string still names exactly what is missing and why
    ("discord.py>=2.3 (not installed)"), so the Modulmanager card is readable
    even in the builds where installing isn't possible (PyInstaller).
    """
    requirements = tuple(getattr(module, "MODULE_REQUIREMENTS", None) or ())
    if not requirements:
        return ""
    missing = module_deps.missing_requirements(requirements)
    if not missing:
        return ""
    return module_deps.format_missing(missing)


def fire_module_hook(name, hook, *args):
    """Call an optional lifecycle hook on a loaded module, swallowing (but
    logging) whatever it raises.

    The four hooks, all optional, all plain module-level functions in a
    thirdparty's ``__init__.py``:

    - ``on_install(app)`` -- the very first start after this module appeared on
      this install (nothing recorded in registry.installed_version()). Create
      tables, seed defaults.
    - ``on_upgrade(app, from_version, to_version)`` -- the first start after
      MODULE_VERSION changed from what was last recorded. Migrate.
    - ``on_enable(app)`` / ``on_disable(app)`` -- the admin flipped the
      module's master toggle (fired on the *edge* only, see registry.py's
      generic PUT route). Start/stop workers, clear caches.
    - ``on_settings_changed(app, keys)`` -- a setting the module owns
      (``module:<MODULE_ID>:*``) was written. Reconnect a client, reload a
      config. Fired on a thread of its own, never on the request that saved it
      (see _on_setting_changed()). A module that registered a background worker
      gets it restarted automatically as well and usually needs no hook at all.

    A hook raising must never take the app down with it -- a broken on_disable
    would otherwise make a module impossible to switch off, which is precisely
    when you most want to switch it off. The failure is logged and recorded on
    the module's Modulmanager card instead.
    """
    module = _LOADED.get(name)
    if module is None:
        return
    fn = getattr(module, hook, None)
    if not callable(fn):
        return
    try:
        fn(*args)
        logger.info("[Thirdparties] %s(%s) ok", hook, name)
    except Exception as exc:
        logger.exception("[Thirdparties] %s() failed for '%s'", hook, name)
        record_module_status(name, error=f"{hook}() failed: {exc}")


def _run_lifecycle_hooks(app, name, module) -> None:
    """Fire on_install/on_upgrade for a module that just registered
    successfully, by comparing its MODULE_VERSION against the version last
    recorded as installed (registry.installed_version()).

    Three cases, and the version is only re-recorded after the matching hook
    ran, so a hook that blew up is retried on the next start rather than being
    silently skipped forever:
    - nothing recorded  -> on_install(app)          (new to this install)
    - recorded != code  -> on_upgrade(app, old, new) (also fires on a
      *downgrade*; a module that cares can compare the two strings itself)
    - recorded == code  -> nothing at all (the normal start)
    """
    module_id = getattr(module, "MODULE_ID", None) or name
    code_version = str(getattr(module, "MODULE_VERSION", "") or "")
    try:
        known = installed_version(module_id)
    except Exception:
        # No DB yet / DB error -- skip the hooks rather than guessing; the next
        # start tries again.
        logger.exception("[Thirdparties] Could not read installed version of '%s'", name)
        return

    if known is None:
        fire_module_hook(name, "on_install", app)
    elif known != code_version:
        fire_module_hook(name, "on_upgrade", app, known, code_version)
    else:
        return
    try:
        record_installed_version(module_id, code_version)
    except Exception:
        logger.exception("[Thirdparties] Could not record installed version of '%s'", name)


def discover_translation_dirs() -> list:
    """Return the ``translations/`` directory of every thirdparty subfolder
    that has one, as absolute paths ready to append to
    ``BABEL_TRANSLATION_DIRECTORIES``.

    Pure filesystem scan — no imports of the integrations themselves — so
    app.py can call this *before* ``Babel.init_app()``, which is when
    Flask-Babel reads that config and it's too late to change afterwards.
    """
    package_dir = modules_dir()
    dirs = []
    for entry in sorted(package_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        tdir = entry / "translations"
        if tdir.is_dir():
            dirs.append(str(tdir))
    return dirs


def _resolve_load_order(modules: dict) -> list:
    """Topologically sort successfully-imported modules by their
    ``DEPENDS_ON`` tuples, so a module that declares
    ``DEPENDS_ON = ("anime_seasons",)`` always has anime_seasons's
    register(app) attempted first — regardless of alphabetical folder
    order. Modules with no ordering constraint between them keep
    alphabetical order (stable sort), same as before DEPENDS_ON existed.

    A dependency naming a folder that either wasn't discovered or failed to
    import isn't in ``modules`` at all, so it's simply not a graph edge here
    — :func:`discover_and_register`'s own "unmet dependency" check (after
    this ordering, once it knows which register(app) calls actually
    succeeded) is what skips the dependent module in that case. This
    function only has to worry about genuine cycles among modules that were
    imported fine.
    """
    names = sorted(modules)
    deps = {name: [d for d in (getattr(modules[name], "DEPENDS_ON", None) or ()) if d in modules]
            for name in names}

    order = []
    visiting: set = set()
    visited: set = set()

    def visit(name, path):
        if name in visited:
            return
        if name in visiting:
            logger.warning(
                "[Thirdparties] Dependency cycle detected: %s -> %s — "
                "ignoring the cycle, load order among these is undefined.",
                " -> ".join(path), name)
            return
        visiting.add(name)
        for dep in deps[name]:
            visit(dep, path + [name])
        visiting.discard(name)
        visited.add(name)
        order.append(name)

    for name in names:
        visit(name, [])
    return order


def _import_folders(names: list) -> dict:
    """Import phase, shared by discover_and_register() (all folders) and
    rescan_new_modules() (just the new ones) -- runs each module's
    top-level code (including a ``DEPENDS_ON`` tuple, if it declares one)
    without calling register(app) yet, so dependency order doesn't matter
    for this part. An import failure is logged and that folder is left out
    of the returned dict entirely (:func:`_resolve_load_order` never sees
    it, so nothing can depend on it)."""
    package_dir = modules_dir()
    modules = {}
    for name in names:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            modules[name] = module
            _LOADED[name] = module
            # Signature check (see signing.py). Purely informational at load
            # time -- an unsigned module loads exactly like a signed one; what
            # the signature decides is what the module may *claim* (the trust
            # badge in the Modulmanager, and whether the store is willing to
            # install it without an explicit "I know this is unreviewed"). The
            # module's own MODULE_ID/MODULE_VERSION are passed in so a valid
            # signature for a *different* module can't be used to launder this
            # one.
            signature = verify_module(
                package_dir / name,
                module_id=getattr(module, "MODULE_ID", None) or name,
                version=str(getattr(module, "MODULE_VERSION", "") or ""),
            )
            record_module_status(
                name, imported=True,
                module_name=getattr(module, "MODULE_NAME", None),
                description=getattr(module, "MODULE_DESCRIPTION", None),
                description_de=getattr(module, "MODULE_DESCRIPTION_DE", None),
                description_en=getattr(module, "MODULE_DESCRIPTION_EN", None),
                author=getattr(module, "MODULE_AUTHOR", None),
                enabled_default=getattr(module, "MODULE_ENABLED_DEFAULT", None),
                # Version, compatibility range and module-store metadata --
                # see this package's docstring. record_module_status() ignores
                # a None (its "not passed" marker), so a module declaring none
                # of these keeps the defaults seeded there ("0.0.0" / "" /
                # folder name).
                version=getattr(module, "MODULE_VERSION", None),
                min_app_version=getattr(module, "MODULE_MIN_APP_VERSION", None),
                max_app_version=getattr(module, "MODULE_MAX_APP_VERSION", None),
                module_id=getattr(module, "MODULE_ID", None),
                homepage=getattr(module, "MODULE_HOMEPAGE", None),
                license=getattr(module, "MODULE_LICENSE", None),
                api_version=getattr(module, "MODULE_API_VERSION", None),
                requirements=tuple(getattr(module, "MODULE_REQUIREMENTS", None) or ()) or None,
                signature=signature,
            )
        except Exception as exc:
            logger.exception("[Thirdparties] Failed to import '%s'", name)
            record_module_status(name, imported=False, registered=False, error=str(exc))
    return modules


def _register_sensitive_settings(name, module) -> None:
    """Mark the app_settings keys a module declares in MODULE_SENSITIVE_SETTINGS
    as sensitive, so their values are stored encrypted (see
    db.register_sensitive_keys()).

    This is the escape hatch for a secret with no settings-card field of its
    own -- a token the module obtains itself (OAuth refresh token, session
    cookie, ...) and only ever writes from its own code. Secrets that *are*
    settings-card fields need nothing here: registry.register_thirdparty()
    registers every extra_settings entry of type "secret" automatically.

    The declared keys are full app_settings keys, so a module namespaces them
    the same way it does everywhere else:

        MODULE_SENSITIVE_SETTINGS = (
            registry.module_setting_key(MODULE_ID, "refresh_token"),
        )

    Failures are logged, never raised: an unencryptable value must not keep the
    module from loading -- that would be a strictly worse outcome than the
    plaintext storage every module had before this existed.
    """
    keys = tuple(getattr(module, "MODULE_SENSITIVE_SETTINGS", None) or ())
    if not keys:
        return
    try:
        from ..db import register_sensitive_keys

        migrated = register_sensitive_keys(keys)
        logger.info(
            "[Thirdparties] '%s' declared %d sensitive setting(s)%s",
            name, len(keys),
            f", encrypted {migrated} previously plaintext value(s)" if migrated else "",
        )
    except Exception:
        logger.warning(
            "[Thirdparties] Could not register sensitive settings of '%s'", name,
            exc_info=True)


def _register_modules(app, modules: dict, registered: set) -> list:
    """Call register(app) for each of `modules`, in an order that respects
    DEPENDS_ON (see _resolve_load_order) -- shared by
    discover_and_register() (start with an empty `registered` set) and
    rescan_new_modules() (seeded with registry.registered_module_names(),
    so a brand-new module can depend on one registered back at original
    startup). Before calling a given module's register(app), every folder
    name in its DEPENDS_ON must already be in `registered` -- otherwise
    it's skipped with a warning instead of risking a crash from a
    half-available dependency. `registered` is mutated in place as each
    one succeeds. Returns the folder names registered successfully in
    *this* call (rescan_new_modules() uses this for its own return value;
    discover_and_register() ignores it)."""
    newly_registered = []
    for name in _resolve_load_order(modules):
        module = modules[name]
        depends_on = tuple(getattr(module, "DEPENDS_ON", None) or ())
        record_module_status(name, depends_on=depends_on)

        # Three gates before register(app) -- the registry API contract, the
        # MediaForge version range, and the module's pip dependencies (see this
        # package's docstring for all three). All are checked here rather than
        # at import, because a module is allowed to *exist* on disk in a state
        # this app can't run: what must not happen is it registering routes,
        # settings cards and background jobs against a contract or environment
        # it wasn't written for. Each failure is treated exactly like an unmet
        # DEPENDS_ON: not registered, reason recorded for the Modulmanager
        # card, everything else keeps loading -- and the module stays out of
        # `registered`, so anything DEPENDS_ON-ing it is skipped too rather
        # than being handed a half-loaded dependency.
        # Recorded separately from the reason string, so the Modulmanager can
        # offer an Install button for exactly these requirements instead of the
        # admin having to parse the message. Cleared (to ()) when nothing is
        # missing, so a module that gets its dependency installed stops
        # advertising one on the next pass.
        missing = module_deps.missing_requirements(
            getattr(module, "MODULE_REQUIREMENTS", None) or ())
        record_module_status(name, missing_requirements=tuple(
            m["requirement"] for m in missing))

        blocked = (
            check_api_compatibility(getattr(module, "MODULE_API_VERSION", None))
            or check_app_compatibility(
                getattr(module, "MODULE_MIN_APP_VERSION", None),
                getattr(module, "MODULE_MAX_APP_VERSION", None),
            )
            or (module_deps.format_missing(missing) if missing else "")
        )
        if blocked:
            logger.warning("[Thirdparties] '%s' skipped — %s", name, blocked)
            record_module_status(name, registered=False, error=blocked)
            continue

        missing = [d for d in depends_on if d not in registered]
        if missing:
            msg = "unmet DEPENDS_ON: " + ", ".join(missing)
            logger.warning("[Thirdparties] '%s' skipped — %s", name, msg)
            record_module_status(name, registered=False, error=msg)
            continue

        # Secrets the module owns but doesn't expose as a "secret"
        # extra_settings field (register_thirdparty() registers those itself).
        # Done before register(app) so the module's own startup code already
        # reads and writes them through the encrypting path.
        _register_sensitive_settings(name, module)

        register_fn = getattr(module, "register", None)
        if not callable(register_fn):
            logger.warning("[Thirdparties] '%s' has no register(app) callable — skipped", name)
            record_module_status(name, registered=False, error="no register(app) callable")
            continue
        try:
            before_ids = item_ids()
            # Flask >=2.2 refuses register_blueprint()/app.route() once the
            # app has handled its first request (Scaffold.
            # _check_setup_finished(), gated on the private app.
            # _got_first_request flag) -- which is always the case by the
            # time rescan_new_modules() (this function's other caller) runs,
            # since it's only ever triggered by an admin clicking "Refresh"
            # on an already-running app. That flag has no purpose beyond
            # this one assertion in current Flask (it's not used for
            # before-first-request hooks anymore, and full_dispatch_request()
            # unconditionally sets it back to True on every request
            # regardless of what we do here), so it's safe to flip off just
            # for the duration of this one register(app) call -- without
            # this, a genuinely new module's Blueprint can never be added
            # live, defeating the entire point of "Refresh".
            had_first_request = getattr(app, "_got_first_request", False)
            app._got_first_request = False
            try:
                register_fn(app)
            finally:
                app._got_first_request = had_first_request
            logger.info("[Thirdparties] Registered integration: %s", name)
            registered.add(name)
            newly_registered.append(name)
            # Diff item_ids() before/after instead of asking the module to
            # report itself — one register(app) can call
            # register_thirdparty() zero, one, or (in principle) several
            # times, and the Extensions overview page wants to know exactly
            # which item(s) came out of it either way.
            new_ids = sorted(item_ids() - before_ids)
            record_module_status(name, registered=True, item_ids=new_ids)
            # Apply MODULE_ENABLED_DEFAULT (if declared) to whatever this
            # module just registered -- a no-op after the very first run,
            # see seed_default_enabled()'s docstring.
            seed_default_enabled(new_ids, getattr(module, "MODULE_ENABLED_DEFAULT", False))
            # ...and only now, once the module is actually registered and its
            # defaults are seeded, fire on_install/on_upgrade -- a hook that
            # creates tables or migrates data has no business running for a
            # module that turned out not to load.
            _run_lifecycle_hooks(app, name, module)
        except Exception as exc:
            logger.exception("[Thirdparties] register(app) failed for '%s'", name)
            record_module_status(name, registered=False, error=str(exc))

    if newly_registered:
        _secure_new_endpoints(app)
        # A module registered *live* (store install, dependency install,
        # Modulmanager Refresh) gets its background worker started here rather
        # than having to wait for a restart. At original startup this is the
        # same call discover_and_register() makes a moment later -- sync_workers()
        # is idempotent, a running worker is not started twice.
        for name in newly_registered:
            start_workers(app, module_name=name)
    return newly_registered


def _secure_new_endpoints(app) -> None:
    """Put the routes a module just added through app.py's auth pass.

    That pass (login_required / admin_required / CSRF exemption) runs once, at
    the end of create_app(). A module registered *live* -- a store install, a
    dependency install, the Modulmanager's Refresh -- adds its blueprint after
    that, so without this its routes would be reachable with no login check at
    all. At original startup this is a no-op: create_app() hasn't installed the
    hook yet, and its own pass a few lines later covers everything.

    In no-auth mode there is nothing to install and nothing to secure, so the
    hook simply isn't there.
    """
    try:
        fn = (getattr(app, "extensions", None) or {}).get("mediaforge_secure_endpoints")
        if fn:
            fn()
    except Exception:
        logger.exception("[Thirdparties] Could not secure newly registered endpoints")


def discover_and_register(app) -> None:
    """Import every subpackage of web/thirdparties/, then call each one's
    register(app) in dependency order, then wire up the shared
    settings-toggle API once. See _import_folders()/_register_modules()
    for the two phases -- kept as separate functions so
    rescan_new_modules() (the Modulmanager "Refresh" button) can reuse
    both without duplicating this logic.

    This function itself is only ever safe to call once, at original app
    startup (see app.py's create_app()) -- calling it a second time would
    re-run register(app) for every already-registered folder, which
    re-executes app.register_blueprint(...)/app.route(...) a second time
    and crashes (Flask raises on a duplicate endpoint the second time
    register_generic_settings_routes(app) runs its app.route(...) calls).
    Picking up a NEW folder added after startup without restarting the app
    is what rescan_new_modules() is for instead -- it never touches an
    already-registered folder.
    """
    names = discovered_module_names()
    modules = _import_folders(names)
    _register_modules(app, modules, set())

    # Second half of "uninstall": apply_pending_changes() deleted the folders
    # before Flask (and therefore the DB) existed, so the settings those
    # modules owned are purged here, at the first point where get_setting/
    # set_setting actually work. Only namespaced keys are removed -- see
    # registry.purge_module_settings().
    while _PENDING_SETTING_PURGES:
        module_id = _PENDING_SETTING_PURGES.pop()
        try:
            removed = purge_module_settings(module_id)
            purge_module_data(module_id)
            logger.info("[Thirdparties] Purged %d setting(s) of uninstalled module '%s'",
                        removed, module_id)
        except Exception:
            logger.exception("[Thirdparties] Could not purge settings of '%s'", module_id)

    # Shared enable/disable API for the simple "just a toggle" settings card
    # every registered thirdparty gets automatically — see registry.py.
    # Only ever called here, once -- see this function's own docstring.
    register_generic_settings_routes(app)

    # Settings changes reach modules as an event instead of each of them
    # polling get_setting() on a timer (which is what every module with a bot
    # or a worker ended up doing, badly). See _on_setting_changed().
    _install_settings_listener(app)

    # Start the background workers of every module that registered one and is
    # currently enabled -- and make sure they are stopped on the way out. See
    # registry.register_background_worker().
    start_workers(app)
    atexit.register(stop_workers)


# ---------------------------------------------------------------------------
# Settings change events
# ---------------------------------------------------------------------------

_APP = None
_LISTENER_INSTALLED = False


def _install_settings_listener(app) -> None:
    """Subscribe to db.set_setting() once, so a write to ``module:<id>:<key>``
    reaches the module that owns it.

    Registered here rather than in db.py because only this package knows which
    folder owns which module_id -- db.py just knows keys.
    """
    global _APP, _LISTENER_INSTALLED

    _APP = app
    if _LISTENER_INSTALLED:
        return
    from ..db import add_setting_listener

    add_setting_listener(_on_setting_changed)
    _LISTENER_INSTALLED = True


def _module_name_for_setting_key(key):
    """The thirdparties/<folder>/ whose MODULE_ID owns the namespaced setting
    key ``module:<module_id>:<name>``, or None for a core (or un-namespaced)
    key. Matches on MODULE_ID rather than folder name, because that is what
    registry.module_setting_key() namespaces with -- the two are the same for
    most modules, but not for one whose folder was renamed."""
    if not key or not key.startswith("module:"):
        return None
    parts = key.split(":", 2)
    if len(parts) < 3:
        return None
    module_id = parts[1]
    for name, mod in registry_module_entries().items():
        if (mod.get("module_id") or name) == module_id:
            return name
    return None


def _on_setting_changed(key, value) -> None:
    """db.set_setting() callback: fire ``on_settings_changed(app, keys)`` on the
    owning module and restart its background worker (if it registered one).

    Runs on a short-lived daemon thread, never on the thread that saved the
    setting: a module's handler restarts a bot, reconnects a client, rebuilds a
    cache -- seconds of work that have no business hanging the HTTP request the
    admin just made from the settings page. registry.sync_workers() serializes
    per module, so two saves in quick succession can't have a module's worker
    starting and stopping at the same time.

    Never raises into set_setting(): a module with a broken handler must not be
    able to make saving a setting fail.
    """
    try:
        name = _module_name_for_setting_key(key)
        if not name or _APP is None:
            return
        app = _APP

        def _run():
            try:
                fire_module_hook(name, "on_settings_changed", app, (key,))
            except Exception:
                logger.exception("[Thirdparties] on_settings_changed failed for '%s'", name)
            try:
                # Whatever the module did (or didn't do) in its own hook, the
                # worker contract is the core's: a setting the module owns
                # changed, so its worker is restarted against the new value --
                # and stopped, not restarted, if what changed was the master
                # toggle going off. See registry.sync_workers().
                sync_workers(app, module_name=name, restart=True)
            except Exception:
                logger.exception("[Thirdparties] worker sync failed for '%s'", name)

        threading.Thread(target=_run, daemon=True,
                         name=f"settings-change-{name}").start()
    except Exception:
        logger.exception("[Thirdparties] settings-change dispatch failed for %r", key)


# ---------------------------------------------------------------------------
# Dependency installation (the Modulmanager's "Install dependency" button)
# ---------------------------------------------------------------------------

def install_module_requirements(app, name) -> dict:
    """pip-install the MODULE_REQUIREMENTS of the module in folder `name`, then
    register it live if that's all that was blocking it.

    The server-side half of the Modulmanager's Install button (see
    routes/extensions.py's ``POST /api/extensions/install-deps``). Only ever
    installs what the module *declares* -- the requirement strings come from the
    imported module object, never from the request -- and only into
    ~/.mediaforge/module_deps/ (see deps.py).

    Returns ``{"ok", "error", "output", "installed", "registered", "restart_required"}``.
    ``registered`` says whether the module is now live; a module that still
    doesn't register after its dependency arrived (bad code, unmet DEPENDS_ON)
    keeps its reason on its card, and one whose *code was already imported* in a
    failed state needs a restart -- which is what restart_required reports.
    """
    module = _LOADED.get(name)
    if module is None:
        return {"ok": False, "error": f"module '{name}' is not loaded", "output": "",
                "installed": [], "registered": False, "restart_required": True}

    requirements = tuple(getattr(module, "MODULE_REQUIREMENTS", None) or ())
    if not requirements:
        return {"ok": False, "error": f"module '{name}' declares no MODULE_REQUIREMENTS",
                "output": "", "installed": [], "registered": False, "restart_required": False}

    result = module_deps.install(requirements)
    result.setdefault("installed", [])
    if not result.get("ok"):
        result["registered"] = False
        result["restart_required"] = False
        return result

    # Dependency is there -- try to bring the module up right now. Already
    # registered (e.g. the admin clicked twice) is a no-op, not an error.
    if name in registered_module_names():
        result["registered"] = True
        result["restart_required"] = False
        return result

    try:
        newly = _register_modules(app, {name: module}, set(registered_module_names()))
    except Exception as exc:
        logger.exception("[Thirdparties] register after dependency install failed for '%s'", name)
        result["registered"] = False
        result["restart_required"] = True
        result["error"] = str(exc)
        return result

    result["registered"] = name in newly
    # Flask can add a blueprint to a running app but not replace one. A module
    # that never got as far as register(app) (which is the case for every module
    # blocked on a dependency) has claimed nothing yet, so it comes up live --
    # anything else means something other than the dependency was wrong, and the
    # admin gets the reason on the card rather than a silent no-op.
    result["restart_required"] = not result["registered"]
    if result["registered"]:
        start_workers(app, module_name=name)
    return result


def rescan_new_modules(app) -> list:
    """Scan web/thirdparties/ for folders NOT already known (see
    registry.known_module_names()) and register just those, live, with no
    app restart -- the Modulmanager "Refresh" button's server-side half
    (routes/extensions.py's ``POST /api/extensions/rescan``).

    Deliberately narrower than re-running discover_and_register() wholesale
    (which isn't safe -- see that function's docstring): a folder that's
    already registered has already claimed its blueprint name, its routes,
    its settings card, etc., and Flask has no supported way to safely
    replace any of that on a live app. A brand-new, never-before-seen
    folder name has none of that baggage, so registering it here is exactly
    as safe as it was in the original startup pass -- the only difference
    is `registered` is seeded with every folder that's *already* registered
    (registry.registered_module_names()) rather than starting empty, so a
    new module's DEPENDS_ON on an existing one still resolves correctly.

    Note this only covers ADDING a genuinely new folder. It does not (and,
    on stock Flask, safely cannot) pick up code *changes* to an
    already-registered module, or fully remove one's routes -- both still
    need a restart; see mediacalendar's per-worker enabled-checks for the
    part of "disable a module live" that *is* fully supported (stopping
    its background effects), and this docstring for what isn't.

    Returns the list of folder names newly registered by this call (empty
    if nothing new was found, or everything new failed to import/register
    -- check the Extensions overview page for why).
    """
    package_dir = modules_dir()
    # Without this, a folder copied in after the process started can stay
    # invisible to pkgutil.iter_modules() indefinitely: Python's import
    # machinery caches each directory's listing (importlib.machinery.
    # FileFinder) and only a handful of code paths (an actual import
    # attempt, mainly) trigger a refresh -- iter_modules() alone doesn't.
    # See importlib.invalidate_caches()'s own docs: "If you are dynamically
    # importing a module that was created since the interpreter began
    # execution [...] you may need to call invalidate_caches()". This is
    # exactly that case, every time this is called.
    importlib.invalidate_caches()
    known = known_module_names()
    names = [name for name in discovered_module_names(package_dir) if name not in known]
    if not names:
        return []
    modules = _import_folders(names)
    if not modules:
        return []
    registered = registered_module_names()
    return _register_modules(app, modules, registered)


# ---------------------------------------------------------------------------
# Live install / live uninstall (Modulmanager, no restart)
# ---------------------------------------------------------------------------
# What CAN be done on a running Flask app:
#   * add a brand-new blueprint  -> rescan_new_modules() (see its docstring)
#   * add translation catalogs   -> _add_module_translations(), below
#   * drop registry entries      -> registry.unregister_module()
#   * fire on_disable(app)       -> fire_module_hook()
# What CANNOT:
#   * remove or replace a blueprint that is already registered
#
# So: a FIRST-TIME install of a folder nobody has imported yet goes live
# immediately. An UPGRADE of a folder that is already imported and registered
# stays staged for the next start -- its old module object, blueprint and routes
# are unreplaceable while the process runs, and half-swapping them is worse than
# waiting. An uninstall goes live in the sense that matters (the module is
# switched off, disappears from the UI, its settings are purged and its files are
# deleted); only its now-orphaned URL rules survive until the next restart, and
# those are 404'd by app.py's guard.


def _add_module_translations(app, name) -> None:
    """Merge a freshly installed module's translations/ into the live catalog.

    Flask-Babel reads BABEL_TRANSLATION_DIRECTORIES exactly once, at
    init_app() -- but it keeps the parsed result in a mutable list on
    ``app.extensions["babel"].translation_directories`` and caches loaded
    catalogs per (locale, domain) on the domain instance. Appending to the
    former and clearing the latter is enough to make a live-installed module's
    strings translate on the very next request, instead of showing raw English
    msgids until a restart. Best-effort: a Flask-Babel that ever changes this
    shape simply leaves the module untranslated, which is not worth failing an
    install over.
    """
    tdir = modules_dir() / name / "translations"
    if not tdir.is_dir():
        return
    try:
        babel_cfg = app.extensions.get("babel")
        dirs = babel_cfg.translation_directories
        if str(tdir) not in dirs:
            dirs.append(str(tdir))
        app.config["BABEL_TRANSLATION_DIRECTORIES"] = ";".join(dirs)
        babel_cfg.instance.domain_instance.cache.clear()
        logger.info("[Thirdparties] Live-loaded translations of '%s'", name)
    except Exception:
        logger.warning(
            "[Thirdparties] Could not live-load translations of '%s' — its strings stay "
            "untranslated until the next restart", name, exc_info=True
        )


def install_staged_live(app, folder=None) -> dict:
    """Apply what the store just staged in ``_pending/`` and register it on the
    RUNNING app -- the no-restart half of an install.

    *folder* limits this to the one module the admin just clicked (the normal
    case); None applies everything staged.

    Returns ``{"live": [...], "staged": [...], "failed": [...]}``:

    - **live**: folder was new to this process -- moved into place, imported,
      register(app) called, translations merged. Fully usable now.
    - **staged**: left in ``_pending/`` because the folder is ALREADY imported
      and registered (an upgrade/reinstall). Flask cannot replace a live
      blueprint, so this one genuinely needs the restart, and
      apply_pending_changes() will pick it up at the next start exactly as
      before.
    - **failed**: moving the folder blew up (permissions, a file still open).

    Note the staged->live move is a plain shutil.move of a folder nothing has
    imported yet, so there is no window in which half a module is importable:
    rescan_new_modules() only looks at the folder after it is fully in place.
    """
    package_dir = modules_dir()
    pending_dir = package_dir / PENDING_DIR
    result = {"live": [], "staged": [], "failed": []}
    if not pending_dir.is_dir():
        return result

    known = known_module_names()
    candidates = [
        entry for entry in sorted(pending_dir.iterdir())
        if entry.is_dir() and not entry.name.startswith("_")
        and (folder is None or entry.name == folder)
    ]

    moved = []
    for staged in candidates:
        target = package_dir / staged.name
        # Already live in this process (upgrade/reinstall): the blueprint, the
        # imported module object and the routes are all unreplaceable now.
        if staged.name in known or target.exists():
            result["staged"].append(staged.name)
            continue
        try:
            if not (staged / "__init__.py").is_file():
                raise ValueError("staged folder has no __init__.py")
            shutil.move(str(staged), str(target))
            moved.append(staged.name)
            logger.info("[Thirdparties] Installed module folder '%s' live", staged.name)
        except Exception as exc:
            logger.exception("[Thirdparties] Could not apply staged module '%s' live", staged.name)
            result["failed"].append(f"{staged.name}: {exc}")

    if not moved:
        return result

    for name in moved:
        _add_module_translations(app, name)

    # One rescan covers every folder just moved (and is a no-op for anything
    # else): import + register(app) + lifecycle hooks, exactly as at startup.
    registered = rescan_new_modules(app)
    for name in moved:
        if name in registered:
            result["live"].append(name)
        else:
            # Moved into place but refused to import/register (bad code, unmet
            # DEPENDS_ON, incompatible version...). It is installed -- the
            # Modulmanager card now shows the reason -- it just isn't running.
            result["failed"].append(name)
    return result


def uninstall_module_live(app, name) -> dict:
    """Switch a module off and remove it, on the running app.

    Order matters, and it is the order an admin would expect:

    1. **Disable it first.** Every item the module registered has its master
       toggle set to "0" and ``on_disable(app)`` fires -- so its background
       workers stop, its caches are dropped and it has a chance to clean up
       *while its code is still importable*. Deleting the files of a module
       that is still switched on and mid-poll is how you get a worker thread
       exploding into a traceback five minutes later.
    2. **Unregister it.** Its sidebar link, settings card, dashboard widget and
       provider pill come out of the registry -- gone from the UI on the next
       request (registry.unregister_module()).
    3. **Purge its settings** (namespaced keys only -- see
       registry.purge_module_settings()) and forget the imported module object.
    4. **Delete the folder.**

    Returns ``{"ok", "error", "live", "restart_required"}``. If the folder
    itself cannot be deleted (Windows likes to hold on to files that are still
    open somewhere), everything up to and including step 3 has still happened --
    the module is off, gone from the UI and purged -- and the deletion alone is
    staged for the next start, which is what ``restart_required`` then reports.
    """
    import sys

    name = (name or "").strip()
    if not name or name.startswith("_") or "/" in name or "\\" in name:
        return {"ok": False, "error": "invalid module folder", "live": False,
                "restart_required": False}

    package_dir = modules_dir()
    target = package_dir / name
    if not target.is_dir():
        return {"ok": False, "error": f"no such module folder: {name}", "live": False,
                "restart_required": False}

    entry = module_entry(name) or {}
    module = _LOADED.get(name)
    module_id = getattr(module, "MODULE_ID", None) or _module_id_on_disk(target) or name

    # 1. Disable every item this module registered, then fire on_disable once.
    was_enabled = False
    try:
        from ..db import get_setting, set_setting

        for item_id in (entry.get("item_ids") or ()):
            item = get_thirdparty(item_id)
            if not item:
                continue
            key = item["enabled_setting_key"]
            if get_setting(key, "0") == "1":
                was_enabled = True
            set_setting(key, "0")
    except Exception:
        logger.exception("[Thirdparties] Could not switch off '%s' before removal", name)
    if was_enabled:
        fire_module_hook(name, "on_disable", app)

    # 2. Out of the registry -> out of the UI, immediately.
    prefix = f"{__name__}.{name}"
    blueprints = set(unregister_module(name))
    # ...and ask Flask itself, rather than trusting the registry to know every
    # blueprint: a module can register a Blueprint and then fail (or never call)
    # register_thirdparty(), in which case the registry has no item for it and no
    # blueprint name to report — but its routes are live all the same. Matching on
    # import_name catches those too, so nothing of an uninstalled module stays
    # reachable.
    for bp_name, blueprint in (getattr(app, "blueprints", None) or {}).items():
        import_name = getattr(blueprint, "import_name", "") or ""
        if import_name == prefix or import_name.startswith(prefix + "."):
            blueprints.add(bp_name)
    _UNINSTALLED_BLUEPRINTS.update(blueprints)

    # 3. Settings + data dir + the imported module object.
    try:
        removed = purge_module_settings(module_id)
        logger.info("[Thirdparties] Purged %d setting(s) of '%s'", removed, module_id)
    except Exception:
        logger.exception("[Thirdparties] Could not purge settings of '%s'", module_id)
    # ~/.mediaforge/module_data/<id>/ -- the module's own writable directory
    # (registry.module_data_dir()). Deliberately survives upgrades and stays put
    # while a module is merely disabled; uninstall is the one thing that removes
    # it, exactly like the settings above.
    try:
        purge_module_data(module_id)
    except Exception:
        logger.exception("[Thirdparties] Could not purge data dir of '%s'", module_id)
    _LOADED.pop(name, None)
    for mod_name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        sys.modules.pop(mod_name, None)
    importlib.invalidate_caches()

    # 4. The files.
    try:
        shutil.rmtree(target)
    except Exception as exc:
        logger.warning("[Thirdparties] Could not delete '%s' live (%s) — staging it for the "
                       "next start instead", name, exc)
        try:
            stage_removal(name)
        except Exception as stage_exc:
            logger.exception("[Thirdparties] Could not stage removal of '%s' either", name)
            return {"ok": False, "error": str(stage_exc), "live": False,
                    "restart_required": False}
        return {"ok": True, "error": None, "live": True, "restart_required": True}

    logger.info("[Thirdparties] Uninstalled module '%s' live", name)
    return {"ok": True, "error": None, "live": True, "restart_required": False}
