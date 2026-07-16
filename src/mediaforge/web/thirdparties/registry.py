"""Shared registry third-party integrations use to plug into MediaForge's UI
without any other file needing to change. Several independent axes, all
optional and all fully dynamic:

1. Sidebar placement -- a link under one of the existing sidebar categories
   (Discover, Management, System). See ``section`` below and
   :func:`resolve_menu_items`.
2. Settings placement -- a card on one of the existing tabbed/pilled settings
   pages (Integrations, Notifications). A card can either be appended into
   an *existing* tab/pill (e.g. Integrations' "Third Party" tab, or
   Notifications' "Discord" pill) or, if ``settings_tab`` names something
   that page doesn't already render by hand, a brand-new tab/pill is
   created for it automatically. See ``settings_host``/``settings_tab``
   below and :func:`resolve_settings_cards` / :func:`resolve_dynamic_tabs`.
3. Dashboard placement -- a widget on the home page. See
   ``dashboard_widget_template`` below and :func:`resolve_dashboard_widgets`.
4. Provider pills -- a small colored badge in the detail modal / browse
   cards (the same slot Crunchyroll's and Fernsehserien.de's pills use).
   See ``provider_pill_script`` below and
   :func:`resolve_provider_pill_scripts`.
5. Load/relative order -- ``priority`` and ``DEPENDS_ON`` (the latter read
   directly off a thirdparty's ``__init__.py`` module by
   ``web/thirdparties/__init__.py``, not through this function).
6. Module metadata -- four more module-level constants
   (``MODULE_NAME``/``MODULE_DESCRIPTION``/``MODULE_AUTHOR``/
   ``MODULE_ENABLED_DEFAULT``), also read directly off ``__init__.py`` like
   ``DEPENDS_ON``, purely descriptive plus one shipped default. They power
   the admin Modulmanager page (:func:`resolve_extensions_overview`) and,
   for ``MODULE_ENABLED_DEFAULT``, the one-time initial value of a newly
   installed module's enable toggle (:func:`seed_default_enabled`).
7. Version & store metadata -- ``MODULE_VERSION`` plus the compatibility
   range (``MODULE_MIN_APP_VERSION`` / ``MODULE_MAX_APP_VERSION``) and the
   future module store's identity fields (``MODULE_ID`` / ``MODULE_HOMEPAGE``
   / ``MODULE_LICENSE``), read off ``__init__.py`` exactly like the metadata
   above. The version is shown as a badge on the Modulmanager page next to
   the module name; the compatibility range is checked against MediaForge's
   own version at discovery time (:func:`check_app_compatibility`), and a
   module declaring a range the running app falls outside of is skipped with
   that reason instead of being registered -- see
   ``web/thirdparties/__init__.py``'s ``_register_modules()``.

An integration needs none of these axes -- a background-job-only
integration can register with no ``endpoint`` and no card content beyond
the master toggle, or skip registration entirely and just call
``register(app)``'s ``app.register_blueprint(bp)``.

A thirdparty's own ``__init__.py`` calls :func:`register_thirdparty` once,
from its ``register(app)`` function -- see
``web/thirdparties/anime_seasons/__init__.py`` for a full worked example.
That single call is enough for every integration point: nothing in app.py,
base.html, integrations.html, notifications.html or index.html needs to be
touched to add a new one.

``web/thirdparties/__init__.py``'s ``discover_and_register()`` also feeds
:func:`record_module_status` for every discovered folder (not just ones
that successfully call :func:`register_thirdparty`), so the admin
Extensions overview page (:func:`resolve_extensions_overview`,
``routes/extensions.py``) can show broken/skipped integrations too.
"""

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_ITEMS = []

# Per-folder load status, keyed by thirdparties/<name>/ folder name -- see
# record_module_status()'s docstring. Separate from _ITEMS because a folder
# can fail before ever calling register_thirdparty() (import error, no
# register(app), unmet DEPENDS_ON, ...), and the overview page wants to
# show those failures too.
_MODULES = {}

# The sidebar categories a registered item's link can appear under -- see
# base.html's "Discover" / "Management" / "SyncPlay" / "System" blocks. Kept
# as a fixed set (rather than letting a plugin invent a brand-new sidebar
# category) so the sidebar's overall shape stays predictable; settings
# placement (below) is where free-form "new tab" dynamism lives instead.
# "syncplay" entries only ever render while syncplay_enabled is on (see
# base.html) — same as the built-in SyncPlay link itself.
_SECTIONS = ("discover", "management", "syncplay", "system")

# Tab/pill ids each settings host already renders by hand in its own
# template. A registered item whose settings_tab matches one of these is
# appended into that existing tab/pill's panel (e.g. adding a card to
# Integrations' "uptime" tab, or to Notifications' "discord" pill). A
# settings_tab that matches neither is a request for a brand-new tab/pill,
# which resolve_dynamic_tabs() surfaces so the template can render it.
_KNOWN_TABS = {
    "integrations": ("seerr", "mediaplayer", "cineinfo", "thirdparty", "syncplay", "uptime"),
    "notifications": ("webpush", "telegram", "pushover", "ntfy", "discord", "whatsapp", "storage"),
    "settings": ("overview", "general", "design", "sources", "downloads", "autosync", "network", "auth", "api", "privacy", "backup", "updates"),
}

# Field types _build_card()/the settings-card macro/the generic PUT route
# understand for extra_settings entries. "toggle" is the original (and
# still default) boolean checkbox; the rest render as a labelled input of
# the matching HTML type plus a small inline Save button.
_FIELD_TYPES = ("toggle", "text", "number", "secret", "select")

# What the generic GET route returns in place of a "secret" field's actual
# value, and the value the generic PUT route treats as "unchanged, keep what
# is stored" (see register_generic_settings_routes). The point is that a
# stored token/API key is never sent back to the browser at all -- the input
# is a type="password" field, so its plaintext value was previously sitting in
# the DOM of every admin page that rendered the card, readable by any script
# on it. An empty string is still a real value (it clears the setting), so the
# mask has to be a distinct sentinel rather than "".
SECRET_MASK = "•" * 8

# Access levels register_thirdparty's auth_required accepts -- just "admin"
# for now (the app only distinguishes logged-in-user vs admin, see
# auth.py's login_required/admin_required); None (the default) means no
# stricter check than "logged in", same as any other page.
_AUTH_LEVELS = ("admin",)

# Generic placeholder icon for an auto-created tab/pill button when the
# registering integration doesn't supply its own (settings_tab_icon_svg).
# Only used by notifications.html's pill row; integrations.html's tab
# buttons are plain text and don't render an icon at all.
_DEFAULT_TAB_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="7" y="7" width="10" height="10" rx="2"></rect>'
    '<path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"></path>'
    '</svg>'
)

# Shown as a module's version badge on the Modulmanager page when it declares
# no MODULE_VERSION at all -- every module predating the constant, and any
# module whose author simply didn't bother, still renders a well-formed badge
# instead of an empty gap.
_UNKNOWN_VERSION = "0.0.0"

# Version of the contract THIS file exposes to modules: register_thirdparty()'s
# parameters, the recognized extra_settings field types, the lifecycle hooks
# web/thirdparties/__init__.py calls, and the module-level MODULE_* constants
# it reads. A module declares the API version it was written against as
# MODULE_API_VERSION; see check_api_compatibility() for what happens when the
# two don't line up.
#
# Deliberately separate from MediaForge's own version (see app_version()):
# MediaForge can go to 2.0 for reasons that have nothing to do with modules,
# and a module pinning MODULE_MAX_APP_VERSION out of caution would then have
# to be re-released for no reason. Bumping *this* number is a deliberate act
# that means exactly one thing -- "the module contract changed in a way older
# modules can't be assumed to survive" -- so it's the number a module should
# actually be pinning against. Bump it only for breaking changes; a purely
# additive one (a new optional parameter, a new MODULE_* constant a module can
# ignore) doesn't need it.
REGISTRY_API_VERSION = 1

