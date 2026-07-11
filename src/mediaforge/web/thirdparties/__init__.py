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

Lifecycle hooks
---------------
Besides ``register(app)`` (the only required one), a module may define any of
four optional module-level functions -- see :func:`fire_module_hook`:

    def on_install(app): ...                            # first ever start
    def on_upgrade(app, from_version, to_version): ...  # MODULE_VERSION changed
    def on_enable(app): ...                             # master toggle switched on
    def on_disable(app): ...                            # master toggle switched off

``on_install``/``on_upgrade`` are driven by comparing ``MODULE_VERSION``
against the version last recorded for this install
(:func:`registry.installed_version`), so a module gets a real migration point
without hand-rolling its own schema-version tracking -- see
:func:`_run_lifecycle_hooks`. ``on_enable``/``on_disable`` fire on the *edge*
only (registry.py's generic settings route), so they can be treated as
start/stop rather than "re-check on every save".

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
import importlib
import pkgutil
import shutil
from pathlib import Path

from .registry import (
    register_generic_settings_routes, record_module_status, item_ids, seed_default_enabled,
    known_module_names, registered_module_names, check_app_compatibility,
    check_api_compatibility, installed_version, record_installed_version,
    purge_module_settings,
)
from .signing import verify_module
from ...logger import get_logger

logger = get_logger(__name__)

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
    package_dir = Path(__file__).parent
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
    package_dir = Path(__file__).parent
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
    package_dir = Path(__file__).parent
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
    (a tuple of PEP 508 requirement strings, e.g. ``("icalendar>=6.0",)``) is
    installed and satisfies its version specifier, or a human-readable reason
    if not.

    MediaForge deliberately *checks* rather than installs: pip-installing into
    a running app's environment at import time is how you get a half-upgraded
    dependency shared with the core (Flask, niquests, packaging are all in
    here), and in a Docker install the change wouldn't survive the container
    anyway. A module store package that needs a dependency the app doesn't
    ship is therefore something the admin has to install deliberately -- what
    this function buys is that they find out from a one-line "skipped: missing
    dependency icalendar>=6.0" on the Modulmanager page instead of an
    ImportError traceback in the log.
    """
    requirements = tuple(getattr(module, "MODULE_REQUIREMENTS", None) or ())
    if not requirements:
        return ""
    from importlib.metadata import PackageNotFoundError, version as dist_version
    from packaging.requirements import InvalidRequirement, Requirement

    missing = []
    for raw in requirements:
        try:
            req = Requirement(str(raw))
        except InvalidRequirement:
            missing.append(f"{raw} (unparseable)")
            continue
        try:
            have = dist_version(req.name)
        except PackageNotFoundError:
            missing.append(f"{raw} (not installed)")
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            missing.append(f"{raw} (have {have})")
    if not missing:
        return ""
    return "missing dependency: " + ", ".join(missing)


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
    package_dir = Path(__file__).parent
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
    package_dir = Path(__file__).parent
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
        blocked = (
            check_api_compatibility(getattr(module, "MODULE_API_VERSION", None))
            or check_app_compatibility(
                getattr(module, "MODULE_MIN_APP_VERSION", None),
                getattr(module, "MODULE_MAX_APP_VERSION", None),
            )
            or _check_requirements(module)
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
    return newly_registered


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
    package_dir = Path(__file__).parent
    names = sorted(
        name for _finder, name, is_pkg in pkgutil.iter_modules([str(package_dir)])
        if is_pkg and not name.startswith("_")
    )
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
            logger.info("[Thirdparties] Purged %d setting(s) of uninstalled module '%s'",
                        removed, module_id)
        except Exception:
            logger.exception("[Thirdparties] Could not purge settings of '%s'", module_id)

    # Shared enable/disable API for the simple "just a toggle" settings card
    # every registered thirdparty gets automatically — see registry.py.
    # Only ever called here, once -- see this function's own docstring.
    register_generic_settings_routes(app)


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
    package_dir = Path(__file__).parent
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
    names = sorted(
        name for _finder, name, is_pkg in pkgutil.iter_modules([str(package_dir)])
        if is_pkg and not name.startswith("_") and name not in known
    )
    if not names:
        return []
    modules = _import_folders(names)
    if not modules:
        return []
    registered = registered_module_names()
    return _register_modules(app, modules, registered)
