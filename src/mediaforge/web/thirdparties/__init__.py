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
    MODULE_MIN_APP_VERSION = "1.1.0"      # optional compatibility range
    MODULE_MAX_APP_VERSION = ""           # optional; "" = no upper bound
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
Modulmanager page. It's purely informational *today* -- nothing compares it
against anything -- but it's what the planned module store will use to
decide whether an installed module is out of date, which is why every
shipped module should declare (and bump) one now rather than retrofitting
versions onto an already-published module later.

``MODULE_MIN_APP_VERSION`` / ``MODULE_MAX_APP_VERSION`` are the only two of
these that *do* something at load time: they declare which MediaForge
versions the module works on (inclusive bounds, either or both omittable)
and are checked against the running app's own version by
:func:`registry.check_app_compatibility` before ``register(app)`` is called.
A module the running MediaForge falls outside the range of is skipped with
that reason shown on the Modulmanager page, exactly like an unmet
``DEPENDS_ON`` -- better than letting it register and fail in some less
obvious way against an API it wasn't written for.

``MODULE_ID`` / ``MODULE_HOMEPAGE`` / ``MODULE_LICENSE`` are for the module
store and are not used for anything at runtime (the folder name is still
what identifies a module to discovery, ``DEPENDS_ON``, and everything else
here). ``MODULE_ID`` exists so a module keeps a stable store identity even
if its folder gets renamed on disk; the other two are shown on the
Modulmanager card.

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

import importlib
import pkgutil
from pathlib import Path

from .registry import (
    register_generic_settings_routes, record_module_status, item_ids, seed_default_enabled,
    known_module_names, registered_module_names, check_app_compatibility,
)
from ...logger import get_logger

logger = get_logger(__name__)


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
    modules = {}
    for name in names:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            modules[name] = module
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

        # MODULE_MIN_APP_VERSION / MODULE_MAX_APP_VERSION (see this package's
        # docstring) -- checked before register(app), so a module written
        # against an API this MediaForge doesn't have yet (or no longer has)
        # never gets to run against it. Skipped exactly like an unmet
        # DEPENDS_ON: not registered, reason recorded, everything else keeps
        # loading. Note the module is deliberately left out of `registered`,
        # so anything DEPENDS_ON-ing it is skipped too rather than being
        # handed a half-loaded dependency.
        incompatible = check_app_compatibility(
            getattr(module, "MODULE_MIN_APP_VERSION", None),
            getattr(module, "MODULE_MAX_APP_VERSION", None),
        )
        if incompatible:
            logger.warning("[Thirdparties] '%s' skipped — %s", name, incompatible)
            record_module_status(name, registered=False, error=incompatible)
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