# Prefix every setting a module owns lives under -- see module_setting_key().
_MODULE_SETTING_PREFIX = "module:"

# Key (under a module's own namespace) where the version last seen installed is
# recorded, so the next start can tell a fresh install from an upgrade from an
# unchanged module and fire the right lifecycle hook. Double underscore so it
# can't collide with a setting key a module itself declares.
_INSTALLED_VERSION_KEY = "__installed_version"


def app_version():
    """MediaForge's own version, as a plain comparable string ("1.1.0").

    Deliberately the *base* version from package metadata rather than
    version_info._get_display_version(): the latter appends a
    "-dev+<sha>" suffix on git installs, which parses as a
    pre-release/local version and would make a dev checkout of 1.1.0 count
    as *older* than 1.1.0 for a module declaring
    MODULE_MIN_APP_VERSION = "1.1.0" -- i.e. every dev install would refuse
    to load modules that work perfectly well on it. The display version is
    still what the Modulmanager page *shows* (see base.html's app_version);
    this one is only ever used for the compatibility comparison.

    Returns "" when the package isn't installed (running straight from a
    source tree without an install), which check_app_compatibility() treats
    as "can't tell, don't block".
    """
    from ..version_info import _get_version

    return _get_version()


def check_app_compatibility(min_app_version=None, max_app_version=None):
    """Return None if the running MediaForge satisfies a module's declared
    compatibility range, or a human-readable reason string if it doesn't.

    Both bounds are optional and inclusive: MODULE_MIN_APP_VERSION = "1.1.0"
    means "needs 1.1.0 or newer", MODULE_MAX_APP_VERSION = "1.9.9" means
    "not tested past 1.9.9". A module declaring neither (i.e. every module
    that existed before this constant did) is always compatible.

    Anything that can't be compared cleanly -- no installed version to check
    against, or a bound that isn't a valid version string -- is treated as
    compatible rather than as a failure: a typo'd bound in one module must
    not be able to keep that module (or, worse, its dependents) from loading
    on an otherwise fine app. The mismatch is still visible in the log via
    the caller.
    """
    if not min_app_version and not max_app_version:
        return None

    from packaging.version import InvalidVersion, Version

    current_raw = app_version()
    if not current_raw:
        return None
    try:
        current = Version(current_raw)
    except InvalidVersion:
        return None

    try:
        if min_app_version and current < Version(str(min_app_version)):
            return (f"requires MediaForge >= {min_app_version} "
                    f"(running {current_raw})")
        if max_app_version and current > Version(str(max_app_version)):
            return (f"requires MediaForge <= {max_app_version} "
                    f"(running {current_raw})")
    except InvalidVersion:
        return None
    return None


def check_api_compatibility(module_api_version):
    """Return None if this MediaForge's registry API can serve a module written
    against `module_api_version` (its MODULE_API_VERSION), or a reason string
    if it can't.

    Only a module asking for a *newer* API than REGISTRY_API_VERSION is
    refused: it was written against a contract this MediaForge doesn't have,
    and calling its register(app) would mean handing it a registry missing
    whatever it's counting on. A module written against an *older* API is
    accepted -- that's the whole point of only bumping REGISTRY_API_VERSION on
    breaking changes, and of every MODULE_* constant having a fallback: an API
    1 module keeps working on an API 1 MediaForge forever, and the day API 2
    exists it's this function's job to say so out loud rather than let it fail
    somewhere less obvious.

    A module that declares no MODULE_API_VERSION at all (None) is treated as
    "API 1" -- i.e. every module written before this constant existed.
    """
    if module_api_version in (None, ""):
        return None
    try:
        requested = int(module_api_version)
    except (TypeError, ValueError):
        return f"invalid MODULE_API_VERSION {module_api_version!r}"
    if requested > REGISTRY_API_VERSION:
        return (f"needs registry API v{requested}, "
                f"this MediaForge provides v{REGISTRY_API_VERSION}")
    return None


def module_setting_key(module_id, key):
    """The app_settings key a module's setting `key` should live under:
    ``module:<module_id>:<key>``.

    Modules are free to keep using flat, unnamespaced keys (anime_seasons's
    "anime_seasons_enabled" predates this and still works -- nothing here
    rewrites or requires anything), but a module that wants to be
    *uninstallable* by the module store should namespace everything it stores
    through this helper: purge_module_settings() can then remove all of it in
    one go when the module's folder is removed, instead of leaving orphaned
    rows in app_settings forever. Namespacing is therefore a soft requirement
    for store-published modules and a no-op for everything else.
    """
    return f"{_MODULE_SETTING_PREFIX}{module_id}:{key}"


def purge_module_settings(module_id):
    """Delete every namespaced setting belonging to `module_id` (see
    module_setting_key()), returning how many rows were removed.

    Called when a module is uninstalled (see web/thirdparties/__init__.py's
    apply_pending_changes()). Un-namespaced keys a module wrote directly are
    deliberately *not* touched -- there's no way to know which flat keys were
    a module's without it telling us, and guessing by prefix-matching the
    folder name would risk deleting a core setting that merely starts with the
    same word.
    """
    from ..db import delete_settings_by_prefix

    return delete_settings_by_prefix(f"{_MODULE_SETTING_PREFIX}{module_id}:")


def installed_version(module_id):
    """The MODULE_VERSION last recorded as installed for this module, or None
    if it's never been seen on this install before (a fresh install).

    This is what makes on_install/on_upgrade possible without the module
    tracking its own state: the folder on disk says which version the *code*
    is, this says which version the *data* (settings, DB tables, caches) was
    last written by. See web/thirdparties/__init__.py's _run_lifecycle_hooks().
    """
    from ..db import get_setting

    return get_setting(module_setting_key(module_id, _INSTALLED_VERSION_KEY), None)


def record_installed_version(module_id, version):
    """Persist `version` as the installed version of `module_id` -- called
    right after a successful on_install/on_upgrade hook (or right after a
    successful register(app) for a module with no hooks), so the next start
    sees an unchanged module and fires nothing.
    """
    from ..db import set_setting

    set_setting(module_setting_key(module_id, _INSTALLED_VERSION_KEY), str(version or ""))


def module_data_dir(module_id, create=True):
    """The one directory a module may write to:
    ``~/.mediaforge/module_data/<module_id>/``.

    A module's own folder is NOT a place to write. Two things make that a trap,
    and both have already bitten:

    * It is what the module's signature is computed over
      (``signing.content_hash``) -- a cache, a downloaded file, a vendored
      dependency dropped in there makes the module "modified" and drops it to
      unverified, by running normally.
    * The store replaces a module on upgrade by deleting the folder
      (``store._unpack``'s rmtree) and unpacking the new one. Anything the
      module put in there is gone on every update.

    This directory has neither problem: outside every module folder, so it can't
    touch a signature, and untouched by installs and upgrades. It survives an
    upgrade on purpose -- it's the module's data, not its code. It is removed
    only when the module is uninstalled (see purge_module_data()).

    Namespaced by MODULE_ID rather than folder name, so a module keeps its data
    across a folder rename, exactly like its settings (module_setting_key()).
    """
    from ...config import MEDIAFORGE_CONFIG_DIR

    path = Path(MEDIAFORGE_CONFIG_DIR) / "module_data" / str(module_id)
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("[Registry] Could not create module data dir %s", path)
    return path


def purge_module_data(module_id) -> bool:
    """Delete a module's data directory -- called on uninstall, alongside
    purge_module_settings(). Returns True if something was removed.

    Uninstall is the only thing that removes it: an *upgrade* deliberately keeps
    it (that is the entire difference between this directory and the module
    folder), and a disabled module keeps its data so re-enabling it doesn't mean
    starting from scratch.
    """
    import shutil

    path = module_data_dir(module_id, create=False)
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
        logger.info("[Registry] Removed module data dir %s", path)
        return True
    except Exception:
        logger.warning("[Registry] Could not remove module data dir %s", path, exc_info=True)
        return False


