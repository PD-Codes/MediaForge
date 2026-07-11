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
    "settings": ("general", "design", "sources", "downloads", "autosync", "network", "auth", "api", "updates"),
}

# Field types _build_card()/the settings-card macro/the generic PUT route
# understand for extra_settings entries. "toggle" is the original (and
# still default) boolean checkbox; the rest render as a labelled input of
# the matching HTML type plus a small inline Save button.
_FIELD_TYPES = ("toggle", "text", "number", "secret", "select")

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


def register_thirdparty(*, item_id, label, endpoint=None, icon_svg=None,
                         enabled_setting_key, badges=None, description="",
                         enable_label=None, enable_desc="",
                         page_id=None, extra_settings=None,
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
        "blueprint": resolved_blueprint,
    })


def get_thirdparty(item_id):
    for item in _ITEMS:
        if item["id"] == item_id:
            return item
    return None


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
    MediaForge compatibility range, see check_app_compatibility()), and the
    module store's identity fields module_id/homepage/license (MODULE_ID --
    the stable store id, falling back to the folder name -- plus
    MODULE_HOMEPAGE/MODULE_LICENSE, both ""). None of them are required:
    they're descriptive, except min/max_app_version, which
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
    })
    for key, value in fields.items():
        if value is not None:
            entry[key] = value


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
            # Named registered_items, not items -- a plain dict's own
            # .items() method shadows a same-named key under Jinja's
            # attribute-then-subscript lookup (ext.items would silently
            # return the dict method, not this list).
            "registered_items": registered_items,
        })
    return out


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


def resolve_menu_items(section):
    """Return the currently-enabled sidebar entries for one sidebar
    category ("discover", "management", "syncplay" or "system"), ready for
    base.html's per-category loop: [{url, label, icon, page}, ...], sorted
    by priority (registration order among ties). Called from app.py's
    context processors on every request.
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
        extra = {
            s["key"]: get_setting(s["key"], s.get("default", "0"))
            for s in item.get("extra_settings", [])
        }
        return jsonify({"enabled": get_setting(item["enabled_setting_key"], "0"), "extra": extra})

    @app.route("/api/settings/thirdparty/<item_id>", methods=["PUT"])
    def api_thirdparty_settings_put(item_id):
        item = get_thirdparty(item_id)
        if not item:
            return jsonify({"error": "unknown"}), 404
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            set_setting(item["enabled_setting_key"], "1" if str(data["enabled"]) == "1" else "0")

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
            else:  # "text" / "secret"
                set_setting(key, str(value).strip() if field_type == "text" else str(value))
        return jsonify({"ok": True})