def register_thirdparty(*, item_id, label, endpoint=None, icon_svg=None,
                         enabled_setting_key, badges=None, description="",
                         enable_label=None, enable_desc="",
                         page_id=None, extra_settings=None,
                         sensitive_settings=None, admin_endpoints=None,
                         section="discover",
                         settings_host="integrations", settings_tab="thirdparty",
                         settings_tab_label=None,
                         settings_tab_icon_svg=None,
                         priority=0,
                         dashboard_widget_template=None,
                         provider_pill_script=None,
                         requires_enabled=None,
                         auth_required=None,
                         blueprint=None):
    """Register (or replace) a third-party integration.

    - item_id: unique key; re-registering the same id replaces it instead of
      duplicating it (safe under the debug reloader).
    - label: English source string for the sidebar link / card title,
      translated at render time via flask_babel.gettext (same catalog as
      the Jinja _() calls).
    - endpoint / icon_svg: Flask endpoint name (blueprint-qualified, e.g.
      "anime_seasons.anime_seasons_page") and raw <svg>...</svg> markup
      (stroke="currentColor" so it inherits the sidebar's icon color) for
      the sidebar link. Both optional together -- omit both for an
      integration that has no page of its own (e.g. a settings-only
      extension, such as an extra notification channel). Supplying only one
      of the two raises ValueError.
    - enabled_setting_key: app_settings key gating the sidebar link, the
      dashboard widget, the provider-pill script and (indirectly) whatever
      the integration's own routes check.
    - badges: list of (text, css_color) tuples shown as small pills on the
      settings card header, e.g. [("Jikan", "#2e51a2"), ("Menu", "#7c3aed")].
    - description: hint text shown at the top of the settings card.
    - enable_label / enable_desc: label/description for the card's enable
      toggle row. enable_label defaults to "Enable {label}".
    - page_id: value for the sidebar-link's data-page attribute (used to
      highlight the active link). Defaults to item_id.
    - extra_settings: optional list of additional setting rows shown below
      the master enable toggle, e.g.
      [{"key": "anime_seasons_show_adult", "label": "Show adult content",
      "description": "...", "type": "toggle", "default": "0"}]. Each dict
      needs "key" (the app_settings key this field reads/writes) and
      "label"; "description" is optional. "type" defaults to "toggle" (a
      checkbox) and can also be:
        * "text" / "secret" -- a single-line input ("secret" renders as
          type="password", for API keys/tokens). "placeholder" optional.
          A "secret" field is additionally treated as a *sensitive* setting:
          its value is stored encrypted (db.register_sensitive_keys(), same
          encryption the core's own API keys/tokens use) and is never sent
          back to the browser -- the generic GET route returns SECRET_MASK
          once a value is set, and a PUT carrying that mask back is taken to
          mean "unchanged" (send "" to clear the value).
        * "number" -- an input[type=number]. "placeholder" optional.
        * "select" -- a dropdown. Requires "options": a list of
          (value, label) tuples (or plain strings, used as both).
      "default" is the field's initial value if unset ("0"/"1" for
      "toggle", "" otherwise unless given). All types are read and saved
      generically by the same GET/PUT /api/settings/thirdparty/<item_id>
      pair the master toggle uses (see register_generic_settings_routes) --
      no per-integration route needed. An integration that needs something
      even the "select"/"text" types can't express (a test-connection
      button, dynamic option lists, ...) should still add its own routes
      instead.
    - sensitive_settings: optional list of *further* setting keys whose values
      must be stored encrypted, on top of every extra_settings field of type
      "secret" (which is registered as sensitive automatically). For a secret
      the module manages itself and never renders as a card field -- an OAuth
      refresh token, a session cookie. Full app_settings keys, so namespace
      them like everything else:
      sensitive_settings=(module_setting_key(MODULE_ID, "refresh_token"),).
      Equivalent to the module-level MODULE_SENSITIVE_SETTINGS constant (see
      web/thirdparties/__init__.py); use whichever is closer to hand.
    - admin_endpoints: optional list of endpoint names that require the admin
      role, for a Blueprint whose routes are NOT uniformly admin-only.
      auth_required="admin" (below) is blueprint-wide, all-or-nothing: a module
      where any logged-in user may read but only an admin may write has to
      guard the write routes by hand -- and forgets exactly one of them, which
      is how a non-admin ended up able to write global settings once already.
      This is the per-route version: name the endpoints, in either
      "blueprint.view" or plain "view" form (the blueprint is inferred from
      this item's own), e.g.
      admin_endpoints=("my_module.api_settings_put", "api_purge").
      A route can equally well carry the exported @module_admin_required
      decorator itself (see below) -- both end up in the same place, app.py's
      wrapping pass, so use whichever reads better. The two compose: an
      endpoint named here AND decorated is simply admin-only once.
    - section: which sidebar category the link (if any) appears under --
      one of "discover" (default), "management", "syncplay" or "system",
      matching base.html's sidebar categories. A "syncplay" entry only ever
      renders while SyncPlay itself is enabled (same gating as the built-in
      SyncPlay link). Ignored if endpoint/icon_svg aren't set (no link to
      place).
    - settings_host: which existing settings page's tab/pill system the
      settings card should be shown on -- "integrations" (default, the
      Integrations page's tab bar), "notifications" (the Notifications
      page's service-pill row), or "settings" (the main Settings page's tab
      bar, e.g. settings_tab="downloads" to add a card alongside the
      built-in Downloads tab).
    - settings_tab: tab/pill id within settings_host to attach to. Matching
      one of that host's existing ids (see _KNOWN_TABS above) appends the
      card into that tab/pill's existing content, alongside whatever it
      already renders by hand -- e.g. settings_host="notifications",
      settings_tab="discord" adds a card into the existing Discord pill
      instead of creating a new one. Anything else (default: "thirdparty",
      preserving the original behaviour) creates a brand-new tab/pill
      automatically -- see resolve_dynamic_tabs().
    - settings_tab_label / settings_tab_icon_svg: label and (notifications
      pill only) icon for the tab/pill button when settings_tab creates a
      *new* tab/pill. Both ignored when attaching to an existing one.
      settings_tab_label defaults to label; settings_tab_icon_svg defaults
      to a generic placeholder icon.
    - priority: sort key (lower = earlier/higher up) used to order this
      item relative to *other registered items* within the same sidebar
      section, settings tab, or set of brand-new tabs/dashboard widgets.
      Never reorders anything relative to MediaForge's own built-in
      entries -- those always come first, registered items are always
      appended after them in priority order. Defaults to 0; items with
      equal priority keep their (dependency-resolved) registration order.
    - dashboard_widget_template: optional Jinja template name/path
      rendered as a widget on the home page (index.html), via
      {% include %} -- e.g. a template from this integration's own
      Blueprint template_folder. Only shown while enabled_setting_key is
      "1". See :func:`resolve_dashboard_widgets`.
    - provider_pill_script: optional static URL (e.g. built with
      url_for('your_blueprint.static', filename='pill.js')) to a small
      JS file that self-registers a provider-pill resolver via the global
      registerProviderPill(name, resolverFn) (see base.html / app.js).
      Included as a <script> on every page while enabled_setting_key is
      "1". See :func:`resolve_provider_pill_scripts`.
    - requires_enabled: optional tuple of *other* registered item_ids this
      one needs switched on to actually work, e.g.
      requires_enabled=("mediacalendar",) for a module that reads Media
      Kalender's data. Unlike DEPENDS_ON (web/thirdparties/__init__.py's
      module-level constant, checked once at startup against whether the
      other folder's register(app) succeeded at all), this is a live,
      per-request check against the dependency's *current* enabled_setting_key*
      toggle state -- see :func:`dependencies_satisfied`. resolve_menu_items()
      already calls it (an item whose requires_enabled isn't currently met
      simply doesn't show a sidebar link, same as if its own toggle were
      off), and a module's own routes should call
      ``registry.dependencies_satisfied(item_id)`` alongside their own
      enabled check for the same reason -- e.g. a page route that should
      404/redirect if a dependency got switched off after the page's own
      toggle was turned on.
    - auth_required: optional access level for every route this
      integration's own Blueprint registers -- None (default, same as any
      other logged-in page), or "admin" to require the admin role, mirroring
      app.py's hardcoded _admin_only set but declaratively instead of
      needing an entry added there by hand. Matched by blueprint name (the
      part of a Flask endpoint before the dot), not by item_id, since one
      folder's Blueprint can register several routes beyond the single
      sidebar `endpoint` tracked here -- see
      :func:`admin_required_blueprints`.
    - blueprint: the Blueprint name auth_required should apply to. Defaults
      to endpoint's own blueprint (endpoint.split(".")[0]) when endpoint is
      set -- only needed explicitly for a settings-only integration that
      has no sidebar endpoint/icon_svg but still registers its own
      Blueprint/routes elsewhere.
    """
    if section not in _SECTIONS:
        raise ValueError(f"register_thirdparty: unknown section {section!r}, must be one of {_SECTIONS}")
    if bool(endpoint) != bool(icon_svg):
        raise ValueError("register_thirdparty: endpoint and icon_svg must both be set or both omitted")
    if auth_required is not None and auth_required not in _AUTH_LEVELS:
        raise ValueError(
            f"register_thirdparty: unknown auth_required {auth_required!r}, must be one of {_AUTH_LEVELS} or None")
    resolved_blueprint = blueprint or (endpoint.split(".")[0] if endpoint else None)
    if auth_required and not resolved_blueprint:
        raise ValueError(
            "register_thirdparty: auth_required needs a blueprint -- either endpoint/icon_svg "
            "(blueprint inferred from it) or an explicit blueprint= name")

    normalized_extra = []
    for setting in extra_settings or []:
        field_type = setting.get("type", "toggle")
        if field_type not in _FIELD_TYPES:
            raise ValueError(
                f"register_thirdparty: extra_settings[{setting.get('key')!r}] has unknown "
                f"type {field_type!r}, must be one of {_FIELD_TYPES}")
        if field_type == "select" and not setting.get("options"):
            raise ValueError(
                f"register_thirdparty: extra_settings[{setting.get('key')!r}] is type='select' "
                "but has no 'options'")
        normalized = dict(setting)
        normalized["type"] = field_type
        normalized.setdefault("default", "0" if field_type == "toggle" else "")
        normalized["options"] = [
            (o, o) if isinstance(o, str) else (o[0], o[1])
            for o in setting.get("options", [])
        ]
        normalized_extra.append(normalized)

    # Every "secret" field is a sensitive setting by definition -- register it
    # so db.set_setting() encrypts it at rest instead of storing an API
    # key/token in plaintext in app_settings, and so any value already stored
    # in plaintext (from before this module declared the field, or from before
    # this mechanism existed) gets encrypted right now. This is what makes a
    # module's own token as safe as a core one (notif_telegram_bot_token &
    # co., db.SENSITIVE_KEYS) without a core release per module.
    #
    # A module with a secret it does NOT expose as an extra_settings field
    # (something it stores itself, with no settings-card row) can name those
    # keys in MODULE_SENSITIVE_SETTINGS instead -- see
    # web/thirdparties/__init__.py.
    secret_keys = [s["key"] for s in normalized_extra
                   if s["type"] == "secret" and s.get("key")]
    secret_keys += [str(k) for k in (sensitive_settings or ()) if k]
    if secret_keys:
        try:
            from ..db import register_sensitive_keys

            register_sensitive_keys(secret_keys)
        except Exception:
            # Never let this fail a registration: the field still works, it
            # just stays in plaintext -- exactly the pre-existing behaviour.
            logger.warning(
                "[Registry] Could not mark secret field(s) of '%s' as sensitive: %s",
                item_id, ", ".join(secret_keys), exc_info=True)

    # Endpoint names are blueprint-qualified once Flask has them
    # ("my_module.api_save"), so a module naming a bare view function gets it
    # qualified with its own blueprint here -- writing the prefix out is
    # boilerplate, and getting it wrong would silently mean "no admin check".
    normalized_admin_endpoints = tuple(
        e if "." in e else (f"{resolved_blueprint}.{e}" if resolved_blueprint else e)
        for e in (str(x) for x in (admin_endpoints or ()))
    )

    global _ITEMS
    _ITEMS = [i for i in _ITEMS if i["id"] != item_id]
    _ITEMS.append({
        "id": item_id,
        "label": label,
        "endpoint": endpoint,
        "icon_svg": icon_svg,
        "enabled_setting_key": enabled_setting_key,
        "badges": list(badges or []),
        "description": description,
        "enable_label": enable_label or f"Enable {label}",
        "enable_desc": enable_desc,
        "page_id": page_id or item_id,
        "extra_settings": normalized_extra,
        "section": section,
        "settings_host": settings_host,
        "settings_tab": settings_tab,
        "settings_tab_label": settings_tab_label or label,
        "settings_tab_icon_svg": settings_tab_icon_svg or _DEFAULT_TAB_ICON_SVG,
        "priority": priority,
        "dashboard_widget_template": dashboard_widget_template,
        "provider_pill_script": provider_pill_script,
        "requires_enabled": tuple(requires_enabled or ()),
        "auth_required": auth_required,
        "admin_endpoints": normalized_admin_endpoints,
        "blueprint": resolved_blueprint,
    })


def get_thirdparty(item_id):
    for item in _ITEMS:
        if item["id"] == item_id:
            return item
    return None


def unregister_module(name):
    """Remove every trace of thirdparties/<name>/ from the registry, live.

    The counterpart of a module's register_thirdparty() call(s): its sidebar
    link, settings card, dashboard widget and provider-pill script all come out
    of _ITEMS, so dropping its entries here makes the module disappear from the
    UI on the very next request — no restart, no template change. The recorded
    folder status (_MODULES) goes too, so the Modulmanager stops listing it and
    a later rescan would treat the folder as brand new again if it reappeared.

    Returns the blueprint names the removed items owned. Flask cannot
    *un*register a blueprint on a running app, so the caller (see
    web/thirdparties/__init__.py's uninstall_module_live()) uses those names to
    404 whatever routes are left behind until the next restart.

    Purely a registry operation: it does not touch the module's files, settings
    or hooks -- uninstall_module_live() orchestrates all of that.
    """
    global _ITEMS

    entry = _MODULES.get(name) or {}
    ids = set(entry.get("item_ids") or ())
    blueprints = {
        item.get("blueprint")
        for item in _ITEMS
        if item["id"] in ids and item.get("blueprint")
    }
    # Stop and forget any background worker these items registered, before their
    # items disappear -- afterwards _worker_should_run() couldn't even find the
    # item to know it should be off, and the thread would outlive the module.
    for item_id in ids:
        unregister_background_worker(item_id)
    _ITEMS = [item for item in _ITEMS if item["id"] not in ids]
    _MODULES.pop(name, None)
    return sorted(blueprints)


def item_ids():
    """Current snapshot of registered item ids. Used by
    web/thirdparties/__init__.py's discover_and_register() to diff
    before/after a module's register(app) call, so it knows which item_ids
    that particular folder just registered (for record_module_status /
    the Extensions overview page) without every integration having to
    report that itself.
    """
    return {item["id"] for item in _ITEMS}


def known_module_names() -> set:
    """Every thirdparties/<name>/ folder name already seen by a prior
    discover_and_register() or rescan_new_modules() call (successfully or
    not -- a folder that failed to import is still "known", so a rescan
    doesn't keep retrying it forever without a code fix). Used by
    web/thirdparties/__init__.py's rescan_new_modules() to figure out
    which folders currently on disk are genuinely new."""
    return set(_MODULES)


def registered_module_names() -> set:
    """Folder names whose register(app) already ran successfully in this
    process. Used by rescan_new_modules() as the starting point for
    DEPENDS_ON resolution, so a brand-new module can depend on a folder
    that was registered back at original startup, not just on another
    brand-new one discovered in the same rescan."""
    return {name for name, mod in _MODULES.items() if mod.get("registered")}


def record_module_status(name, **fields):
    """Track the load status of one thirdparties/<name>/ folder, called
    from web/thirdparties/__init__.py's discover_and_register() at each
    phase (import, dependency check, register(app) call). Only keys
    explicitly passed (and not None) are updated, so partial updates from
    different phases layer instead of clobbering each other -- e.g. the
    import phase sets imported=True, and the later register phase sets
    registered=True/False without needing to re-pass imported=.

    Recognized fields: imported (bool), registered (bool), error (str),
    depends_on (tuple of folder names), item_ids (list of item ids this
    folder registered -- set once, after diffing item_ids() before/after
    its register(app) call), module_name/description/description_de/
    description_en/author/enabled_default (the MODULE_* constants read off
    the module -- see web/thirdparties/__init__.py's import phase;
    module_name falls back to the folder name, description/description_de/
    description_en/author to "", enabled_default to False when a module
    doesn't declare them, so older/minimal modules with no MODULE_*
    constants at all keep working unchanged). description_de/description_en
    are optional per-language overrides of description -- see
    resolve_extensions_overview()'s _localized_module_description(), which
    picks between them by the current UI language at render time; plain
    description is still what's stored/shown when neither is declared.

    Also recognized, from the same import phase: version (MODULE_VERSION,
    falling back to _UNKNOWN_VERSION), min_app_version/max_app_version (the
    MediaForge compatibility range, see check_app_compatibility()),
    api_version (MODULE_API_VERSION, see check_api_compatibility()),
    requirements (MODULE_REQUIREMENTS, the pip distributions the module needs
    -- checked, not installed) and the module store's identity fields
    module_id/homepage/license (MODULE_ID -- the stable store id, falling back
    to the folder name -- plus MODULE_HOMEPAGE/MODULE_LICENSE, both "").
    None of them are required: they're descriptive, except
    min/max_app_version, api_version and requirements, which
    web/thirdparties/__init__.py checks before calling register(app).

    This exists purely for the admin Modulmanager / Extensions overview
    (resolve_extensions_overview) -- nothing else reads _MODULES.
    """
    entry = _MODULES.setdefault(name, {
        "name": name, "depends_on": (), "imported": None, "registered": None,
        "error": None, "item_ids": [],
        "module_name": name, "description": "", "description_de": "", "description_en": "",
        "author": "", "enabled_default": False,
        "version": _UNKNOWN_VERSION, "min_app_version": "", "max_app_version": "",
        "module_id": name, "homepage": "", "license": "",
        "api_version": None, "requirements": (),
        # The subset of `requirements` that isn't satisfied right now (see
        # deps.missing_requirements()). Non-empty = the Modulmanager shows this
        # module as "needs a dependency" with an Install button, instead of a
        # module that mysteriously isn't there.
        "missing_requirements": (),
        # Result of signing.verify_module() -- see its docstring. The default is
        # what an unsigned module gets, which is most of them: perfectly loadable,
        # just not vouched for by anybody.
        "signature": {"tier": "unverified", "signed": False, "valid": False,
                      "signer": "", "key_id": "", "reason": "not signed"},
    })
    for key, value in fields.items():
        if value is not None:
            entry[key] = value


def module_entry(name):
    """The raw recorded status of one thirdparties/<name>/ folder (see
    record_module_status), or None. Used by the store client
    (web/thirdparties/store.py) to compare an installed module's version and
    module_id against what the store offers, without re-deriving either.
    """
    entry = _MODULES.get(name)
    return dict(entry) if entry else None


def module_entries():
    """Every recorded folder, keyed by folder name -- the store client's
    "what's installed" snapshot. Copies, so a caller can't mutate _MODULES.
    """
    return {name: dict(entry) for name, entry in _MODULES.items()}


def module_name_for_item(item_id):
    """The thirdparties/<name>/ folder that registered `item_id`, or None.

    The registered items and the folders that registered them are tracked
    separately on purpose (see _MODULES' docstring), but the enable/disable
    toggle only knows an item_id -- and the lifecycle hooks
    (on_enable/on_disable) live on the *module*. This is the bridge.
    """
    for name, mod in _MODULES.items():
        if item_id in (mod.get("item_ids") or ()):
            return name
    return None


def seed_default_enabled(new_item_ids, enabled_default):
    """Called once, right after a module's register(app) has registered
    new_item_ids (see web/thirdparties/__init__.py), to apply its
    MODULE_ENABLED_DEFAULT -- but *only* the very first time each item is
    ever seen. get_setting(key, None) is None exactly when nothing has
    ever been written for that key (no row in app_settings), which is how
    this tells "never configured" apart from "user explicitly turned it
    off" -- a module shipping enabled_default=True must never re-flip a
    setting a user (or a previous run) already decided.
    """
    if not enabled_default or not new_item_ids:
        return
    from ..db import get_setting, set_setting

    for item_id in new_item_ids:
        item = get_thirdparty(item_id)
        if not item:
            continue
        key = item["enabled_setting_key"]
        if get_setting(key, None) is None:
            set_setting(key, "1")


def _localized_module_description(mod):
    """Pick MODULE_DESCRIPTION_DE/_EN for the admin's current UI language,
    falling back to the plain (language-agnostic) MODULE_DESCRIPTION when
    the specific-language variant isn't declared. Both are optional --
    a module that only sets MODULE_DESCRIPTION keeps showing exactly that,
    unchanged, in either language.

    This is deliberately NOT routed through gettext/.po files like
    register_thirdparty()'s description/enable_desc are (see _build_card()):
    MODULE_DESCRIPTION is a plain constant read once off a module's
    __init__.py at import time (web/thirdparties/__init__.py's discovery
    phase), long before there's a request/locale to translate for, and
    wiring up pybabel extraction + a .po msgid for a single ad-hoc string
    per module would be a lot of machinery for very little benefit -- an
    explicit German/English pair the module author fills in directly is
    simpler and needs no compile step."""
    from flask_babel import get_locale
    lang = str(get_locale() or "en")
    if lang == "de" and mod.get("description_de"):
        return mod["description_de"]
    if lang == "en" and mod.get("description_en"):
        return mod["description_en"]
    return mod.get("description") or ""


def resolve_extensions_overview():
    """Combine per-folder load status (_MODULES) with per-item placement
    info (_ITEMS) into one list for the admin Extensions overview page
    (routes/extensions.py, templates/extensions.html). Includes folders
    that failed to import/register, not just healthy ones -- that's the
    whole point of this page: a place to see why an integration isn't
    showing up, without reading the server log.
    """
    from ..db import get_setting

    out = []
    for name in sorted(_MODULES):
        mod = _MODULES[name]
        registered_items = []
        for item_id in mod["item_ids"]:
            item = get_thirdparty(item_id)
            if not item:
                continue
            registered_items.append({
                "id": item["id"],
                "label": item["label"],
                "enabled": get_setting(item["enabled_setting_key"], "0") == "1",
                "section": item["section"] if item["endpoint"] else None,
                "settings_host": item["settings_host"],
                "settings_tab": item["settings_tab"],
                "has_dashboard_widget": bool(item["dashboard_widget_template"]),
                "has_provider_pill": bool(item["provider_pill_script"]),
                # requires_enabled (see register_thirdparty) -- shown on the
                # Modulmanager row so an admin can see *why* an enabled
                # module might not actually be doing anything yet.
                "requires_enabled": item["requires_enabled"],
                "requires_enabled_ok": dependencies_satisfied(item["id"]),
            })
        out.append({
            "name": mod["name"],
            "imported": mod["imported"],
            "registered": mod["registered"],
            "error": mod["error"],
            "depends_on": mod["depends_on"],
            # The four MODULE_* constants (see web/thirdparties/__init__.py
            # and this module's "Module metadata" docs) -- module_name is
            # what the Modulmanager page displays as the card title
            # instead of the raw folder name, when a module declares one.
            "module_name": mod["module_name"],
            "description": _localized_module_description(mod),
            "author": mod["author"],
            "enabled_default": mod["enabled_default"],
            # Version & store metadata (see this module's docstring, axis 7).
            # compatible is recomputed here rather than read back off the
            # recorded error string, so the page states it as a fact of the
            # *current* app version rather than of whatever version was
            # running when the module was first discovered -- the two only
            # differ across an in-place upgrade, but that's exactly the case
            # this row exists for.
            "version": mod["version"],
            "min_app_version": mod["min_app_version"],
            "max_app_version": mod["max_app_version"],
            "incompatible_reason": check_app_compatibility(
                mod["min_app_version"], mod["max_app_version"]),
            "module_id": mod["module_id"],
            "homepage": mod["homepage"],
            "license": mod["license"],
            "api_version": mod["api_version"] or REGISTRY_API_VERSION,
            "requirements": tuple(mod["requirements"] or ()),
            # Dependencies: what's missing, and whether this build can install it
            # (a PyInstaller build has no pip -- see deps.pip_available()). The
            # card turns into "needs a dependency" + an Install button rather
            # than a module that was silently skipped.
            "missing_requirements": tuple(mod.get("missing_requirements") or ()),
            "deps_installable": _deps_installable(),
            # Trust, as *derived from the signature in the module* (signing.py)
            # -- never as claimed by a store's index or by the module's own
            # constants. An unsigned module is "unverified"; a signed one whose
            # files were touched afterwards is "unverified" *and* loudly says so
            # via signature.reason, which the card renders in red.
            "trust": mod["signature"]["tier"],
            "signature": dict(mod["signature"]),
            # Named registered_items, not items -- a plain dict's own
            # .items() method shadows a same-named key under Jinja's
            # attribute-then-subscript lookup (ext.items would silently
            # return the dict method, not this list).
            "registered_items": registered_items,
        })
    return out


def _deps_installable() -> bool:
    """Whether the Install button can do anything in this build -- see
    deps.pip_available(). Cached per process: it shells out to pip, and the
    overview is rendered on every page load."""
    global _DEPS_INSTALLABLE

    if _DEPS_INSTALLABLE is None:
        try:
            from .deps import pip_available

            _DEPS_INSTALLABLE = bool(pip_available()[0])
        except Exception:
            logger.warning("[Registry] Could not determine pip availability", exc_info=True)
            _DEPS_INSTALLABLE = False
    return _DEPS_INSTALLABLE


_DEPS_INSTALLABLE = None


def dependencies_satisfied(item_id):
    """True if every item this item's requires_enabled names is itself
    currently switched on (its own enabled_setting_key == "1").

    Deliberately separate from DEPENDS_ON (web/thirdparties/__init__.py's
    module-level constant): DEPENDS_ON is a one-time, import-time check
    ("did that other folder's register(app) succeed at startup") used to
    decide load order and whether to attempt registration at all.
    requires_enabled is a live, per-request check against the dependency's
    *current* toggle state, so a dependency switched off later (no restart
    needed) is reflected immediately -- resolve_menu_items() below already
    calls this for every sidebar entry, and a module's own routes should
    call it too alongside their own enabled check wherever DEPENDS_ON alone
    wouldn't catch a dependency that's merely disabled rather than absent.

    An item_id that isn't currently registered at all is treated as
    "nothing to check" (True) rather than blocking the caller -- this
    function only ever tightens an item's own enabled_setting_key check, it
    never invents a new failure mode for a caller that didn't register any
    requires_enabled.
    """
    from ..db import get_setting

    item = get_thirdparty(item_id)
    if not item:
        return True
    for dep_id in item.get("requires_enabled", ()):
        dep = get_thirdparty(dep_id)
        if not dep or get_setting(dep["enabled_setting_key"], "0") != "1":
            return False
    return True


def admin_required_blueprints():
    """Set of Blueprint names whose routes should be wrapped with
    admin_required instead of the default login_required -- every
    registered item's auth_required="admin" resolved to its blueprint (see
    register_thirdparty's auth_required/blueprint params). Consulted by
    app.py's endpoint-wrapping pass (see _admin_only) so a thirdparty module
    can declare this instead of needing an entry hand-added to app.py's
    hardcoded set.
    """
    return {item["blueprint"] for item in _ITEMS if item.get("auth_required") == "admin" and item.get("blueprint")}


# Endpoints a module marked admin-only *per route*, either declaratively
# (register_thirdparty's admin_endpoints=) or by decorating the view with
# @module_admin_required. Blueprint-wide auth_required="admin" is still a
# separate, coarser thing (admin_required_blueprints() above) -- this is for
# the common case it can't express: a Blueprint where reading is fine for any
# logged-in user and only the writes are admin's business.
_ADMIN_ENDPOINTS = set()

# What the decorator marks a view function with. app.py's wrapping pass reads
# the attribute off the view function rather than the name, because a decorated
# view can be registered under an endpoint name the module never told us about.
_ADMIN_ATTR = "_mediaforge_admin_required"


def module_admin_required(view):
    """Mark one route as admin-only. The per-route counterpart of
    register_thirdparty(auth_required="admin"), for a module whose Blueprint
    isn't uniformly admin-only::

        from ..registry import module_admin_required

        @bp.route("/api/my_module/settings", methods=["PUT"])
        @module_admin_required
        def api_settings_put():
            ...

    This does *not* wrap the view itself -- it flags it, and app.py's single
    endpoint-wrapping pass applies the real admin_required check to it along
    with everything else. Which means the check can't be bypassed by the order
    decorators happen to be stacked in, and a module can't accidentally end up
    with admin_required *inside* login_required (i.e. never reached).

    Being a marker also makes it honest about what it can't do: a decorator
    that ran at import time couldn't see Flask's session at all.
    """
    setattr(view, _ADMIN_ATTR, True)
    return view


def is_admin_view(view) -> bool:
    """True if `view` carries @module_admin_required. Used by app.py's wrapping
    pass, which is the only thing that enforces it."""
    return bool(getattr(view, _ADMIN_ATTR, False))


def admin_required_endpoints():
    """Every endpoint name declared admin-only via register_thirdparty's
    admin_endpoints= (blueprint-qualified). app.py adds the ones marked with
    @module_admin_required to this on its own, by inspecting the view
    functions -- see is_admin_view()."""
    out = set(_ADMIN_ENDPOINTS)
    for item in _ITEMS:
        out.update(item.get("admin_endpoints") or ())
    return out


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------
#
# Every module with a bot, a poller or a sync loop used to rebuild the same
# machinery: a thread, a lock, a "has the config changed?" poll every N seconds,
# and a restart path -- which is where the bugs live (discord_request_bot held
# its own lock across a thread join and deadlocked itself on every restart).
#
# So the core owns the lifecycle instead. A module says "here is how you start
# me and how you stop me"; MediaForge decides *when*:
#
#   * enabled at startup            -> start
#   * master toggle switched on/off -> start / stop
#   * a setting the module owns changed -> stop, then start (against the new
#     value -- which is what the polling was for)
#   * module uninstalled, or MediaForge shutting down -> stop
#
# One lock per worker, held only around that worker's own start/stop, so two
# saves in a row can't overlap and nothing else in the app can be blocked by a
# module's slow shutdown.
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()
_WORKER_APP = None


def register_background_worker(item_id, start, stop, restart_on_settings=True):
    """Hand MediaForge a background worker's start/stop, and stop hand-rolling
    its lifecycle::

        def register(app):
            register_thirdparty(item_id="my_bot", ...)
            register_background_worker("my_bot", start=_start_bot, stop=_stop_bot)

    ``start(app)`` and ``stop(app)`` are called with the Flask app, never
    concurrently for the same worker, and never from a request thread (a
    settings change dispatches them onto a short-lived thread of their own --
    see web/thirdparties/__init__.py's _on_setting_changed()). Both must be
    idempotent-ish and must not raise into the caller; whatever they do raise is
    logged and swallowed, exactly like a lifecycle hook.

    ``stop(app)`` MUST NOT be a "stop and wait forever": it is what runs on
    shutdown and on uninstall. Join with a timeout; do not take a lock the
    worker thread itself needs to exit.

    - item_id: the registered item this worker belongs to. Its enable toggle is
      what gates the worker -- an item that is off, or whose requires_enabled
      isn't satisfied, has no running worker, and nothing needs to check that in
      the module.
    - restart_on_settings: restart the worker when any setting the module owns
      (``module:<module_id>:*``) changes. True is what a bot wants (the token or
      the channel changed -- reconnect). Set False for a worker that reads its
      settings on every tick anyway and would rather not be bounced.
    """
    if not callable(start) or not callable(stop):
        raise ValueError("register_background_worker: start and stop must be callables")
    with _WORKERS_LOCK:
        _WORKERS[item_id] = {
            "start": start,
            "stop": stop,
            "restart_on_settings": bool(restart_on_settings),
            "running": False,
            "lock": threading.Lock(),
        }
    logger.debug("[Registry] Registered background worker for '%s'", item_id)


def unregister_background_worker(item_id):
    """Drop a worker from the registry, stopping it first if it's running --
    called when a module is unregistered/uninstalled (see unregister_module())."""
    with _WORKERS_LOCK:
        worker = _WORKERS.pop(item_id, None)
    if worker:
        _stop_worker(item_id, worker)


def _worker_should_run(item_id) -> bool:
    """A worker runs exactly when its item is enabled and its requires_enabled
    dependencies are actually on -- the same condition that decides whether the
    item's sidebar link is shown, so "visible" and "running" can't disagree."""
    from ..db import get_setting

    item = get_thirdparty(item_id)
    if not item:
        return False
    if get_setting(item["enabled_setting_key"], "0") != "1":
        return False
    return dependencies_satisfied(item_id)


def _start_worker(item_id, worker, app):
    with worker["lock"]:
        if worker["running"]:
            return
        try:
            worker["start"](app)
            worker["running"] = True
            logger.info("[Registry] Started background worker '%s'", item_id)
        except Exception:
            logger.exception("[Registry] Background worker '%s' failed to start", item_id)


def _stop_worker(item_id, worker, app=None):
    with worker["lock"]:
        if not worker["running"]:
            return
        try:
            worker["stop"](app if app is not None else _WORKER_APP)
            logger.info("[Registry] Stopped background worker '%s'", item_id)
        except Exception:
            logger.exception("[Registry] Background worker '%s' failed to stop", item_id)
        finally:
            # Marked stopped even if stop() raised: a worker whose stop is broken
            # must not be un-stoppable forever -- the next start() gets a clean
            # slate, and the exception is in the log.
            worker["running"] = False


def _selected_workers(module_name=None, item_id=None):
    with _WORKERS_LOCK:
        items = list(_WORKERS.items())
    out = []
    for wid, worker in items:
        if item_id and wid != item_id:
            continue
        if module_name and module_name_for_item(wid) != module_name:
            continue
        out.append((wid, worker))
    return out


def sync_workers(app, module_name=None, item_id=None, restart=False):
    """Bring every (selected) worker's running state in line with its item's
    enable toggle. The single entry point for all four lifecycle triggers --
    startup, the master toggle, a settings change, shutdown.

    restart=True additionally bounces workers that are running *and* should
    still be running (restart_on_settings), which is what a settings change
    means: the worker is holding the old value.
    """
    global _WORKER_APP

    _WORKER_APP = app
    for wid, worker in _selected_workers(module_name, item_id):
        should_run = _worker_should_run(wid)
        if not should_run:
            _stop_worker(wid, worker, app)
            continue
        if worker["running"]:
            if restart and worker["restart_on_settings"]:
                _stop_worker(wid, worker, app)
                _start_worker(wid, worker, app)
            continue
        _start_worker(wid, worker, app)


def start_workers(app, module_name=None):
    """Start the workers of every enabled module -- called once at the end of
    discover_and_register(), and again for a single module that came up live
    (a store install, a dependency install)."""
    sync_workers(app, module_name=module_name, restart=False)


def stop_workers(module_name=None):
    """Stop workers -- one module's, or (no argument) all of them, which is what
    runs at shutdown via atexit. Never raises: this is the last thing that
    happens on the way out."""
    for wid, worker in _selected_workers(module_name):
        try:
            _stop_worker(wid, worker)
        except Exception:
            logger.exception("[Registry] Error stopping worker '%s'", wid)


def resolve_menu_items(section):
    """Return the currently-enabled sidebar entries for one sidebar
    category ("discover", "management", "syncplay" or "system"), ready for
    base.html's per-category loop: [{url, label, icon, page, module_name},
    ...], sorted by priority (registration order among ties). Called from
    app.py's context processors on every request.

    Every entry this returns came from a thirdparties/ module's
    register_thirdparty() call -- base.html's built-in links are hardcoded
    separately and never go through here -- so base.html renders a small
    "M" pill (see shell.css's .sidebar-module-pill) next to every one of
    these, using module_name as its tooltip, to visually tell a module's
    sidebar entry apart from MediaForge's own.
    """
    from flask import url_for
    from flask_babel import gettext as _gt
    from ..db import get_setting

    out = []
    for item in sorted(_ITEMS, key=lambda i: i["priority"]):
        if item["section"] != section or not item["endpoint"]:
            continue
        try:
            if get_setting(item["enabled_setting_key"], "0") != "1":
                continue
            if not dependencies_satisfied(item["id"]):
                continue
            out.append({
                "url": url_for(item["endpoint"]),
                "label": _gt(item["label"]),
                "icon": item["icon_svg"],
                "page": item["page_id"],
                "module_name": module_name_for_item(item["id"]) or item["id"],
            })
        except Exception:
            # A missing endpoint or a transient DB hiccup should never break
            # the sidebar for every other page.
            continue
    return out


def resolve_discover_menu_items():
    """Back-compat alias for resolve_menu_items("discover") -- kept in case
    anything outside app.py still imports the original name.
    """
    return resolve_menu_items("discover")


def _build_card(item):
    from flask_babel import gettext as _gt

    return {
        "id": item["id"],
        "title": _gt(item["label"]),
        "badges": [(_gt(text), color) for text, color in item["badges"]],
        "description": _gt(item["description"]) if item["description"] else "",
        "enable_label": _gt(item["enable_label"]),
        "enable_desc": _gt(item["enable_desc"]) if item["enable_desc"] else "",
        "extra_settings": [
            {
                "key": s["key"],
                "label": _gt(s["label"]),
                "description": _gt(s["description"]) if s.get("description") else "",
                "type": s["type"],
                "placeholder": _gt(s["placeholder"]) if s.get("placeholder") else "",
                "options": [(value, _gt(label)) for value, label in s["options"]],
            }
            for s in item.get("extra_settings", [])
        ],
    }


def resolve_card(item_id):
    """The same "card" shape resolve_settings_cards() builds, for exactly
    one item -- so a page that isn't itself a settings_host/settings_tab
    (the Modulmanager / Extensions overview, templates/extensions.html)
    can still reuse _settings_card_macro.html's render_settings_card() to
    show a fully working, in-place enable toggle (and any extra_settings
    fields) for a registered item, without duplicating that markup/JS.
    Returns None if item_id isn't currently registered.
    """
    item = get_thirdparty(item_id)
    return _build_card(item) if item else None


def resolve_settings_cards(host="integrations", tab="thirdparty"):
    """Return every *enabled* registered card targeting a given
    (settings_host, settings_tab) pair, ready for that tab/pill's template
    to render -- e.g. resolve_settings_cards("integrations", "thirdparty")
    populates the Integrations "Third Party" tab. Sorted by priority
    (registration order among ties).

    Disabled modules are filtered out here (checked live, per request --
    same read this module uses everywhere else, e.g. resolve_menu_items()):
    the Modulmanager /extensions overview (resolve_extensions_overview(),
    which lists every module regardless of state) is the canonical place
    to see and re-enable a disabled module, so a module turned off there
    disappearing from this tab too avoids a module you just disabled still
    cluttering a settings tab with nothing left to configure -- its own
    toggle (still shown by render_settings_card()) would otherwise be the
    only way back, when Modulmanager already covers that. Re-enabling a
    module (from Modulmanager or, while it's still visible here, from its
    own toggle) makes it reappear on the next request.
    """
    from ..db import get_setting
    items = sorted(
        (i for i in _ITEMS if i["settings_host"] == host and i["settings_tab"] == tab
         and get_setting(i["enabled_setting_key"], "0") == "1"),
        key=lambda i: i["priority"],
    )
    return [_build_card(item) for item in items]


def resolve_dynamic_tabs(host):
    """Return the extra tab/pill buttons a settings page needs to render on
    top of its own hand-written ones, for items whose settings_tab isn't
    one of that host's _KNOWN_TABS. One entry per distinct new tab id,
    sorted by the lowest priority among the items targeting it (ties break
    by first-registered order): [{id, label, icon_svg}, ...]. The template
    renders a button (integrations.html) or pill (notifications.html) plus
    a panel for each, then populates the panel via
    resolve_settings_cards(host, entry.id).
    """
    known = _KNOWN_TABS.get(host, ())
    tabs = {}
    for item in _ITEMS:
        if item["settings_host"] != host:
            continue
        tab = item["settings_tab"]
        if tab in known:
            continue
        existing = tabs.get(tab)
        if existing is None or item["priority"] < existing["priority"]:
            tabs[tab] = {
                "id": tab,
                "label": item["settings_tab_label"],
                "icon_svg": item["settings_tab_icon_svg"],
                "priority": item["priority"],
            }
    ordered = sorted(tabs.values(), key=lambda t: t["priority"])
    return [{"id": t["id"], "label": t["label"], "icon_svg": t["icon_svg"]} for t in ordered]


def resolve_dashboard_widgets():
    """Return the currently-enabled home-page widgets, sorted by priority:
    [{id, template}, ...]. index.html includes each entry's template
    inside its own container. See dashboard_widget_template above.
    """
    from ..db import get_setting

    out = []
    for item in sorted(_ITEMS, key=lambda i: i["priority"]):
        if not item["dashboard_widget_template"]:
            continue
        try:
            if get_setting(item["enabled_setting_key"], "0") != "1":
                continue
        except Exception:
            continue
        out.append({"id": item["id"], "template": item["dashboard_widget_template"]})
    return out


def resolve_provider_pill_scripts():
    """Return the static URLs of currently-enabled provider-pill scripts,
    sorted by priority, ready for base.html to render as <script> tags.
    See provider_pill_script above.
    """
    from ..db import get_setting

    out = []
    for item in sorted(_ITEMS, key=lambda i: i["priority"]):
        if not item["provider_pill_script"]:
            continue
        try:
            if get_setting(item["enabled_setting_key"], "0") != "1":
                continue
        except Exception:
            continue
        out.append(item["provider_pill_script"])
    return out


def register_generic_settings_routes(app):
    """One shared GET/PUT pair covering every extra_settings field type
    (see register_thirdparty's docstring) plus the master enable toggle
    every registered thirdparty gets for free. An integration that needs
    more than these field types (a test button, dynamic option lists, ...)
    can still add its own additional routes in its own routes.py -- this
    generic pair only ever touches enabled_setting_key and this item's own
    declared extra_settings keys.
    """
    from flask import jsonify, request
    from ..db import get_setting, set_setting

    @app.route("/api/settings/thirdparty/<item_id>", methods=["GET"])
    def api_thirdparty_settings_get(item_id):
        item = get_thirdparty(item_id)
        if not item:
            return jsonify({"error": "unknown"}), 404
        extra = {}
        for s in item.get("extra_settings", []):
            value = get_setting(s["key"], s.get("default", "0"))
            # A "secret" is write-only over this API: the browser only learns
            # *whether* one is set, never what it is. Anything else would put
            # the plaintext token straight into the DOM of the settings page
            # (the field is type="password", which hides it from the user's
            # eyes but not from any script on the page).
            if s["type"] == "secret":
                value = SECRET_MASK if value else ""
            extra[s["key"]] = value
        return jsonify({"enabled": get_setting(item["enabled_setting_key"], "0"), "extra": extra})

    @app.route("/api/settings/thirdparty/<item_id>", methods=["PUT"])
    def api_thirdparty_settings_put(item_id):
        item = get_thirdparty(item_id)
        if not item:
            return jsonify({"error": "unknown"}), 404
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            was_enabled = get_setting(item["enabled_setting_key"], "0") == "1"
            now_enabled = str(data["enabled"]) == "1"
            set_setting(item["enabled_setting_key"], "1" if now_enabled else "0")
            # Lifecycle hooks: on_enable(app)/on_disable(app) fire only on an
            # actual state *change*, so a module can treat them as edges
            # (start/stop a worker, clear a cache) rather than having to
            # de-duplicate repeated saves of the same value itself. Imported
            # lazily -- web/thirdparties/__init__.py imports this module, so a
            # top-level import here would be circular.
            if was_enabled != now_enabled:
                from . import fire_module_hook

                fire_module_hook(module_name_for_item(item_id),
                                 "on_enable" if now_enabled else "on_disable", app)
                # ...and the core half of the same edge: a registered background
                # worker is started/stopped to match, so a module doesn't have to
                # do it from its own on_enable/on_disable (and doesn't have to get
                # the locking right). No-op for a module without one.
                sync_workers(app, item_id=item_id)

        # Only ever writes keys this item itself registered via
        # extra_settings -- an unrecognized key in the "extra" payload is
        # silently ignored rather than allowing an arbitrary app_settings
        # write from client input. Coercion/validation depends on the
        # field's declared type.
        fields_by_key = {s["key"]: s for s in item.get("extra_settings", [])}
        for key, value in (data.get("extra") or {}).items():
            field = fields_by_key.get(key)
            if not field:
                continue
            field_type = field["type"]
            if field_type == "toggle":
                set_setting(key, "1" if str(value).lower() in ("true", "1") else "0")
            elif field_type == "number":
                try:
                    set_setting(key, str(float(value)) if value not in (None, "") else "")
                except (TypeError, ValueError):
                    continue
            elif field_type == "select":
                valid_values = {v for v, _label in field["options"]}
                if value in valid_values:
                    set_setting(key, str(value))
            elif field_type == "secret":
                # The GET route hands the browser SECRET_MASK instead of the
                # stored value, so a save of a card the user never touched
                # sends the mask straight back -- that means "leave it as it
                # is", not "overwrite my token with eight bullets". An empty
                # string is still honoured and clears the setting.
                if str(value) != SECRET_MASK:
                    set_setting(key, str(value))
            else:  # "text"
                set_setting(key, str(value).strip())
        return jsonify({"ok": True})
