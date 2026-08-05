# Third-party integrations — how the plug-in system works

This document explains MediaForge's plug-in system for optional,
self-contained features (Crunchyroll-style external-API integrations, extra
Discover pages, etc.). It also explains `example_integration/` next to this
file — a complete, working, heavily commented reference implementation you can
copy as a starting point.

Read this file top to bottom once; after that it should work as a checklist.

> **Looking to restyle the UI instead of extending it?** That is a **theme
> pack**, not a module — CSS + assets only, distributed through the same store
> (`"type": "theme"` in the index), always applied live without a restart. See
> `../themes/README.md` and the `../themes/example_theme/` reference pack.

## Where modules live

**Not in the source tree.** Installed modules live in MediaForge's data
directory, next to the database and the image cache:

```
~/.mediaforge/thirdparties/<your_module>/     # your module
~/.mediaforge/thirdparties/_pending/          # staged installs, applied at the next start
```

(On Windows that is `C:\Users\<you>\.mediaforge\thirdparties\`.)

This is the whole manual-install procedure: **drop the folder in there and
restart.** The module store installs into exactly the same place — there is one
directory, and both routes lead to it.

`src/mediaforge/web/thirdparties/` is core code — `registry.py`, `store.py`,
`signing.py`, `trusted_keys.py`, `__init__.py` — and nothing else. A module
folder placed there is *ignored*: it would be inside MediaForge's program files,
where it would be wiped by the next update and, in a pip install, would be
sitting in `site-packages`. Your code is not part of MediaForge's installation.

The two directories are stitched together by one line in
`web/thirdparties/__init__.py`: the data directory is appended to the package's
`__path__`. So your module is imported as
`mediaforge.web.thirdparties.<your_module>` even though it lives outside the
source tree — which is why `from ..registry import register_thirdparty` and
`from ....logger import get_logger` work in your code exactly as they always
have. Nothing about writing a module changed; only where it is put.

Four folder names are refused, because they are the core files' own: `registry`,
`store`, `signing`, `trusted_keys`.

## The contract, in one sentence

Every folder under `~/.mediaforge/thirdparties/` that contains an `__init__.py`
exposing a `register(app)` function is imported and wired up automatically when
the app starts. Nothing else in the codebase — not `app.py`, not `base.html`,
not `integrations.html` — needs to change.

## What "automatically" means, concretely

At startup, `web/thirdparties/__init__.py`'s `discover_and_register(app)`:

1. Lists every subfolder of the module directory (skipping ones starting
   with `_`, so `_pending` and `__pycache__` are ignored).
2. Imports each one as a Python package and calls its `register(app)`.
3. Registers one shared `/api/settings/thirdparty/<id>` GET/PUT pair that
   every integration's simple enable/disable toggle uses for free (see
   "Settings and the sidebar" below).

Separately, but for the same reason, `web/thirdparties/__init__.py`'s
`discover_translation_dirs()` scans for a `translations/` folder in each
subfolder and feeds it to Flask-Babel *before* Babel initializes, so a
translation catalog someone drops into their integration folder is merged
into the app's combined catalog automatically (see "Translations" below).

Both of these are plain filesystem scans. A new integration is picked up
the moment its folder exists on disk with the right shape — no registry
file to edit, no import to add anywhere.

## Folder layout

```
~/.mediaforge/thirdparties/<your_integration>/
  __init__.py            # required — must define register(app)
  routes.py               # a Flask Blueprint with your pages/API routes
  service.py               # optional — your business logic / external API client
  templates/                # optional — your own templates (Jinja can extend "base.html")
  static/                    # optional — your own CSS/JS
  translations/               # optional — your own gettext catalog (see below)
    de/LC_MESSAGES/
      messages.po
      messages.mo
  babel.cfg                    # optional — only needed if you regenerate messages.po yourself
```

None of these files are individually mandatory except `__init__.py` with a
`register(app)` — an integration with no UI at all (e.g. a background job)
could be just that one file. `example_integration/` in this folder uses
every piece so you can see them all wired together.

## `register(app)` — what it needs to do

Two things, typically:

1. Register your Blueprint: `app.register_blueprint(bp)`.
2. Tell the shared registry about yourself: call
   `register_thirdparty(...)` from `web/thirdparties/registry.py`.

`register_thirdparty(...)` is the one call that plugs you into both the
sidebar and a settings page. Its parameters:

| Parameter              | Meaning                                                                                     |
|-------------------------|-----------------------------------------------------------------------------------------------|
| `item_id`               | Unique key, e.g. `"example_integration"`. Used in URLs, DOM ids, and the settings key prefix. |
| `label`                 | English source string for the sidebar link / card title (translated via gettext at render time). |
| `endpoint` / `icon_svg`  | Blueprint-qualified Flask endpoint for your main page (e.g. `"example_integration.index"`) plus raw `<svg>...</svg>` markup for the sidebar icon (`stroke="currentColor"`). **Both optional** — omit both if you have no page of your own (a settings-only extension, see below). Setting only one of the two raises `ValueError`. |
| `enabled_setting_key`       | The `app_settings` DB key that turns you on/off, e.g. `"example_integration_enabled"`.       |
| `badges`                     | List of `(text, css_color)` tuples shown as small pills on your settings card.                |
| `description`                  | Hint text shown at the top of your settings card.                                          |
| `enable_label` / `enable_desc`  | Label/description for the card's enable toggle. `enable_label` defaults to `"Enable {label}"`. |
| `extra_settings`                   | Optional list of additional setting fields below the master toggle — text/number/secret/select, not just booleans. See "Richer settings fields" below. |
| `section`                        | Which sidebar category your link (if any) appears under: `"discover"` (default), `"management"`, `"syncplay"` or `"system"` — matching base.html's four sidebar categories. A `"syncplay"` entry only ever renders while SyncPlay itself is enabled (`syncplay_enabled` setting), same gating as the built-in SyncPlay link. Ignored if you didn't set `endpoint`/`icon_svg`. |
| `settings_host` / `settings_tab`   | Which settings page your card is shown on: `"integrations"` (default), `"notifications"`, `"monitoring"` or `"settings"`. **Where on that page is the host's decision, not yours** — `settings_tab` is a request the host may override. See "Settings placement" below for the full picture. |
| `settings_tab_label` / `settings_tab_icon_svg` | Label (and, on the Notifications page only, icon) for the tab/pill button — only used when `settings_tab` creates a *new* tab/pill (see below). Ignored when attaching to an existing one. |
| `priority`                       | Sort key (lower = earlier) for ordering this item among *other registered items* in the same sidebar section / settings tab / set of new tabs / dashboard widgets. Never reorders MediaForge's own built-in entries. Defaults to `0`; ties keep registration order. |
| `dashboard_widget_template`          | Optional Jinja template name/path rendered as a widget on the home page. See "Dashboard widgets" below. |
| `provider_pill_script`                | Optional static URL to a small JS file that adds a provider pill to the detail modal / browse cards. See "Provider pills" below. |
| `requires_enabled`                     | Optional tuple of *other* registered item ids this one needs switched on to actually work, e.g. `requires_enabled=("anime_seasons",)`. Live, per-request check against the dependency's *current* enable toggle — unlike `DEPENDS_ON` (below), which only runs once at startup. See "Runtime dependencies (requires_enabled)" below. |
| `auth_required` / `blueprint`             | Optional access level (`None` default, or `"admin"`) applied to every route this integration's own Blueprint registers, matched by blueprint name. `blueprint` only needs setting explicitly for a settings-only integration with no `endpoint`/`icon_svg` of its own (blueprint is otherwise inferred from `endpoint`). See "Admin-only integrations (auth_required)" below. |

## Settings and the sidebar

- The sidebar entry (if you set `endpoint`/`icon_svg`) only appears while
  `enabled_setting_key` is `"1"` in the database. It disappears again the
  moment it's turned off — no restart needed, this is checked fresh on
  every request. `section` picks *which* sidebar category it appears
  under (Discover/Management/System) — nothing else needs to change to
  move it, just re-register with a different `section`.
- Your settings card automatically gets a collapsible card (title, badges,
  description, one enable toggle) on whichever tab/pill `settings_host` +
  `settings_tab` point at — you don't write any HTML or JS for this. The
  toggle reads/writes through the shared `GET/PUT
  /api/settings/thirdparty/<item_id>` endpoint, which maps straight to
  `enabled_setting_key`.
- If your integration needs *more* settings than a single on/off switch
  (an API key, extra options, ...), add your own routes in your own
  `routes.py` (see `web/thirdparties/anime_seasons/routes.py` for a real
  example that only needs the generic toggle, and `web/routes/
  integrations.py`'s Crunchyroll section for the pattern of a richer
  settings block if you need one — you'd add the extra HTML to
  `integrations.html` yourself in that case, since it's not something the
  generic card can express).
- Inside your own route handlers, gate behaviour on the same setting:
  `get_setting("example_integration_enabled", "0") == "1"`, and redirect
  or 404 if it's off — see `routes.py` in `example_integration/`.

## Settings placement — the host decides, not the module

`settings_host` picks *which page* your card shows up on. Every host renders the
same way: a vertical **floating side menu** (`.floating-side-menu`, sticky on
desktop, an off-canvas drawer on mobile) next to the page content — see
`_settings_menu.html`/`_notifications_menu.html`/`_monitoring_nav.html` and
`integrations.html`'s own in-template menu.

**Where on that page the card lands is the host's decision.** `settings_tab` is
a request, not an instruction — the placement rules live in
`web/thirdparties/registry.py`'s `_placed_tab()` and are applied at registration
time, so everything downstream sees where the card really is:

| `settings_host` | Where the card ends up | What happens to `settings_tab` |
|---|---|---|
| `"integrations"` (default) | Always the **Third Party** tab. | **Ignored**, silently. |
| `"notifications"` | Always a tab of its own in the Notifications menu. | A built-in channel id (`"discord"`, …) or the bare default is replaced by `module_<item_id>`. A genuinely custom id is kept. |
| `"monitoring"` | Always a tab of its own in the Monitoring menu. | Same rule as Notifications. |
| `"settings"` | The **Module Settings** page under the Module Manager. | Ignored there — that page groups by host, not by tab. |

The reason is the question a user actually asks. "What have my modules added to
this page?" has exactly one answer per page, and it stops being an answer the
moment a module can hide itself on the CineInfo tab or inside Telegram's panel.
A module that wants its own tab does not pick a tab id — it picks a host that
gives it one.

Nothing breaks if your module was written for the old behaviour: it still
registers fine, its card just moves to where users look for it.

On top of its own place, **every** module card is listed on the **Module
Settings** page under the Module Manager, grouped by host ("Own settings",
"Integrations", "Notifications") — the one complete list of what installed
modules can be configured to do. That is not a copy: both places render the same
card through the same generic `/api/settings/thirdparty/<id>` API, so editing
either one is the same edit.

A module tab is appended to the host's floating side menu, grouped under a
**"Modules"** heading and carrying the module **"M" pill** — plus, on hosts with
an overview grid (Settings, Monitoring), a tile there. Feed that tile with two
optional info fields:

- `overview_description` — text shown on the overview tile (defaults to
  `description`).
- `overview_icon_svg` — icon for the tile (defaults to `settings_tab_icon_svg`,
  then a generic placeholder).

`resolve_dynamic_tabs(host)` surfaces `id`, `label`, `icon_svg`, `description`,
`module_name` and `is_module` so the template can render all of these places
(menu entry, overview tile where applicable, panel). Note it has nothing left to
return for `"integrations"` — that host has no module tabs by design.

This is entirely independent of `section`/the sidebar: an integration can
have a sidebar link *and* a settings card, just a settings card (no
`endpoint`/`icon_svg` — e.g. a pure extra notification channel with
nothing to browse), or just a sidebar link (no settings beyond the
implicit enable toggle, by leaving `settings_tab` at its default).

## Dependencies between integrations

If your integration needs another one to already be registered — e.g. it
extends `anime_seasons` instead of standing alone — declare it with a
module-level `DEPENDS_ON` tuple in your `__init__.py`, naming the other
integration's folder:

```python
DEPENDS_ON = ("anime_seasons",)

def register(app) -> None:
    ...
```

`web/thirdparties/__init__.py`'s `discover_and_register()` reads this
before calling anyone's `register(app)`, and guarantees:

- Every name in `DEPENDS_ON` has its own `register(app)` attempted first
  (regardless of alphabetical folder order).
- If a declared dependency is missing, failed to import, or its own
  `register(app)` raised, your integration's `register(app)` is skipped
  entirely (with a warning in the log) instead of risking a crash from a
  half-available dependency — the rest of the app, and every *other*
  integration, keeps working regardless.

`DEPENDS_ON` is optional and defaults to `()` — most integrations don't
need it.

## Runtime dependencies (`requires_enabled`)

`DEPENDS_ON` only ever runs once, at startup: it decides load *order* and
whether `register(app)` is attempted at all. It has no opinion about
whether the dependency is still switched on ten minutes later — a module
can be enabled at startup, then have its own toggle flipped off at
runtime by an admin, and `DEPENDS_ON` will never notice.

`requires_enabled` is the live counterpart, passed to
`register_thirdparty(...)` instead of declared on the module:

```python
register_thirdparty(
    item_id="my_addon",
    ...
    requires_enabled=("anime_seasons",),
)
```

This is a tuple of *other registered item ids* (not folder names) that
must currently have `enabled_setting_key == "1"` for this item to count as
fully working. It's re-checked on every request via
`web/thirdparties/registry.py`'s `dependencies_satisfied(item_id)`:

- `resolve_menu_items()` already calls it for you — a sidebar link whose
  `requires_enabled` isn't currently met simply doesn't render, exactly as
  if this item's own toggle were off, no restart needed either way.
- The admin **Modulmanager** page shows a "Requires: ..." hint next to any
  item that declares `requires_enabled`, plus a warning banner when that
  dependency isn't currently met — so an admin can see *why* an enabled
  module might not actually be doing anything.
- Your own routes should call `registry.dependencies_satisfied(item_id)`
  too, alongside your own enabled check, for the same reason `DEPENDS_ON`
  alone doesn't catch this: a page route that stays reachable by URL even
  after its dependency got disabled should redirect/404, the same way it
  already does when its *own* toggle is off.

An `item_id` that isn't currently registered at all (typo, or that
integration's folder failed to load) is treated as "nothing to check"
rather than permanently blocking you — this only ever tightens an
already-registered dependency's enabled check, it doesn't invent a new
failure mode.

## Admin-only integrations (`auth_required`)

By default every registered integration's routes get wrapped with the same
`login_required` every other page gets (when auth is enabled at all) — any
logged-in user, not just admins, can reach them. Pass
`auth_required="admin"` to require the admin role instead, declaratively,
without needing an entry hand-added to `app.py`'s `_admin_only` set:

```python
register_thirdparty(
    item_id="my_admin_tool",
    ...
    endpoint="my_admin_tool.index",
    auth_required="admin",
)
```

This is matched by **Blueprint name**, not `item_id` — every route your
Blueprint registers (not just the one `endpoint` tracked for the sidebar
link) gets wrapped with `admin_required`, via
`web/thirdparties/registry.py`'s `admin_required_blueprints()`, consulted
by `app.py`'s endpoint-wrapping pass alongside its hardcoded admin set. The
blueprint name is inferred from `endpoint` (`endpoint.split(".")[0]`) —
you only need to pass `blueprint=` explicitly for a settings-only
integration that has no sidebar `endpoint`/`icon_svg` of its own but still
registers its own Blueprint/routes elsewhere. Passing any value other than
`"admin"` (or omitting it, the default `None`) raises `ValueError` — this
is deliberately a closed set matching what `auth.py`'s
`login_required`/`admin_required` actually distinguish today, not an
open-ended role string.

## Module metadata & the Modulmanager

Six more optional module-level constants, read the same way as
`DEPENDS_ON` (off the module itself, before `register(app)` is even
called):

```python
MODULE_NAME = "My Integration"
MODULE_DESCRIPTION = "What it does, in one sentence."
MODULE_DESCRIPTION_DE = "Was es macht, in einem Satz."  # optional
MODULE_DESCRIPTION_EN = "What it does, in one sentence."  # optional
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False
```

All six are purely descriptive except `MODULE_ENABLED_DEFAULT` — they
power the admin **Modulmanager** page (`/extensions`, linked from the
sidebar as "Module Manager"), which shows every discovered
`~/.mediaforge/thirdparties/<name>/` folder with its name, description and author,
plus a fully working enable/disable toggle for whatever it registered
(the exact same card — and the exact same toggle — that would otherwise
only be reachable by finding its tab on Integrations or Notifications;
`resolve_card()` in `registry.py` reuses `_settings_card_macro.html` so
there's no separate implementation to keep in sync).

- `MODULE_NAME` — shown as the card title instead of the raw folder name.
  Falls back to the folder name if omitted.
- `MODULE_DESCRIPTION` — shown under the title. Falls back to nothing.
- `MODULE_DESCRIPTION_DE` / `MODULE_DESCRIPTION_EN` — optional overrides of
  `MODULE_DESCRIPTION` for one specific UI language. The Modulmanager page
  picks whichever matches the admin's current language at render time
  (`registry._localized_module_description`), falling back to plain
  `MODULE_DESCRIPTION` when the current language has no override declared.
  Declare only the one(s) you need -- a module that only sets
  `MODULE_DESCRIPTION` shows that same text in every language, exactly as
  before.
- `MODULE_AUTHOR` — shown as a small badge next to the title. `"PD Codes"`
  for MediaForge's own shipped integrations (`anime_seasons`,
  `mediacalendar`); use your own name/handle for anything you write.
  Falls back to nothing (no badge shown).
- `MODULE_ENABLED_DEFAULT` — if `True`, every item this module registers
  starts enabled the very first time it's discovered, instead of the
  usual disabled-by-default. This only ever applies once: `get_setting(key,
  None) is None` is how `registry.seed_default_enabled()` tells "this
  install has never seen this setting before" apart from "the user (or a
  previous run) already turned it off" — a later `register(app)` call
  never re-flips a value that's already been explicitly set, on this run
  or any earlier one. Falls back to `False` (today's original behaviour:
  every new integration starts disabled) if omitted.

None of the six require any change anywhere else — same filesystem-scan
discovery as everything else in this document, and the same
backward-compatible fallback story as `DEPENDS_ON`: a module that
declares none of them (or was written before this convention existed)
keeps working exactly as before, just with a plainer-looking card on the
Modulmanager page.

## Versioning & module-store metadata

Six further constants, read exactly like the ones above, carry a module's
version, the MediaForge versions it works on, and the identity fields the
planned **module store** will index it by:

```python
MODULE_VERSION = "1.0.0"              # this module's own version
MODULE_API_VERSION = 1                # registry contract it was written against
MODULE_MIN_APP_VERSION = "1.1.0"      # optional; "" = no lower bound
MODULE_MAX_APP_VERSION = ""           # optional; "" = no upper bound
MODULE_REQUIREMENTS = ("icalendar>=6.0",)   # pip deps — checked, never installed
MODULE_ID = "my_integration"          # stable store id, survives a rename
MODULE_HOMEPAGE = "https://example.com/my-integration"
MODULE_LICENSE = "MIT"
```

- `MODULE_VERSION` — your module's own version, shown as a badge next to
  its name on the Modulmanager page. Bump it on every change you ship.
  Nothing compares it against anything *yet*, but it's what the module
  store will use to tell an installed module apart from a newer one on
  offer — so declare it from the start rather than retrofitting versions
  onto an already-published module. Falls back to `"0.0.0"` if omitted
  (which is exactly how an unversioned module shows up in the UI: as one
  that never declared a version).
- `MODULE_MIN_APP_VERSION` / `MODULE_MAX_APP_VERSION` — the only two here
  that do anything at load time. They declare the (inclusive) range of
  MediaForge versions your module supports, checked against the running
  app's version by `registry.check_app_compatibility()` *before*
  `register(app)` is called. If the running MediaForge falls outside the
  range, the module is skipped with that reason — the same treatment an
  unmet `DEPENDS_ON` gets, and for the same reason: better a clearly
  labelled skip on the Modulmanager page than a module half-registering
  against an API it wasn't written for. Declare a floor when you start
  using a `registry.py`/API feature that didn't exist in older
  MediaForge versions; declare a ceiling only when you actually know
  something breaks. Anything unparseable (a typo'd bound, or no installed
  version to compare against, e.g. running straight from a source tree)
  is treated as compatible rather than as a failure.
- `MODULE_ID` — the stable id the module store knows your module by, so it
  survives the folder being renamed on disk. Nothing at runtime uses it
  (the folder name is still what discovery, `DEPENDS_ON` and the log refer
  to); Modulmanager shows both when they differ. Falls back to the folder
  name.
- `MODULE_HOMEPAGE` / `MODULE_LICENSE` — purely descriptive, shown on the
  Modulmanager card. Fall back to nothing.

- `MODULE_API_VERSION` — the version of the *registry contract* this module was
  written against (`registry.py`'s `REGISTRY_API_VERSION`, currently **1**).
  This — not `MODULE_MIN_APP_VERSION` — is the number you should normally pin:
  MediaForge's own version moves for reasons that have nothing to do with
  modules, while this one only ever changes when `register_thirdparty()`, the
  field types or the hooks break in a way an older module can't survive. A
  module asking for a *newer* API than the running MediaForge provides is
  skipped with that reason; an older one keeps working. Omitted = 1.
- `MODULE_REQUIREMENTS` — pip distributions your module imports but MediaForge
  doesn't ship, as PEP 508 strings. They are **checked, never installed**:
  pip-installing into a running app's environment would mean silently upgrading
  a dependency the core also uses, and in Docker it wouldn't survive the
  container anyway. A module whose requirement is missing or too old is skipped
  with `missing dependency: icalendar>=6.0 (not installed)` on its Modulmanager
  card — which is a much better first clue than an ImportError in the log.

Same story as everything else here: all of these are optional, and a module
declaring none of them loads exactly as before — it just shows up as
`v0.0.0` with no compatibility range.

## Lifecycle hooks

`register(app)` is the only function a module *must* export. Four more are
optional, all called by `web/thirdparties/__init__.py`:

```python
def on_install(app): ...                            # first ever start on this install
def on_upgrade(app, from_version, to_version): ...  # MODULE_VERSION changed
def on_enable(app): ...                             # master toggle switched on
def on_disable(app): ...                            # master toggle switched off
```

- **`on_install` / `on_upgrade`** are driven by `MODULE_VERSION`: MediaForge
  records the version it last saw installed (per module, in the settings) and
  compares it to the version in the code on every start. Nothing recorded →
  `on_install`. Different → `on_upgrade(app, old, new)`. Same → neither is
  called. That's your migration point, and it means you don't hand-roll a
  schema-version column like `mediacalendar` had to before this existed. The
  new version is only recorded *after* the hook returns, so a hook that raised
  is retried on the next start rather than being skipped forever.
- **`on_enable` / `on_disable`** fire on the *edge* only — the admin actually
  flipping the toggle, not every save — so they can be treated as start/stop
  (spin up a worker, clear a cache) rather than "re-check whether I'm on".
- A hook that raises is logged and shown on the module's Modulmanager card, but
  never takes the app down. A broken `on_disable` that made a module impossible
  to switch off would be exactly backwards.

## Settings namespacing (and why uninstall needs it)

```python
from ..registry import module_setting_key

ENABLED_KEY  = module_setting_key(MODULE_ID, "enabled")     # module:my_integration:enabled
GREETING_KEY = module_setting_key(MODULE_ID, "greeting")    # module:my_integration:greeting
```

Flat keys (`my_integration_enabled`) still work and nothing rewrites them — but
they cannot be cleaned up. When a module is uninstalled, MediaForge deletes
every setting under `module:<MODULE_ID>:` and nothing else: there is no safe way
to guess which *flat* keys belonged to a module without deleting a core setting
that happens to start with the same word. So an un-namespaced key is one you're
choosing to leave behind on every install of your module, forever. Namespace
anything you want removable — which, for a module you intend to publish to the
store, is everything.

Data in tables your module created is deliberately **not** dropped on uninstall.
Deleting a user's calendars because they removed the module that displayed them
is not a decision MediaForge is willing to make on their behalf.

## Installing, updating, uninstalling

Dropping a folder into `web/thirdparties/` is still all it takes to install a
module by hand, and the Modulmanager's **Refresh** button picks up a brand-new
folder without a restart.

A store install downloads into `_pending/` first (that is where the signature
is verified — a package that fails it never reaches the live folder) and is then
applied **live**: a folder new to this process is moved into place and registered
(`install_staged_live()` → `rescan_new_modules()`), and an **update of a module
that is already loaded** goes through `upgrade_module_live()` — deregister, swap
the folder, register the new version. Both without a restart.

Two cases still wait for one, and the Modulmanager's "restart required" banner
says so (it lets you discard the staged folder too):

- **Another loaded module `DEPENDS_ON` the one being upgraded.** Its dependents
  imported symbols from the old module object and would keep using them; Python
  cannot rebind that, so pretending otherwise would be worse than waiting.
- **The new version refused to register.** Then it is not live *and* not
  installed: `upgrade_module_live()` rolls the working version back and puts the
  broken download in `_pending/_failed_<folder>/`, so a restart does not silently
  retry it and you can still look at what was downloaded.

An upgrade deliberately does **not** touch your settings, your data directory or
your enabled state, and does **not** fire `on_disable` — you are not being
switched off, your code is being replaced underneath you. `on_upgrade(app, from,
to)` fires as usual once the new version has registered.

### Uninstall really removes your blueprint

Flask has no `unregister_blueprint()`, so an uninstalled module's routes used to
merely be *blocked* by a guard on every request — still matched, still in the URL
map, and a reinstall of the same module could never register its blueprint again
(Flask refuses a duplicate name). `deregister_blueprint()` in
`web/thirdparties/__init__.py` now takes it off the app properly: rules, view
functions, `before_request`/`after_request`/`teardown` hooks, URL preprocessors,
error handlers, the template folder and the blueprint's static route. The URL map
is rebuilt from the surviving rules, because Werkzeug's `Map` keeps a
state-machine matcher that `Map.update()` does *not* prune — dropping entries
from `Map._rules` alone leaves the routes matchable.

What that means for you as a module author:

- **Uninstall and reinstall now work live**, in the same process. Your
  `register(app)` may run a second time in one process lifetime, so keep it
  idempotent: no "assert this only happens once", no module-level state that a
  second call would double.
- **Nested blueprints go with the parent** (they register as `parent.child`, and
  everything matches on that prefix).
- **Stop your own threads in `on_disable(app)`.** Deregistering a blueprint
  removes routes, not the work you started; the uninstall path fires
  `on_disable` *before* the files are deleted for exactly this reason.
- Blueprints found by `import_name` — not only the ones the registry knows —
  are removed. A module that registers a Blueprint and then never calls
  `register_thirdparty()` is covered too.
- Anything that cannot be removed falls back to the old block list, so its
  routes 404 either way. The guard is the fallback now, not the mechanism.

## What the Modulmanager says your module does

Every card lists what the module actually hung into the app — "1 × menu entry ·
2 × event hook · 1 × background worker" — with the capabilities that reach into
MediaForge's own work (content source, search source, hoster, notification
channel, event hook, background worker) marked in warning colour. For anything
an admin did not write themselves that is the more useful question than who
signed it, so give it an honest answer.

The counts come from `registry.module_capabilities()`, which reads the same
`item_id` convention `unregister_module()` cleans up by: **register your
provider, hoster, search source, subtitle source, mirror list, monitor site,
notification channel, event hook and background worker under the `item_id`
you passed to `register_thirdparty()`**. Use a different id and your capability is invisible on
the card *and* survives your own uninstall — the two failure modes have the same
cause. The read-only accessors behind it are
`providers.thirdparty_provider_ids()`, `search.thirdparty_search_source_ids()`,
`extractors.thirdparty_hoster_ids()`, `mirrors.thirdparty_mirror_ids()`,
`uptime_monitor.thirdparty_monitor_ids()` and
`subtitle_sources.thirdparty_subtitle_source_ids()`.

The card header shows exactly one state — Running / Off / Skipped / Error — and
sorts problems to the top. Note that **an unsigned module gets no badge**: that
is the normal state of a third-party module, and a warning on the normal case
only teaches people to ignore warnings. A signature that exists and does *not*
verify is a different matter and says so in red.

## Richer settings fields

`extra_settings` entries aren't limited to a checkbox. Each dict's `type`
(default `"toggle"`) picks the field:

```python
extra_settings=[
    {"key": "myext_show_adult", "label": "Show adult content",
     "type": "toggle", "default": "0"},
    {"key": "myext_api_key", "label": "API key", "type": "secret",
     "placeholder": "sk-...", "description": "From your account settings."},
    {"key": "myext_max_items", "label": "Max items per page",
     "type": "number", "default": "20"},
    {"key": "myext_region", "label": "Region", "type": "select",
     "default": "eu", "options": [("eu", "Europe"), ("us", "United States")]},
]
```

`"toggle"` renders as the original checkbox. `"text"`/`"secret"` render a
single-line input (`"secret"` uses `type="password"`, for API keys/tokens)
with an inline Save button; `"number"` the same as an `input[type=number]`.
`"select"` renders a dropdown from `options` (`(value, label)` tuples, or
plain strings used as both) and saves on change. All of them are read and
saved through the same generic `GET/PUT /api/settings/thirdparty/<item_id>`
pair the toggle uses — no per-integration route needed unless you need
something these four types can't express (a test-connection button,
dynamically-fetched options, ...).

## Secrets (`"secret"` fields and `MODULE_SENSITIVE_SETTINGS`)

A `"secret"` field is more than a `type="password"` input. MediaForge treats
it as a sensitive setting, exactly like its own API keys and tokens:

- **Encrypted at rest.** The value is stored encrypted in `app_settings`
  (`db.register_sensitive_keys()`, registered for you by
  `register_thirdparty()`). A value already stored in plaintext — from an
  older version of your module, say — is encrypted the next time the module
  registers. Nothing changes for your code: keep calling `get_setting()` /
  `set_setting()`, decryption is transparent.
- **Never sent back to the browser.** `GET /api/settings/thirdparty/<id>`
  returns a mask (`registry.SECRET_MASK`) once a value is set, so the token
  isn't sitting in the DOM of the settings page. A `PUT` that carries the
  mask back means "unchanged" — send `""` to clear the value. If you render
  the field on a page of your own, do the same: never put the stored secret
  into the HTML.
  The mask applies to **every key MediaForge knows to be sensitive**, not
  only to fields you declared as `type="secret"`: a key listed in
  `MODULE_SENSITIVE_SETTINGS` (or registered via `register_sensitive_keys()`)
  stays masked even if its card field is a plain `"text"` input. Reading the
  real value inside your module is unaffected — `get_setting()` decrypts as
  always. The rule is only about what leaves the server.

For a secret with **no settings-card field** — an OAuth refresh token, a
session cookie, anything your module obtains itself — declare the key in
`MODULE_SENSITIVE_SETTINGS` and it gets the same encryption:

```python
MODULE_SENSITIVE_SETTINGS = (
    module_setting_key(MODULE_ID, "refresh_token"),
)
```

## Backups (`register_backup_category`)

MediaForge's admin **Backup** tab exports settings and user data to a portable,
password-protected `.mfbackup` file (and restores it — merge or replace).

Your module needs **no extra work for its settings**: every `module:<id>:<key>`
row in `app_settings` is part of the `settings` category automatically, and your
`"secret"` fields ride along inside the password-encrypted section (they are
never written to the portable plaintext part — even if your module is disabled
at export time).

If your module owns **its own database tables**, register them as a backup
category so admins can include them:

```python
from mediaforge.web.backup import register_backup_category

# default=True → checked by default in the Backup UI
register_backup_category("my_bot", ["my_bot_items", "my_bot_state"], default=True)
```

Call it from your `register(app)`. The category id must be unique (core ids like
`settings` cannot be shadowed). Do **not** register cache/throwaway tables —
backups are meant for data worth keeping.

## Python dependencies (`MODULE_REQUIREMENTS`)

Declare what you need and stop there:

```python
MODULE_REQUIREMENTS = ("discord.py>=2.3",)
```

If it isn't installed, MediaForge doesn't silently skip your module any more:
the Modulmanager shows it as **"needs a dependency"** with an **Install** button.
That button installs the package into `~/.mediaforge/module_deps/` and registers
your module live — no restart.

What you must **not** do (and what the core now makes unnecessary):

- **Don't run pip yourself.** Especially not `pip install --target <your own
  module folder>`: that folder is what your signature is computed over, and the
  store deletes it on every upgrade. You'd break your own signature and lose the
  packages on each update.
- **Don't put anything at the front of `sys.path`.** The core appends its
  dependency directory, so MediaForge's own aiohttp/niquests/packaging always
  win an import. A module that prepends its own copies shadows them
  process-wide, for every other module too, from the moment it's first enabled.

## A place to write (`module_data_dir`)

Your module folder is read-only in spirit: it's hashed for the signature and
replaced wholesale on upgrade. Write here instead:

```python
from ..registry import module_data_dir

path = module_data_dir(MODULE_ID) / "cache.json"   # ~/.mediaforge/module_data/<id>/
```

It survives upgrades and is deleted only when the module is uninstalled.

For *work-in-progress* files -- a download being fetched, an ffmpeg pass, any
output you build up before it is complete -- use the shared scratch directory
instead, so partial files never sit on a slow or networked destination:

```python
import os
import uuid
from mediaforge.config import MEDIAFORGE_TEMP_DIR

os.makedirs(MEDIAFORGE_TEMP_DIR, exist_ok=True)   # <os-temp>/mediaforge
tmp = MEDIAFORGE_TEMP_DIR / f"{name}_{uuid.uuid4().hex[:8]}.mkv"
# ... write tmp, then move it to its destination only once it is complete
```

Always create it first: it lives on the OS temp volume, so a reboot or a tmp
cleaner may have removed it since your last run. Clean up your own temp files
on failure -- nothing sweeps this directory for you.

## Per-user UI preferences (`register_ui_pref_key`)

`set_setting()` is *instance*-wide and admin-owned. For something each user
picks for themselves — a compact-rows toggle, a preferred layout, a colour —
don't reach for `localStorage`: that is per browser, so the user loses it on
their phone, in a private window and after a cache clear. The core's own
appearance settings (theme pack, dark/light, accent) live in `user_ui_prefs`,
and your module can use the same table, the same endpoint and the same
server-side render:

```python
from mediaforge.web.db import register_ui_pref_key

def register(app):
    register_thirdparty(item_id="my_module", ...)
    # Prefix with your MODULE_ID so two modules can never collide.
    register_ui_pref_key("my_module_compact", lambda v: v in ("0", "1"))
```

Read and write it from the browser:

```js
// Rendered into every page by base.html, so it is available before first paint
const compact = (window._USER_PREFS || {}).my_module_compact === "1";

// Saving: same endpoint the core appearance settings use
fetch("/api/user/preferences", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ my_module_compact: compact ? "1" : "0" })
});
```

The validator is not optional politeness: values come back out into the page
via `window._USER_PREFS`, so keep the accepted set as small as the feature
needs. An unknown key or a value your validator rejects fails the whole
request with 400 — nothing is stored half-way. `GET /api/user/preferences`
returns the same dict for clients that need it after page load.

## A button on the home page (`register_home_panel`)

The home page has a row of buttons under the search field and **one** panel
below it whose content depends on the button — Queue, Activity, Library, plus
Storage and System for admins. `register_home_panel()` puts yours next to
them. Use it when your module has a *state* a user would otherwise open
another page to see; use a dashboard widget instead when you want your own
markup on the classic home page.

```python
from mediaforge.home_panels import register_home_panel

def _panel():
    pending = my_store.count_pending()
    return {
        # At most 6. `tone` is "", "ok", "warn" or "err".
        "stats": [{"label": "Pending", "value": str(pending),
                   "tone": "warn" if pending else ""}],
        # At most 12. `percent` draws a bar, `href` makes the whole row a link.
        "items": [{"title": job.name, "sub": job.when, "percent": job.percent,
                   "href": "/my-module", "tone": "ok"}
                  for job in my_store.recent()],
        "link": {"href": "/my-module", "label": "Open my module"},
        "empty": "Nothing pending.",
    }

def register(app):
    register_thirdparty(item_id="my_module", ...)
    register_home_panel(
        item_id="my_module",        # same id as register_thirdparty()
        panel_id="my_module",       # unique; the built-in ids are reserved
        label="My module",
        view=_panel,                # called ONLY when the panel is open
        badge=my_store.count_pending,   # optional; runs on EVERY home page load
        badge_label="{} jobs are waiting",  # what the number means (tooltip)
        badge_suffix="",            # optional unit, up to 4 chars ("%", "GB")
        badge_tone="info",          # info | err | level | muted
        admin_only=False,
        icon="M3 6h18M3 12h18M3 18h12",  # optional 24x24 SVG path data
    )
```

`badge_suffix` and `badge_tone` say what kind of number your badge is, which
is the difference between a badge people act on and a badge people ignore:

| `badge_tone` | Looks like | Use it for |
|---|---|---|
| `info` (default) | accent pill | "there is something here" — a to-do count |
| `err` | red | something is wrong and wants a human |
| `level` | grey, amber from 90, red from 95 | a percentage; pair it with `badge_suffix="%"` |
| `muted` | quiet grey | a plain total that is not a to-do list |

The built-ins use all four: Queue is `info`, System is `err`, Storage is
`level` + `"%"`, Library is `muted`. A badge without a suffix is always read
as "how many things are waiting for me" — which is wrong for a level, and was
the reason the Storage button had no badge at all before.

Five things that decide whether this behaves well:

- **Say what your badge counts.** `badge_label` becomes the button's tooltip
  and its accessible name, with `{}` replaced by the number. Skip it and the
  user sees a bare digit next to a word: the built-in System button shipped
  without one and its "58" was read as a version number and as an error code
  long before anyone worked out it counted failed downloads. Send it
  translated, like `label`.
- **`badge` must be cheap.** `view` is lazy (fetched when the user opens the
  panel, refreshed every 20 s while it is open and the tab is in front), but
  the badge runs on every home page load for every registered panel. A COUNT
  is fine; a network call is not. Badges are cached ~10 s process-wide and are
  expected to be instance-wide, not per user.
- **Send text, not markup.** Everything is escaped by the client, unknown keys
  are dropped, `percent` is clamped to 0–100, and `href` must be a
  site-relative path — an absolute or protocol-relative URL is removed, so a
  panel can never turn into an off-site redirect. For the few things that are
  modals rather than pages, use `"action": "queue"` instead of an href (there
  is no `/queue` route); the allowed set is fixed in `PANEL_ACTIONS`.
- **Translate your own strings.** The built-in panels ship i18n keys that the
  home page template resolves; the core cannot translate a string it has never
  seen, so a module sends finished text (use your own catalogue).
- **`admin_only=True` is a real gate**, checked in the list route *and* in the
  panel route: a normal account gets 403 rather than a hidden button with the
  data one fetch away.

A `view` that raises does not take the page down — the bar keeps working and
the panel reports itself unavailable. Cleanup runs through `item_id`, so
disabling the module removes the button.

## Background workers (`register_background_worker`)

Don't build a thread + lock + config-poll + restart path by hand. Hand the core
your start and stop:

```python
from ..registry import register_thirdparty, register_background_worker

def register(app):
    register_thirdparty(item_id="my_bot", ...)
    register_background_worker("my_bot", start=_start_bot, stop=_stop_bot)
```

MediaForge starts it when the module is enabled, stops it when it's disabled or
uninstalled, restarts it when a setting your module owns changes, and stops it
on shutdown. `start(app)` / `stop(app)` are never called concurrently for the
same worker and never on a request thread.

`stop(app)` must actually be able to finish: join with a timeout, and never take
a lock your own worker thread needs in order to exit.

For anything beyond "restart me", implement the hook:

```python
def on_settings_changed(app, keys):
    """A module:<MODULE_ID>:* setting was saved."""
```

## TMDB metadata (`lookup_media`)

Don't import `_tmdb_lookup_cached` — it's core-internal, and every caller that
did re-implemented the same two guards on top of it. Use the public wrapper:

```python
from ...web.tmdb_cache import lookup_media, is_tmdb_configured

info = lookup_media("Dark", media_type="tv", require_confident=True)
if info:
    tmdb_id, plot = info["tmdb_id"], info["overview"]
```

You get the metadata dict or **`None`** — no `{"found": False}` case to check.
`media_type` requires a movie/show, `require_confident=True` requires the
returned title to actually match the one you asked for (TMDB's search answers
nearly every query with *something*, so turn it on whenever you *display* the
result instead of just using the ID). The API key, provider country and UI
language are resolved for you.

Results are cached 24 h and rate-limited process-wide, so a loop is fine — but
it is blocking network I/O, so bulk lookups belong in a background worker.

## List and dict settings (`get_json_setting`)

Don't wrap `set_setting` in your own `json.dumps`/`json.loads`:

```python
from ...web.db import get_json_setting, set_json_setting

rooms = get_json_setting("module:my_mod:rooms", [])
rooms.append(name)
set_json_setting("module:my_mod:rooms", rooms)
```

A missing, empty, invalid or wrong-shaped value is logged and returns your
default, so a corrupt row reads as "unset" rather than raising inside a request.

Each key is written on its own (`set_setting` is a single-key upsert), so saving
one value can never clear another — there is no bulk "write all my settings"
call and you don't need one.

## Admin-only routes

`auth_required="admin"` is blueprint-wide. When only *some* routes are admin's
business, mark those:

```python
from ..registry import module_admin_required

@bp.route("/api/my_module/settings", methods=["PUT"])
@module_admin_required
def api_settings_put():
    ...
```

...or declare them on the registration:

```python
register_thirdparty(..., admin_endpoints=("api_settings_put",))
```

Both end up in the same enforcement pass in `app.py`. Do not hand-check
`is_admin` in the view body — that is the check everybody forgets on exactly one
route.

## API routes and CSRF

Routes whose view function is named `api_*` **and** whose URL lives under
`/api/` are exempt from CSRF token checks — that's what lets a module's own
`fetch()` calls work without a token. What protects them instead is the
JSON-only rule: MediaForge rejects any `POST`/`PUT`/`DELETE` to those routes
that doesn't declare `Content-Type: application/json`.

So mount your write routes under `/api/<your_module>/...` and always send
`Content-Type: application/json`. A route named `api_*` but mounted somewhere
else keeps full CSRF protection (and logs a warning at startup saying so) —
it would otherwise be a route with neither of the two defenses.

## Building a fully custom page (`_field_macros.html`)

`extra_settings` (above) covers "a few more fields on the generic card".
For a whole custom page/tab with its own routes and data loading — like
Media Kalender's own "Einstellungen" section, hand-built in its own JS
instead of going through the generic card — reach for
`web/templates/_field_macros.html` instead of inventing your own row
markup:

```jinja
{% import "_field_macros.html" as fields %}

{{ fields.toggle_field("myThing", _("Enable my thing"), checked=my_value) }}
{{ fields.number_field("myCount", _("How many"), value=5, min=1, max=20) }}
{{ fields.text_field("myKey", _("API key"), value=my_key, secret=True) }}
{{ fields.select_field("myMode", _("Mode"), [("a", "Option A"), ("b", "Option B")], selected=my_mode) }}

{% call fields.collapsible_card("my_module", _("My Module")) %}
  {{ fields.toggle_field("myEnabled", _("Enable my thing")) }}
{% endcall %}
```

Every macro renders the exact same `.settings-row`/`.toggle`/collapsible
card chrome as every hand-written settings page in the app (and any
`input[type=number]` it produces gets the themed +/- stepper for free, no
extra markup) — but is display-only wiring. Loading the current value and
persisting a change on `onchange="..."` is entirely up to your own
JS/routes; these macros don't assume the generic `/api/settings/
thirdparty/<id>` API `extra_settings` fields use. `collapsible_card` needs
`static/extension_cards.js` loaded on the page (already true on
integrations.html/notifications.html/settings.html/extensions.html; add
the one `<script>` tag yourself on a brand-new custom page).

## Dashboard widgets

`dashboard_widget_template` names a Jinja template (e.g. one from your own
Blueprint's `template_folder`) rendered as a widget on the home page
(`index.html`), via `{% include %}`, while `enabled_setting_key` is `"1"`.
Ordered among other widgets by `priority`. Since Flask merges every
Blueprint's `template_folder` into one global-by-filename lookup, prefix
your filename with your `item_id` (e.g. `myext_widget.html`) to avoid a
collision with another integration's widget template. Your widget's markup
is entirely up to you — nothing else about it is generic, unlike the
settings card.

## Provider pills

`provider_pill_script` names a static URL (e.g.
`url_for('your_blueprint.static', filename='pill.js')`) to a small JS file
that's included as a `<script>` on every page while `enabled_setting_key`
is `"1"`. This is the same pill slot Crunchyroll's and Fernsehserien.de's
integrations use in the detail modal / browse cards — your script just
needs to call the global `registerProviderPill(name, resolverFn)` once at
load time:

```javascript
registerProviderPill("MyProvider", async function (title, imdbId) {
  const resp = await fetch("/api/myext/availability?title=" + encodeURIComponent(title));
  const d = await resp.json();
  if (!d.available) return null;
  return { name: "MyProvider", tooltip: "Available on MyProvider" };
});
```

Resolution order is TMDB → Crunchyroll → Fernsehserien.de → registered
extensions (in registration order, first pill wins) — your resolver is
only called once all three of those came up empty for a given title, to
keep request volume down. A resolver that throws or returns
`null`/`undefined` is simply treated as "no pill"; it never blocks another
extension's resolver.

## Modulmanager (Extensions overview)

Every discovered `~/.mediaforge/thirdparties/<name>/` folder — including ones that
failed to import, have no `register(app)`, or were skipped for an unmet
`DEPENDS_ON` or an unsupported MediaForge version — shows up on the admin
**Module Manager** page (`/extensions`,
linked from the sidebar next to Integrations as "Module Manager"), with the
reason if it isn't fully loaded, its `MODULE_NAME`/`MODULE_DESCRIPTION`/
`MODULE_AUTHOR` (see "Module metadata & the Modulmanager" above), its
`MODULE_VERSION` and compatibility range (see "Versioning & module-store
metadata" above) alongside MediaForge's own version, and a
fully working enable/disable toggle (plus any `extra_settings`) for
everything it registered — the page isn't just diagnostic, it's a real
place to turn a module on/off. Nothing to opt into: this is fed by
`web/thirdparties/__init__.py`'s `discover_and_register()` automatically
(see `registry.py`'s `record_module_status()` /
`resolve_extensions_overview()` / `resolve_card()`), so it's the first
place to check if a new integration doesn't seem to be showing up
anywhere.

## Templates and static files (self-contained via Blueprint)

Use a Flask `Blueprint` with its own `template_folder` and `static_folder`
so your integration never has to put files in the shared `web/templates/`
or `web/static/` trees:

```python
bp = Blueprint(
    "example_integration", __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/example_integration/static",
)
```

Two consequences to know about:

- Inside a template rendered from one of your own view functions, use a
  **relative** `url_for()` for your own endpoints: `url_for('.index')`
  instead of `url_for('example_integration.index')` — both work, but the
  relative form is shorter and doesn't hardcode your own blueprint name.
  From *outside* your blueprint (e.g. in `registry.py`'s resolution code),
  the fully-qualified form (`"example_integration.index"`) is required.
- Your static files are served under your own `static_url_path`, as a
  distinct Flask endpoint named `<blueprint_name>.static`. Reference them
  with `url_for('example_integration.static', filename='...')`. Shared
  assets you didn't move (like `static/app.js` if you reuse its helpers,
  or `shared_modals.css`) are still referenced the normal way:
  `url_for('static', filename='app.js')`.
- Your templates can `{% extends "base.html" %}` and `{% include
  "shared_modals.html" %}` exactly like the app's own templates — those
  live in the app's template folder, which Jinja always searches first,
  regardless of which blueprint is rendering. If you include
  `shared_modals.html` (e.g. to reuse `openAniSearchModal()` from
  `app.js`), also `<link>` `shared_modals.css` — that is where the modal's
  overlay/backdrop chrome lives. Without it the overlay has no positioning
  at all and renders as a plain block below your page content, which looks
  exactly like the button doing nothing.

## Reusable UI components

MediaForge's core CSS is already loaded on every page (`base.html`'s
`<head>`) and its class names are stable, so use them instead of
inventing new ones — a new integration then looks native for free. Enable
`example_ui_components/` (section "Management" in the sidebar once
enabled) for a live, click-through gallery of all of these with the exact
markup underneath each one; the table below is the quick-reference version.
The **Defined in** column names the file and flags the few stylesheets that
are *not* loaded globally — `settings_rows.css`, `stats.css`,
`mf-charts.css` and `mf_detail_modal.css` need their own `<link>` on your
page. `mf_components.css` used to be one of them; since the queue hub (one
window for downloads/encoding/upscaling, in `base.html`) uses `.mf-progress`
and `.mf-facet` on every page, it is loaded globally now — an extra `<link>`
in your own page is harmless but no longer needed.

| Component | Classes | Defined in | Notes |
|---|---|---|---|
| Badges/tags | `.badge` + `.badge-accent`/`-success`/`-warning`/`-error`/`-neutral` | `tabs-badges.css` | `<span class="badge badge-accent">Beta</span>` |
| Service pills (in-content mode selector) | `.service-pills` (wrapper) / `.service-pill` (+ `.active`) | `settings_rows.css` | A horizontal pill row for switching between a few modes *within* one panel — e.g. Settings › Encoding's Copy/H.264/H.265/Expert/Upscaling selector (`_encoding_body.html`). For page-level sub-navigation use the floating side menu below instead — that's what `settings_host`/`settings_tab` (see "Settings placement" above) plugs your own tab into automatically. |
| Floating side menu (page sub-navigation) | `.settings-tabs.floating-side-menu` (wrapper) / `.settings-tab` (+ `.active`, + `.settings-tab-module` for module-contributed entries) on `.settings-container.has-floating-menu`; panels are `.settings-tab-panel` (+ `.active`) | `shell.css` (menu/drawer chrome) + `settings_rows.css` (tab/panel base) | The vertical sticky menu used by Settings, Integrations, Monitoring and Notifications; auto-generated for `settings_host`/`settings_tab` entries via `resolve_dynamic_tabs()`, so a third-party rarely hand-writes this — see `_settings_menu.html`/`_notifications_menu.html` if you're building a brand-new page with its own such menu. Comes with the mobile off-canvas drawer for free (see "Mobile / responsive design" below). |
| Toggle switch | `.toggle` (wrapper) / `.toggle-slider` | `tables.css` | `<label class="toggle"><input type="checkbox" .../><span class="toggle-slider"></span></label>`. Inside a settings card, add `class="thirdparty-toggle" data-thirdparty-id="..."` and it wires itself up for free — see `_settings_card_macro.html` / `static/extension_cards.js` |
| Checkbox | `chb-main` (on the `<input type="checkbox">` itself) | `forms.css` | `<input type="checkbox" class="chb-main" .../>` — MediaForge's *only* plain-checkbox style (as opposed to the on/off `.toggle` switch above): a purple accent box with an animated SVG checkmark, used everywhere from Settings to Auto-Sync to SyncPlay to custom multi-select dropdowns. Use this, not a bare unstyled `<input type="checkbox">`, for anything that reads as "check one or more of these" rather than "flip this setting on/off". Building one from JS: `el.className = "chb-main ..."` works exactly like in a template. |
| Number stepper (−/+) | *(none needed)* | `forms.css` + `number_input.js` | Any `<input type="number">` is auto-enhanced on page load (and for anything added to the DOM later) — no markup, no JS, of your own |
| Segmented buttons | `.mf-segmented` (wrapper) / `.mf-segmented-btn` (+ `.active`) | `forms.css` | Two to four mutually exclusive modes in one pill-shaped group, e.g. the Series/Movies switch on the Advanced Search. For more than four options use a `<select>`; for switching *panels* on a page use the floating side menu above |
| Multi-select dropdown | `.mf-multiselect` (+ `.is-open`) / `-trigger` / `-label` / `-dropdown` / `-item` / `-empty` | `forms.css` + `mf_multiselect.js` | A closed trigger showing a summary ("3 genres selected"), opening a checkbox list built from `.chb-main`. Put each option in a `<label class="mf-multiselect-item">` around its checkbox — then add **`data-mf-multiselect`** to the root and the behaviour is free: `mf_multiselect.js` (loaded globally from `base.html`, exposed as `window.mfMultiSelect`) handles open/close, the trigger's summary label, outside-click and Escape, and repositioning — including staying unclipped inside a scrolling container such as `.user-table-wrapper` and flipping above the trigger when there is no room below. Tune the label with `data-none-label` (nothing checked), `data-many-label` (suffix once more than `data-max-names` items are checked, e.g. "3 sites") and `data-max-names` (default 2). Listen for `mf-multiselect-change` / `mf-multiselect-close` on the root (`detail: {values, labels}`, bubbling, so you can delegate on a container) instead of raw `change`; `window.mfMultiSelect.values(root)`/`.labels(root)`/`.refresh(root)`/`.open(root)`/`.close(root)`/`.closeAll()` are there for the rest. It is delegation-based, so markup you render later from JS needs no init call. Leave the attribute off if you deliberately want to keep your own handlers |
| Token field (autocomplete + tags) | `.mf-token-field` / `.mf-token-input` / `.mf-token-suggestions` (+ `.is-open`) / `.mf-token-suggestion` (+ `.is-active`) / `.mf-token-list` / `.mf-token` / `.mf-token-remove` | `forms.css` | Text input with a suggestion list and removable tokens underneath — the Keywords / Providers / Network filters on the Advanced Search. `.mf-token-input` on its own is also the plain themed text input/`<select>` used across that page |
| Range slider | `.mf-range` (row) / `.mf-range-header` / `.mf-range-value` | `forms.css` | Wraps an `<input type="range">` in the themed track + thumb, with an optional label/value header above it |
| Filter chip | `.mf-chip` (+ `.mf-chip-static` for one without an ✕) / `.mf-chip-remove` | `forms.css` | The "active filter" pills above a result list. Delegate the click on the container and read an index/id off the button — do not build an inline `onclick` |
| Pagination | `.mf-pagination` / `-btn` / `-page` (+ `.active`) / `-ellipsis` / `-jump` | `forms.css` | First/prev, numbered pages with ellipses, next/last and a "jump to page" box. Give every clickable element a `data-page` attribute and delegate the click on the container, since the pager is re-rendered on each page change |
| Pagination bar | `.mf-pagination-bar` (wrapper, supports `[hidden]`) / `.mf-pagination-count` / `.mf-pagination-perpage` | `mf_components.css` | The row a `.mf-pagination` pager usually sits in: a "Showing X–Y of Z" count on one side, the pager in the middle, and a results-per-page `<select>` on the other — stacks to one centered column below 480px. Client-side pagination (slice an already-fetched array, e.g. Library) and server-paged tables (History) both use it; see `libRenderPagination()` in `library.js` for the reference implementation, including a 10/20/50/100 per-page `<select>` persisted to `localStorage` |
| Player controls | `.mfp-icon-btn` / `.mfp-lbl` / `.mfp-menu` (+ `.is-open`, `.is-wide`) / `.mfp-menu-item` / `.mfp-src` / `.mfp-tag` / `.mfp-health` | `player.css` | The overlay control vocabulary of the web player. Loaded globally (`base.html`), so a module that opens the player or draws a player-like surface gets them for free. `.mfp-menu` doubles as a bottom sheet on touch screens without any markup change — only the positioning switches. See "The web player (MFPlayer)" below for the JS side |
| Buttons | `.btn` + `.btn-primary`/`-secondary`/`-ghost`/`-danger`, `.btn-sm`/`-lg`, `.btn-icon` | `buttons.css` | |
| Settings row layout | `.settings-section` (card) / `.settings-row` / `-left`/`-right`/`-label`/`-desc` | `settings_rows.css` (needs its own `<link>`) | The label-left/control-right row every Settings page is built from |
| Empty state | `.empty-state` / `-icon` / `-title` / `-desc` | `feedback.css` | Centered icon+title+description for "nothing here yet" |
| Progress bar | `.progress-wrap` (track) / `.progress-bar` (fill, inline `style="width:N%"`) | `tabs-badges.css` | Prefix your own bar class instead of styling `.progress-bar` directly if several bars exist on one page at once |
| KPI card | `.stat-card` / `.stat-value` / `.stat-label` / `.stat-sub` (+ inline `style="--kpi-color:#7c3aed"` for the accent strip) | `stats.css` (needs its own `<link>`) | The metric tile the Statistics page is built from. `--kpi-color` drives the strip along the top edge and the icon tint; add `.hero-card` for the larger variant with an icon/sparkline header row (`.hero-head` / `.hero-icon`) and `.is-clickable` when the card opens something — render a clickable card as a real `<button>` so it stays keyboard reachable |
| Charts | `MFCharts` (JS) + `.mfc-chart` container | `mf-charts.js` + `mf-charts.css` (both need their own `<link>`/`<script>`) | Dependency-free inline-SVG charts — no CDN, no bundler, CSP-friendly, and painted from the `variables.css` theme tokens so theme packs restyle them for free. See "Charts (MFCharts)" below |
| Search field | `.mf-search` / `.mf-search-icon` / `input.mf-search-input` / `.mf-search-clear` | `mf_components.css` | Icon + input + clear button. Note the **element-qualified** `input.mf-search-input`: `forms.css` styles inputs via `input[type="search"]` (specificity 0,1,1), which silently beats any bare class (0,1,0). The clear button is markup only — two listeners of your own, see the gallery |
| Toolbar | `.mf-toolbar` / `-row` / `-gap` / `-sep` / `.mf-icon-btn` / `.mf-facet` | `mf_components.css` | One `-row` per job: row 1 *finds* (search, navigation), row 2 *shapes the view* (filters left, view options after a `-gap`, which pushes them to the right edge). `.mf-facet` is the count badge inside a `.mf-segmented-btn` |
| Browse card + hover drawer | `.browse-card` / `.browse-info` / `.browse-title` / `.browse-genre` / `.browse-hover-overlay` / `.browse-hover-content` / `.browse-hover-pill` (+ `--rating`/`--fsk` and `.browse-fsk-0`/`-6`/`-12`/`-16`/`-18`) / `.hover-genres` | `cards.css` | The discovery card both home pages are built from (`renderBrowseCards()` in `app.js` renders it, so a module that registers a `home_feed_source` or `search_source` gets it without writing markup). Since July 2026 the metadata does **not** float over the artwork any more: `.browse-hover-overlay` is a pure clipping box over the poster area, and `.browse-hover-content` is a drawer that slides up from the poster's bottom edge on `:hover`/`:focus-within`. FSK and rating are pills, genres are a `·`-joined text line clamped to two lines — at 140px card width a pill per genre wraps one-per-row and buries the poster. Colours come from the classes, never from inline styles, so a theme pack can restyle them. Under `@media (hover:none)` the drawer rests open, so the badges are reachable on a phone at all |
| Poster grid | `.mf-poster-grid` / `.mf-poster-card` (+ `.is-selected`) / `-art` / `-flag` (+ `--pending`/`--approved`/`--partial`/`--done`) / `-scrim` / `-meta` / `-title` / `-foot` / `-who` / `-when` / `-actions` / `-select` | `mf_components.css` | Responsive 2:3 poster grid. Status lives in the corner **flag** instead of another pill, attribution in the **foot** — outside the hover overlay, because touch has no hover; `@media (hover:none)` folds the actions out below the card |
| Type pill / volume tag | `.mf-type-pill` (+ `--series`/`--movie`/`--outline`) / `.mf-vol-tag` | `mf_components.css` | A small inline badge for "what kind of item is this" (`.mf-type-pill`) and "which source location did it come from" (`.mf-vol-tag`, folder icon + label, label text hides below 480px). Drop either into a `.mf-poster-meta` line, a list row, or a detail header — not poster-grid specific. Introduced for the Library page's flattened, multi-volume view |
| Poster progress / watched badge | `.mf-poster-progress` / `-fill` / `.mf-poster-watched` (+ `.mf-poster-card.is-watched`) | `mf_components.css` | A thin playback-progress bar along a poster card's bottom edge plus a small watched checkmark badge, for when a card already knows a single completion percentage without the user opening it (e.g. Library's eager per-movie progress prefetch) |
| Avatar | `.mf-avatar` (+ `.mf-avatar--sm`) | `mf_components.css` | Initials disc for "who asked for this" |
| Timeline | `.mf-timeline` / `.mf-stop` (+ `.is-now`) / `-dot` / `-when` / `-rel` / `-day` / `-mon` / `-item` / `-source` / `-thumb` / `-text` / `-title` / `-sub` / `-badge` / `-count` / `.mf-timeline-gap` | `mf_components.css` | Continuous rail with a glowing "now" dot and **named gaps** ("3 days with nothing") rather than two rows sitting next to each other. `-source` is a 3px coloured spine, not another coloured word next to the title. Week and Agenda on the Calendar page are the same renderer |
| Progress track | `.mf-progress` / `-step` (+ `.is-done`/`.is-active`) / `-bar` / `-label` | `mf_components.css` | A few *named* stages of one item (requested → approved → downloaded). For a percentage use `.progress-wrap` above. Hide it below ~1000px or the labels become stumps |
| Inline empty state | `.mf-empty` (+ `.is-error`) / `-icon` / `-hint` | `mf_components.css` | "Your filters match nothing" *inside* a working view — as opposed to `.empty-state` above, the big centred block for a page with no data at all. Keep the two messages distinct; only one of them deserves a reset button |
| Icons | *(convention, not a class)* | — | Inline `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` — no sprite sheet, `stroke="currentColor"` is what makes it follow theme/text color automatically |
| TMDB detail modal | `MFDetailModal` (JS) + `{% include "mf_detail_modal.html" %}` | `mf_detail_modal.js` + `mf_detail_modal.css` (both need their own `<link>`/`<script>`) | A ready-made "what is this title" modal driven by a TMDB id. See "TMDB detail modal (MFDetailModal)" below |

### TMDB detail modal (MFDetailModal)

`templates/mf_detail_modal.html` + `web/static/mf_detail_modal.js` +
`mf_detail_modal.css` are a drop-in detail view for anything you can name
by TMDB id: poster, headline, date, synopsis, metadata chips (rating,
season count, runtime, genres, status) and a "Search streams" button.

It is deliberately **not** the big series modal from
`shared_modals.html` — that one starts from a *provider URL* and drags in
all of `app.js`. This one starts from a TMDB id and has no dependencies
beyond its own two files, which is exactly the shape a module usually
needs. The Calendar page uses it for every event.

Include it once in your template and call it from your click handler:

```jinja
{% block styles %}
  <link rel="stylesheet" href="{{ url_for('static', filename='mf_detail_modal.css') }}">
{% endblock %}

{% block content %}
  ...
  {% include "mf_detail_modal.html" %}
{% endblock %}

{% block scripts %}
  <script src="{{ url_for('static', filename='mf_detail_modal.js') }}"></script>
  <script src="{{ url_for('static', filename='my_module.js') }}"></script>
{% endblock %}
```

```js
MFDetailModal.open({
  tmdbId: 1396,             // omit to skip the TMDB fetch entirely
  mediaType: "tv",          // "tv" | "movie" (default "tv")
  title: "Breaking Bad",
  subtitle: "S01E01",       // small badge next to the date
  caption: "Pilot",         // episode name / one-liner
  date: "2008-01-20",       // ISO day, rendered in the user's locale
  image: "/poster.jpg",     // already proxied if it is remote
  searchTitle: "Breaking Bad",   // optional, defaults to title
});
MFDetailModal.close();
```

Notes:

- The synopsis and chips come from `GET /api/tmdb/details`, so a **TMDB
  API key** must be configured (Integrations → CineInfo). Without one, the
  modal still shows whatever you passed in and reports that no further
  details are available — it does not error out.
- Everything you pass is escaped, and `image` is URL-scheme-checked, so it
  is safe to hand it values that came from a remote API.
- Building markup yourself? Use `window.mfEscape(value)` for anything that
  goes into HTML — it is quote-safe, so it also covers attributes
  (`title="..."`, `data-x="..."`), and `window.mfSafeUrl(value)` for `href`
  and `src`, which drops everything that is not http(s) or same-origin. Both
  are loaded on every page from `static/mf_escape.js`; do not write your own
  escaper. Better still, put values in `data-*` attributes and read them in
  the handler instead of interpolating them into an `onclick` string.
- Requests are abortable: opening a second entry while the first is still
  loading cannot let the slower answer overwrite the newer one.
- The "Search streams" button uses `openAniSearchModal()` when the host
  page loads `app.js` *and* includes `shared_modals.html`, and otherwise
  navigates to the home search — so it works either way. The Calendar page
  links both, which is why its search results open in place; do the same on
  your own page if you want that flow instead of a page change.
- Close/Escape/backdrop-click are wired by the component itself. Do not
  add your own handlers for them.

### Layout blocks (mf_components.css)

`web/static/mf_components.css` holds the building blocks the July 2026
redesign is made of. The split is worth internalising:

- **`forms.css` = form-control vocabulary** — `.chb-main`, `.mf-segmented`,
  `.mf-multiselect`, `.mf-token-field`, `.mf-range`, `.mf-chip`,
  `.mf-pagination`. Loaded globally. Two of these also come with a
  globally loaded script, so you only render markup: `number_input.js`
  enhances every `<input type="number">` into a −/+ stepper, and
  `mf_multiselect.js` drives any `.mf-multiselect` carrying
  `data-mf-multiselect` (see the component table above).
- **`mf_components.css` = layout and content vocabulary** — `.mf-search`,
  `.mf-toolbar`, `.mf-poster-grid`, `.mf-type-pill`, `.mf-vol-tag`,
  `.mf-poster-progress`/`.mf-poster-watched`, `.mf-avatar`, `.mf-timeline`,
  `.mf-progress`, `.mf-empty`, `.mf-pagination-bar`. Loaded globally since
  the queue hub landed (July 2026); no `<link>` of your own required.
- **`variables.css` = the palette** — never hardcode a colour. Text and
  surfaces come from `--text-primary`/`--text-muted`/`--bg-card`/`--border`,
  and a status pill uses the pair `--success` + `--success-bg` (same for
  `--warning`, `--error`, `--info`). Both halves are defined per theme, so a
  hardcoded hex is a badge that only works in dark mode — exactly what
  notifications.css did until July 2026.

The Calendar, Seerr and Library pages are built out of these, and
`example_ui_components/` shows every one of them live with the markup
underneath. The exact class lists are in the table above; what is worth
knowing beyond them:

- **`input.mf-search-input` is element-qualified on purpose.** `forms.css`
  styles inputs via `input[type="search"]` — specificity 0,1,1, which beats
  any bare class (0,1,0). A plain `.my-input { padding: … }` is therefore
  **silently ignored**, app-wide. Qualifying your class with the element
  matches that specificity, and loading later wins. Same trick applies
  anywhere you restyle an input.
- **Toolbars get one row per job.** `.mf-toolbar-row` 1 finds, row 2 shapes
  the view; `.mf-toolbar-gap` is the "everything after me goes right"
  spacer. Cramming five equal control groups into one row is what made the
  old toolbars read as noise.
- **Attribution belongs outside the hover overlay.** `.mf-poster-foot` is
  always visible for exactly that reason: there is no hover on touch, and
  "who asked for this" is what people scan a request list for. The
  `.mf-poster-actions` overlay is secondary and folds out under the card
  when `@media (hover: none)` applies.
- **Status goes in the corner flag,** not into yet another pill in a pile
  of pills — `.mf-poster-flag--pending/--approved/--partial/--done` (and
  `--new` for "recently added", used by the Library page).
- **`.mf-poster-scrim` stacks its children in a column,** bottom-anchored —
  put `.mf-poster-meta` (small caption line; drop a `.mf-type-pill` and/or
  `.mf-vol-tag` in it) above `.mf-poster-title`, not side by side.
- **Name your gaps.** `.mf-timeline-gap` exists so a fortnight with nothing
  scheduled reads as a fortnight with nothing scheduled, instead of two
  adjacent rows implying the entries are consecutive.
- **The clear button and the timeline are markup, not behaviour.** These are
  CSS components: no JS ships with them, so wire your own listeners (the
  gallery page shows the two-liner for `.mf-search-clear`).
- Every colour comes from the `variables.css` tokens, so a user's theme
  pack restyles your page for free — do not hardcode hex values next to
  them.

### The web player (MFPlayer)

`web/static/player.js` + `player.css` are the in-browser player. Both are
loaded globally by `base.html` and the modal markup lives there too, so any
page — including yours — can start playback without shipping a player.

```js
// A file from the library (an absolute path inside a configured root).
MFPlayer.open(path, "Title", startSeconds, { subtitle: "S01E04" });

// Direct Play: stream straight from a provider, no download.
// The last argument is the full {language: [hoster, ...]} matrix, i.e. the
// body of GET /api/providers?url=... — it is what fills the source picker.
MFPlayer.openSource(episodeUrl, "Title", "VOE", "German Dub", 0, null, null, matrix);

MFPlayer.close();
MFPlayer.skip(-30);                 // seconds, negative = backwards
MFPlayer.getState();                // {open, position, duration, paused, ...}
MFPlayer.setChapters([{ start: 0, end: 90, title: "Intro" }]);
MFPlayer.setMarkers([{ start: 20, end: 110, kind: "intro" }]);
MFPlayer.setNextUp({ path: "/media/next.mkv", title: "Episode 5" });
```

**Tell the player what comes next.** The player has no idea what a "next
episode" is — the page that opened it does. Register a resolver and both the
"Up next" card and the `N` key work:

```js
// Called with {path} for a library file, or {url, language, provider} for
// Direct Play. Return the same shape, or null when there is nothing after.
window.mfPlayerResolveNext = function (current) {
  const next = myList[myList.findIndex((e) => e.path === current.path) + 1];
  return next ? { path: next.path, title: next.title } : null;
};
```

Only one resolver can be active at a time, so set it on your own page (not
globally from a module that runs everywhere) and let the built-in pages keep
theirs.

**Endpoints the player uses** — useful if you are building something adjacent
rather than reusing the player itself:

| Endpoint | Purpose |
|---|---|
| `POST /api/stream/start` | start a transcode of a library file. Accepts `audio_index`, `quality` (`auto`/`1080`/`720`/`480`/`360`) and `burn_subtitle`; answers with `audio_tracks`, `subtitle_tracks`, `qualities` and `chapters` |
| `POST /api/stream/start-source` | the same for a provider URL |
| `POST /api/stream/start-proxy` | pass the provider's own HLS through without FFmpeg |
| `GET /api/stream/subtitle?path=&track=` | one embedded text track as WebVTT |
| `GET /api/stream/thumbs?path=` | seek-preview sprite sheets (built in the background; answers `{pending:true}` until ready) |
| `GET /api/stream/markers?url=` | intro/outro markers for an episode |
| `POST /api/stream/probe-source` | measure one hoster: reachable, and how fast |

Two things to know before you build on this:

- **Audio track, quality and burned-in subtitles restart FFmpeg.** They are
  encoder settings, not switches in a running output. The player keeps the
  position across the restart; anything you build on top has to expect a
  short gap.
- **`path` is validated against the library roots** on every one of these
  endpoints. A path outside them answers 404 — do not try to hand the player
  an arbitrary file, it is a deliberate limit and not a bug to route around.

### The eBook reader (MFReader)

`web/static/reader.js` + `reader.css` are the in-browser book reader, and like
the player they are loaded globally by `base.html` with their markup already in
the page — so a module can open a book without shipping a reader.

```js
MFReader.open({
  path: "/books/Knaak/Der Tag des Drachen.azw3",  // a file inside a library root
  ext: "azw3",                                    // epub | pdf | mobi | azw3 | azw
  title: "Der Tag des Drachen",
  bookKey: "der tag des drachen|richard a knaak"  // see below
});

MFReader.close();
MFReader.isOpen();
MFReader.getState();      // {open, kind, bookKey, title, location, percent, chapter}
MFReader.bookmarks();     // [{location, kind, label, percent, created_at}, ...]
MFReader.toggleBookmark();
```

**`bookKey`, not `path`, is the identity.** The same novel routinely exists as
EPUB, MOBI and PDF at once, and the library merges those files into one entry
(`web/books/identity.py`). Reading position and bookmarks are stored against
that key, which is what lets someone start in the EPUB and carry on in the PDF.
Pass the `key` field the library API reports for the book; falling back to the
file path works but gives each format its own place in the book.

**MOBI, AZW3 and AZW are converted server-side before they can be shown.** No
browser renders Mobipocket. `MFReader.open()` handles this itself — it polls
`/api/library/book/convert` and shows what it is waiting for — but a module that
builds its own reading surface has to expect the first open of such a file to
take a second or two.

| Endpoint | Purpose |
|---|---|
| `GET /api/library/book/file?path=` | the book itself, validated against the library roots |
| `GET /api/library/book/cover?path=` | the cover next to it |
| `GET /api/library/book/convert?path=` | ask for the EPUB of a Kindle file; answers `{pending}`, `{ready, key}` or `{failed, reason}` |
| `GET /api/library/book/converted/<key>.epub` | the finished conversion |
| `GET`/`POST` `/api/reading/get`,`/save`,`/bulk`,`/reset` | reading position, keyed by book |
| `GET /api/reading/bookmarks?book=` | the bookmarks of one book |
| `POST /api/reading/bookmark` / `/bookmark/delete` | set and drop one, by `{book, location}` |

Two things to know before you build on this:

- **A location is engine-specific.** An EPUB position is a CFI, a PDF position
  is a page number, and a CFI from one file is meaningless in another rendering
  of the same book — which is why bookmarks carry a `kind` and the reader only
  offers the ones it can actually jump to. Losing a position is always
  preferable to refusing to open the book.
- **`path` is validated against the library roots**, exactly as the player's
  endpoints are. A path outside them answers 404.

### Charts (MFCharts)

`web/static/mf-charts.js` + `mf-charts.css` are MediaForge's own chart
primitives. They are **not** loaded globally by `base.html`, so link them
yourself on any page that draws a chart — `mf-charts.css` in your
`{% block styles %}` and `mf-charts.js` in `{% block scripts %}` *before*
your own script. `example_ui_components/` has a live gallery of every
chart type with copyable code.

Why hand-rolled rather than Chart.js: MediaForge ships offline-capable and
CSP-friendly (no CDN), and the charts paint with `var(--...)` tokens, so a
user's [theme pack] restyles them without any work on your side.

```js
MFCharts.render("myChart", {
  type: "area",              // area | line | bars | donut | gauge | heatmap
  height: 220,
  labels: ["Mon", "Tue", "Wed"],
  series: [{ name: "Downloads", values: [3, 7, 5], color: "#7c3aed" }],
  valueFmt: function (v) { return v.toFixed(0); },
  empty: "No data",          // shown instead of an empty plot
});
```

| Call | What it does |
|---|---|
| `MFCharts.render(elOrId, spec)` | Mounts a chart and re-renders it at the new pixel width whenever the container resizes (so stroke widths and labels stay constant across breakpoints instead of being scaled) |
| `MFCharts.place(id, spec)` | Returns a placeholder `<div>` and remembers the spec — use this when you build your page as one HTML string |
| `MFCharts.renderAll(root)` | Mounts every placeholder created by `place()` in one pass, after you assign `innerHTML` |
| `MFCharts.sparkline(values, {color, width, height})` | Returns a tiny standalone SVG string for a KPI card — no mounting, no observer |
| `MFCharts.destroy(elOrId)` | Detaches the resize observer (call it if you tear a chart's container down yourself) |
| `MFCharts.palette` | The default categorical color array, if you want your own series to match the built-in pages |

Notes:

- **Bars can be horizontal** (`horizontal: true`), which renders as HTML
  rather than SVG so long category labels truncate with normal CSS.
- **Tooltips are delegated globally** — one listener for the whole page,
  driven by a `data-mfc-tip` attribute, so a 500-bar chart still costs one
  handler. Anything you pass through `valueFmt`/labels is escaped.
- **`empty:`** is the text shown when a series has no data. Always set it;
  the fallback is a bare dashed box.
- **Touch:** hover-only affordances are neutralised under
  `@media (hover: none)`, and every bar/slice/cell is its own tap target.

## Mobile / responsive design

MediaForge's own pages are fully responsive, and a third-party page built
from the shared components above (`.browse-card`, `.settings-row`,
`.service-pills`, ...) already inherits their mobile behavior — this
section is about the handful of things that need a deliberate choice on
your part, not something CSS gives you for free.

- **Test at a phone width, not just a shrunk browser window.** The core
  breakpoint used throughout `web/static/*.css` is `@media (max-width:
  640px)` — a card, table or settings row that looks fine at 900px can
  still overflow or wrap badly at 375px. `example_ui_components/`'s live
  gallery (see "Reusable UI components" above) is already responsive at
  every width, so resizing it is a fast way to sanity-check you're seeing
  the same behavior on your own markup.
- **A brand-new settings/dashboard tab gets the off-canvas drawer for
  free.** Since the July 2026 menu rework, any page with a
  `.floating-side-menu` (which `resolve_dynamic_tabs()` renders
  automatically for a brand-new tab — see "Settings placement" above) gets
  a fixed top-right FAB on mobile that opens the menu as an off-canvas
  drawer instead of a fixed sidebar; nothing to build for this. It only
  applies to the *sub-navigation* chrome, not your panel's own content —
  the content itself still needs to reflow at 640px like any other page.
- **`.browse-card`-based grids reflow on their own** (the grid's own
  `auto-fill`/`minmax` sizing collapses to fewer columns, then one column,
  as the viewport shrinks) — don't fix a column count or a fixed card
  width in your own CSS, or you'll fight the built-in reflow instead of
  getting it for free.
- **Touch targets.** A `.btn`/`.btn-icon`/`chb-main`/`.toggle` is already
  sized for touch; if you add a custom clickable element that isn't one of
  these, keep it at least ~40×40px so it's usable without zooming on a
  phone.
- **Don't assume hover is available.** A tooltip, badge, or action that
  only appears `:hover` is unreachable on a touch device — pair it with a
  tap/click state, or make the information visible without hovering at
  all.

If your integration's page does none of the above (plain settings card
only, no page of your own) there is nothing extra to do — the generic
card/toggle/field macros are already responsive.

## Translations (optional, modular)

If your integration introduces new UI strings, you don't need to touch
`web/translations/`. Instead:

1. Create `translations/de/LC_MESSAGES/messages.po` inside your folder,
   with the same `msgid "English text"` / `msgstr "German text"` format as
   the main catalog. Only include strings *you* introduce — you can freely
   reuse existing strings from the core catalog (e.g. `_('Close')`) in your
   templates without redefining them; they're already translated there.
2. Compile it: `pybabel compile -d src/mediaforge/web/thirdparties/
   <your_integration>/translations -f`. This produces `messages.mo`,
   the binary form Flask-Babel actually loads (the `.po` is source, the
   `.mo` is what ships and what the app reads at runtime — both need to
   exist and stay in sync).
3. That's it. `discover_translation_dirs()` finds your `translations/`
   folder automatically and merges it into the combined catalog the next
   time the app starts.

If you use `_gt = flask_babel.gettext` from Python (not just Jinja's
`{{ _(...) }}`) — e.g. to translate something server-side, like a season
name interpolated into a label — the string still has to exist as a
`msgid` in your `.po` file; `pybabel extract` would normally find these
calls for you automatically if you scope a `babel.cfg` to your own folder
(see the one next to this README) and run `pybabel extract -F
~/.mediaforge/thirdparties/<your_integration>/babel.cfg -o messages.pot
~/.mediaforge/thirdparties/<your_integration>`, then `pybabel update`
against your `.po`. In practice, for a small integration, hand-editing the
`.po` file directly (like `example_integration/translations/...` does) is
usually faster than running the extract/update pipeline.

## Packaging

Nothing to do. A module is not part of MediaForge's build any more: it lives in
the data directory, so it is neither shipped in the wheel nor wiped by an
update. To hand it to someone else, zip the folder as a `.mfmod` (see
`MediaForge_Modulestore`'s `mfstore pack`) or upload it to the module store —
which puts it in the same `~/.mediaforge/thirdparties/` on their machine.

## How to actually create a new integration

1. Pick the closest-matching example from "Reference implementations, by
   pattern" above and copy *that* folder (next to this README) to
   `~/.mediaforge/thirdparties/<your_name>/` — `example_integration/`
   for anything with a real page, `example_attach_tab/` or
   `example_new_tab/` for a settings-only extension with no page.
   (Developing from a git checkout? Symlink your working copy into
   `~/.mediaforge/thirdparties/` — it is imported from wherever the link
   points, and you keep editing in your repo.)
2. Rename the Blueprint name (`"example_integration"` → `"<your_name>"`)
   everywhere it appears: `routes.py`'s `Blueprint(...)` call, every
   `url_for(...)` reference in the templates, `static_url_path`, and the
   `item_id`/`enabled_setting_key`/`endpoint` values passed to
   `register_thirdparty(...)` in `__init__.py`. While you're there, also
   pick `section` (which sidebar category, if any) and `settings_host`/
   `settings_tab` (which settings page/tab, existing or new) — see
   "Settings placement" below; the defaults reproduce the original
   Discover-link + Third-Party-tab behaviour if you don't need anything
   else.
3. Replace `service.py`'s placeholder logic with whatever your integration
   actually does.
4. Replace the template content, CSS and JS with your real UI. Keep using
   `.browse-card` / `.settings-row` / etc. (the app's existing shared CSS
   classes, defined in `web/static/cards.css` and friends, loaded globally
   via `base.html`) rather than inventing new layout primitives where an
   existing one already does the job — this keeps new integrations
   visually consistent with the rest of the app for free.
5. If you introduce new UI strings, add them to your own
   `translations/de/LC_MESSAGES/messages.po` and compile it (see
   "Translations" above). Reuse existing strings verbatim where possible.
6. Start the app. Check the log for `[Thirdparties] Registered
   integration: <your_name>` — if it's missing, check for a `[Thirdparties]
   Failed to import` or `has no register(app) callable` warning instead;
   both mean `register(app)` either isn't defined or raised an exception,
   and the rest of the app keeps running regardless (one broken
   integration never takes down the others or the core app).
7. Enable it in Settings → Integrations → Third Party. The sidebar entry
   appears immediately (no restart).

## Reference implementations, by pattern

The folders here demonstrate the same contract at different scales. Start
with whichever one matches what you're building — each is small enough to
read top to bottom in a few minutes.

| Folder | Sidebar item? | Settings card? | What it shows |
|---|---|---|---|
| `example_own_menu/` | Own page, `section="management"` | Just the implicit enable toggle | The *smallest* "own page" integration: one Blueprint, one route, one template. Start here if you're adding something browsable. |
| `example_integration/` | Own page, `section="discover"` | Extra `select` field, on the shared "Third Party" tab | The same pattern as `example_own_menu/`, at real-integration scale: caching (`provider_cache`), `extra_settings`, a translation catalog, its own CSS/JS. Copy this one as your starting point for anything non-trivial. |
| `example_attach_tab/` | None | One extra toggle, appended into the *existing* Notifications → ntfy pill | The smallest "settings-only" integration — a single `__init__.py`, no Blueprint at all. Start here if you're adding one or two options to something conceptually already covered by an existing tab. |
| `example_new_tab/` | None | Its own *brand-new* tab on the Integrations page | Same "settings-only, no Blueprint" shape as `example_attach_tab/`, but `settings_tab` doesn't match an existing id, so it gets a dedicated tab instead of attaching to one. Start here if your settings don't belong inside any existing tab. |
| `example_advanced/` | Own page, `section="syncplay"` | Just the implicit enable toggle, on a *brand-new* Settings-page tab (`settings_host="settings"`) | `requires_enabled` (soft runtime dependency on `example_own_menu`) and `auth_required="admin"` (admin-only routes) together, plus placing a link under the SyncPlay sidebar category instead of Discover/Management/System. Start here for anything SyncPlay-adjacent, Settings-hosted, dependent on another integration, or admin-only. |
| `src/mediaforge/web/thirdparties/anime_seasons/` | Own page, `section="discover"` | Extra `toggle` field, on the shared tab | A real, shipped integration (fetches seasonal anime listings from the Jikan/MyAnimeList API) — external HTTP calls with rate-limiting, a persistent cache, and a richer page (a season picker plus a card grid reusing the app's existing browse-card enrichment pipeline). Read this once you've outgrown the demo examples. |
| `example_ui_components/` | Own page, `section="management"` | Just the implicit enable toggle | Not a placement pattern — a live, click-through gallery of the core UI classes from "Reusable UI components" above, with copyable markup under each one. Enable it and browse it whenever you're building a new page and want it to look native. |
| `example_content_source/` | None | Its own dynamic tab | Not a sidebar/settings placement pattern — registers a whole demo streaming site (`register_provider` + `register_search_source` + `register_home_feed_source` + `register_site_mirrors` + `register_monitor_site` together), fully offline (`.invalid` domain, no network calls). Start here if you're adding a new streaming site as a module — see "Content sources" below. |
| `example_subtitle_source/` | None | Just the implicit enable toggle, on the shared tab | Settings-only again, but for `register_subtitle_source`: one demo external subtitle source, hooked into the last step of the download path's subtitle chain next to the built-in OpenSubtitles lookup. Offline-safe (it only logs and returns `[]`). Start here if your module should supply subtitles — see "Subtitle sources" below. |
| `example_hooks/` | None | Just the implicit enable toggle, on the shared tab | The smallest "settings-only" shape again, but for `register_notification_channel` + `register_event_hook` instead of `extra_settings` — both just log. Start here if your module needs to react to a download/AutoSync event instead of adding a settings field. |

`example_own_menu/` vs. `example_attach_tab/` / `example_new_tab/` is the
"eigenes Menü" vs. "eigener Tab" choice mentioned earlier in this
document: does your integration need a page of its own (own menu entry,
own Blueprint, own route), or is it just a knob on something that already
exists (settings card only, no Blueprint, no page)? Both are first-class —
neither is a fallback for the other — and `settings_tab` further splits
the second case into "attach to an existing tab" vs. "get a new one",
independently of whether you also have a sidebar item.

## Design rationale (why it's built this way)

- **Filesystem-scan discovery instead of a manifest file.** A folder
  either has a working `register(app)` or it doesn't; there's no separate
  list that can drift out of sync with what's actually on disk.
- **One `register_thirdparty(...)` call instead of separate sidebar/
  settings-card registration functions.** An integration that's visible in
  the sidebar should also be visible in Settings — coupling them in one
  call makes the "half-registered" state (shows in one place but not the
  other) impossible to create by accident.
- **A Blueprint per integration instead of shared `web/templates/` /
  `web/static/` folders.** Keeps the "copy one folder, get a working
  integration" promise literally true — nothing to also copy into the
  shared trees, no filename collisions to worry about with the next
  integration.
- **Per-integration translation catalogs merged via
  `BABEL_TRANSLATION_DIRECTORIES`, instead of one shared catalog everyone
  edits.** The same self-containment argument as templates/static: a
  integration's strings live and travel with its folder. The trade-off is
  that very generic strings (e.g. "Close", "Loading…") are best reused
  from the core catalog rather than redefined per-integration, since
  duplicate `msgid`s across catalogs resolve to whichever directory was
  merged last (directories are merged in the order
  `discover_translation_dirs()` returns them — alphabetical by folder
  name) rather than raising an error.
- **A generic `/api/settings/thirdparty/<id>` toggle instead of every
  integration writing its own settings GET/PUT.** Covers the common case
  (just an on/off switch) with zero backend code per integration; an
  integration that genuinely needs more still can, by adding its own
  routes alongside the generic one.
- **Every load-status detail recorded, not just successes.** `_MODULES`
  tracks every discovered folder — including the ones that never made it
  to `register_thirdparty()` — specifically so the Extensions overview
  page can answer "why isn't this showing up" without anyone needing
  server-log access.
- **`priority` instead of registration order deciding layout.** Discovery
  order is alphabetical-then-dependency-resolved, which is meaningful for
  *when* `register(app)` runs but arbitrary for *where a link/card/widget
  ends up on screen* — `priority` decouples the two instead of forcing
  authors to rename folders to reorder UI.

## CineInfo sources (`register_cineinfo_source`)

A **provider pill** (above) adds a small availability badge. A **CineInfo
source** goes further: it feeds real data fields (rating, providers, custom
fields, ...) into the CineInfo lookups themselves, layered on top of the
built-in TMDB result. It's the extension point to use when you want a module to
*deliver* CineInfo data, not just flag availability — without touching the core
TMDB code.

Register one instance per source from your `register(app)`:

```python
from ...cineinfo.registry import register_cineinfo_source
from .sources import MySource

register_cineinfo_source(MySource(), item_id=MODULE_ID)
```

`item_id` is the id you already gave `register_thirdparty()`, exactly like every
other secondary registry below. It is optional only for backwards compatibility
— always pass it. Without it the source works but is orphaned: the Modulmanager
cannot list it among the module's capabilities, and `unregister_module()` cannot
drop it on uninstall, so it stays registered until the app restarts.

A source subclasses `web/cineinfo/source.py`'s `CineInfoSource` and declares
**one** capability flag that decides how the orchestrator fetches — this is the
whole "two forms" mechanism, chosen automatically, no user setting:

- `supports_bulk = False` → the orchestrator loops `fetch_one(item, ctx)` per
  item ("einzeln nach und nach"), bounded by a worker pool and a per-source rate
  limiter. Use this for upstreams that only answer one lookup per request (like
  TMDB itself).
- `supports_bulk = True` → the orchestrator calls `fetch_many(items, ctx)` once
  per chunk of up to `max_bulk` items ("alles in einer Anfrage"). Use this for
  upstreams with a real batch endpoint.

```python
from ...cineinfo.source import CineInfoSource, QueryContext
from ...db import get_setting

class MySource(CineInfoSource):
    id = "myprovider"                 # stable; also the cache namespace + limiter bucket
    label = "My Provider"
    supports_bulk = False             # ← the entire batch-form decision
    rate = 5.0                        # max upstream requests/second
    cache_ttl = 86400.0              # provider-cache TTL (0 disables caching)

    def is_enabled(self) -> bool:
        # Follow your own toggle so a disabled module stops contributing
        # immediately. (Uninstall is handled by the item_id above.)
        return get_setting("myprovider_enabled", "0") == "1"

    def fetch_one(self, item: dict, ctx: QueryContext) -> dict:
        # item carries a stable "key" plus lookup fields (title/imdb_id/tmdb_id).
        # Return only the fields you know; ctx.country / ctx.ui_lang are resolved.
        r = requests.get(..., timeout=8)
        return {"vote_average": r.json()["score"], "myprovider_url": r.json()["url"]}
```

What the orchestrator handles for you (identical for both forms): **cache-first**
(only cache-misses ever hit the network, via the shared `provider_cache` table),
a **per-source token-bucket rate limiter**, **in-flight de-duplication** of
concurrent identical lookups, bounded concurrency, per-query timeouts and error
isolation (a failing item or source never takes CineInfo down).

How the data lands: the core CineInfo endpoints (`/api/tmdb/info`,
`/api/tmdb/batch`) call `cineinfo.enrich(...)`, which runs each enabled source
and **field-merges** its payload onto the TMDB base. **The built-in TMDB data
wins**; a source only fills fields TMDB is missing or left empty (plus any custom
fields of its own). With no source registered, `enrich()` is a zero-cost
pass-through, so default behaviour is unchanged.

Where it shows up: the module manager lists it as `1 × CineInfo source`, and
**Integrations → CineInfo → "Source order"** gives it a draggable row with a
"Module" badge next to the provider pills. Both lists live in one setting
(`cineinfo_provider_order`, `ci:<source id>` for a CineInfo source, `ext:<name>`
and bare ids for pills; each consumer reads only its own prefix). The position
decides the order `enrich()` applies the sources in — first source to know a
field fills it, TMDB's base still wins over all of them. Nothing configured
means the old alphabetical-by-`id` order, unchanged.

See **`example_cineinfo_source/`** for a complete, offline-safe reference that
registers one source of *each* batch form (per-item and bulk) under the CineInfo
settings tab.

## Subtitle sources (`register_subtitle_source`)

The download path collects subtitles in three passes, cheapest first: the
renditions yt-dlp finds in the stream, the tracks the hoster's player config
carries out of band, and — only for the languages still missing after those two
— an *external* lookup. OpenSubtitles.com is the built-in implementation of that
third pass (`models/common/opensubtitles.py`, off by default).

`register_subtitle_source` is what makes that third pass extensible: a module
can plug its own service (a private server, a fansub index, a paid API) into the
same step, under the same rules — never asked for a language the file already
has, always allowed to fail.

Register one callable from your `register(app)`:

```python
from ....subtitle_sources import register_subtitle_source
from .source import fetch

register_subtitle_source(MODULE_ID, "myservice", "My Subtitle Service", fetch)
```

`item_id` is the id you already gave `register_thirdparty()`, exactly like every
other secondary registry here. Registrations are keyed by it, so
`unregister_module()` drops them when the module is disabled or uninstalled and
the Modulmanager can list the capability (`subtitle_source` → "subtitle
source"); a source registered under any other id keeps running after the module
is gone. `source_id` must not collide with a built-in
(`subtitle_sources.RESERVED_SOURCE_IDS`, currently `{"opensubtitles"}`) or with
another module's source — both raise.

The callable is the whole contract:

```python
def fetch(video_path, have_langs, meta) -> list:
    # video_path: the finished (still temporary) video file. Its size and
    #   content are readable, which is what a hash-based match needs.
    # have_langs: ISO 639-2/B tags the file ALREADY has. Never fetch these
    #   again — those tracks are timed to this exact stream.
    # meta: {"query", "season", "episode", "imdb_id", "tmdb_id"}, each value
    #   possibly None (a direct-link download knows none of them).
    wanted = [l for l in ("ger", "eng") if l not in have_langs]
    if not wanted:
        return []                     # nothing missing → no request at all
    path = video_path.with_suffix(".ger.srt")
    path.write_text(fetch_from_my_api(video_path, meta), encoding="utf-8")
    return [path]                     # <video stem>.<lang>.<ext>
```

Return the sidecar paths you wrote, named `<video stem>.<lang>.<ext>`; the
existing collect/mux path picks them up and muxes them into the `.mkv` as tagged
soft-sub tracks, indistinguishable from a subtitle the hoster delivered.
Anything else you return is ignored.

Two rules the core relies on:

- **It runs in the queue worker, between the download and the ffmpeg mux**, so
  it holds up that one episode. A couple of HTTP requests with short timeouts —
  no crawling, no retry storms.
- **It must not raise.** Exceptions are caught and logged, so a throwing source
  never fails a download; it is just dead weight.

Counterparts, if you manage registrations yourself:
`unregister_subtitle_source(item_id)`, `thirdparty_subtitle_source_ids()` and
`iter_subtitle_sources()`.

See **`example_subtitle_source/`** for a complete, offline-safe reference that
registers one demo source and returns `[]`.

## Content sources (`register_provider` / `register_search_source` / `register_home_feed_source`)

A **CineInfo source** (above) adds *metadata* about a title MediaForge
already knows about. A **content source** is different: it teaches
MediaForge about a whole new streaming site to browse and download
from — the same role AniWorld/SerienStream/FilmPalast/MegaKino/hanime.tv
play today. This is the one part of the app that predates the plugin
system, so a full integration is a handful of small, composable
registrations from your own `register(app)` instead of one call — pick the
ones you need:

```python
from mediaforge.providers import Provider, register_provider
from mediaforge.search import register_search_source
from mediaforge.home_feed import register_home_feed_source
from mediaforge.mirrors import register_site_mirrors
from mediaforge.web.uptime_monitor import register_monitor_site
import re as _re

MY_SERIES_PATTERN = _re.compile(r"^https?://mysite\.example/serie/[a-zA-Z0-9\-]+/?$")
MY_EPISODE_PATTERN = _re.compile(r"^https?://mysite\.example/serie/[a-zA-Z0-9\-]+/staffel-\d+/episode-\d+/?$")

def register(app):
    register_thirdparty(item_id="my_source", label="My Source", ...)

    # 1. URL resolution -- turns a mysite.example URL into your scraper classes.
    register_provider("my_source", Provider(
        name="MySource",                 # must be globally unique
        series_pattern=MY_SERIES_PATTERN,
        episode_pattern=MY_EPISODE_PATTERN,
        series_cls=MySourceSeries,       # your own scraper classes
        episode_cls=MySourceEpisode,
    ))

    # 2. The search bar / POST /api/search.
    def _search(keyword):
        # return [{"title": ..., "url": ...}, ...] -- url must match one of
        # the patterns registered above.
        ...
    register_search_source("my_source", site_id="my_source", search_fn=_search)

    # 3. Optional: domain fallback, same as AniWorld/s.to/etc already have.
    register_site_mirrors("my_source", "my_source",
                           ["mysite.example", "mysite.cc"], label="My Source")

    # 4. The home page. Without this your source is reachable by URL and by
    #    search, but never *offered* -- it does not appear on the start page.
    #    Only the new home page (Settings -> General -> "Use the new home
    #    page") reads this registry; the classic one has its rows in the
    #    template.
    def _browse_new():
        # Same card shape every built-in browse list returns. Return None to
        # say "upstream is down" -- the feed then reports your source as
        # unavailable instead of silently showing nothing.
        return [{"title": ..., "url": ..., "poster_url": ..., "genre": ...}]

    register_home_feed_source(
        "my_source", "my_source", "My Source",
        {"new": _browse_new, "popular": _browse_popular},
        media_type="series",          # or "movies" / "adult"
        color="#7c5cff",              # optional chip colour
    )

    # 5. Optional: an UpTime dashboard card for this site -- and, for free,
    #    a row in the DNS test too (same _MONITOR_SITES dict, both features
    #    read it).
    register_monitor_site(
        "my_source", "my_source", "My Source", "https://mysite.example",
        "mysite.example", body_markers=["mysite"],
        expected_headers={"server": "cloudflare"},
        enabled_setting_key="my_source_enabled",
    )
```

- **`register_provider(item_id, provider)`** (`mediaforge/providers.py`) adds
  your `Provider` — the exact dataclass the built-in sites use: URL regexes
  plus the model classes (`series_cls`/`season_cls`/`episode_cls`) that
  implement scraping. `resolve_provider()`, the single function
  `web/routes/{browse,search,stream}.py`, `queue_worker.py` and
  `autosync_worker.py` all use to turn a URL into a scraper class, checks
  every built-in provider first, then yours — a third-party name can never
  shadow a built-in one (raises `ValueError` on a collision either way).
  Your `series_cls`/`season_cls`/`episode_cls` need to expose whatever
  interface the rest of the app already calls on a built-in provider's
  classes for the URL kinds you support — read a `models/<site>/` package
  (e.g. `models/megakino_to/`, the newest and closest in shape to a
  from-scratch site) as the reference for that interface; it isn't
  re-documented here since it's the same either way, built-in or not.
- **`register_search_source(item_id, site_id, search_fn, label=None,
  adult=False, enabled_key=None)`**
  (`mediaforge/search.py`) is the search half: it makes
  `POST /api/search {"site": "<site_id>", "keyword": "..."}` (the endpoint
  itself, the one every built-in site's search already goes through) reach
  your `search_fn`, **and** puts your source into the normal search bar —
  it is listed by `GET /api/search/sources`, which is what the WebUI fans
  every keyword out to. `site_id` must not collide with a built-in one
  (`aniworld`/`sto`/`filmpalast`/`megakino`/`hanime`) or another
  registration, and must match `[a-z0-9][a-z0-9_-]{1,39}` — it becomes a
  settings key suffix, a DOM id and part of a CSS class. `label` is what the
  user sees: the heading above your results, your chip under the search
  field, your row in Settings → Sources. `adult=True` marks the source 18+,
  which makes it opt-in behind the age confirmation and hides it entirely
  from an age-limited session, exactly like the built-in adult source.
  `enabled_key` points at a settings key you already own; leave it unset and
  the standard `source_enabled_<site_id>` is used, which the Sources tab
  writes for you — either way the source defaults to **on**, since it was
  installed on purpose. Exceptions inside `search_fn` are caught by the route
  and logged, same as everything else in this document.

  Note that this is deliberately a *separate* call from
  `register_provider()`: registering only a provider makes your URLs
  resolvable (pasted links, AutoSync) without claiming your site can answer
  a keyword search. A source that offers both should register both.
- **`register_home_feed_source(item_id, source_id, label, fetchers, media_type="series", color=None)`**
  (`mediaforge/home_feed.py`) puts your source on the start page. `fetchers`
  is `{"new": fn}` and/or `{"popular": fn}`; each `fn()` returns the same
  `{"title", "url", "poster_url", "genre"}` cards every built-in browse list
  returns, and `GET /api/home-feed` caches the result for an hour exactly
  like the built-in lists, so `fn` may do real network work. Returning
  `None` means "upstream failed" and is reported to the user as such;
  returning `[]` means "nothing new", which is a different thing. The feed
  interleaves your cards with the built-in ones, drops a title you share
  with another source into a single card that names both, and gives you a
  chip in the filter row (`color` is its dot; anything that is not a plain
  hex literal is dropped). `media_type="movies"` also feeds the Movies row,
  `"adult"` is only ever fetched when the user turned the 18+ chip on.
  `source_id` must not collide with a built-in one or another registration.
  Your source is enabled unless `source_enabled_<source_id>` is `"0"`, so an
  `extra_settings` toggle under that key gives the user a real off switch.
- **`register_site_mirrors(item_id, site_id, hosts, label=None)`**
  (`mediaforge/mirrors.py`) is optional, for a site that (like most of the
  built-in ones) sometimes moves domains or gets DNS-blocked. It adds an
  entry to the exact same `DEFAULT_SITE_MIRRORS`/`SITE_LABELS` dicts the
  five built-in sites live in, and everything downstream of those two dicts
  is already generic — you get, for free: a mirror-editing card under
  Settings → Sources → "Domain fallback (mirrors)", persistence, and
  transparent host failover for any request your module makes through
  `mediaforge.config.GLOBAL_SESSION` (not a bare `requests`/`niquests`
  session of your own — only `GLOBAL_SESSION` is wired to the failover
  logic). `hosts[0]` is the canonical host your `Provider`'s patterns should
  be written against.
- **`register_monitor_site(item_id, site_id, label, url, expected_domain, body_markers, expected_headers=None, enabled_setting_key=None, enabled_setting_default=True, tracked_by_default=True)`**
  (`mediaforge/web/uptime_monitor.py`) is also optional, and equally
  free-standing — it adds an entry to `_MONITOR_SITES`, the **same dict both
  the UpTime dashboard (probe loop, API, JS rendering) and the DNS test**
  (Settings → Network & Access → "DNS test", `GET /api/settings/dns/test`)
  are already generic over. One call, two features: your site gets its own
  tracked/untracked toggle, heartbeat history and blocked-page detection on
  UpTime, *and* is probed and reported alongside the built-in sites the next
  time a user runs the DNS test — no separate registration, no other change.
  `enabled_setting_key` (e.g.
  the same key you passed `register_thirdparty()`) makes the card's
  "enabled_source" badge reflect your actual toggle instead of guessing.
  `enabled_setting_default` says what an **unset** key means — it defaults to
  `True`, because a module source was installed on purpose. Pass `False` only
  if your key is genuinely opt-in. Before this parameter existed the core
  guessed "off" for every module key, so a module that writes its key only on
  first save showed a permanent "source disabled" badge on an entirely
  healthy card.
- **Uninstall is automatic for all five.** Every registration above is keyed
  by `item_id`, the same id you already pass to `register_thirdparty()` —
  `web/thirdparties/registry.py`'s `unregister_module()` calls
  `unregister_provider()` / `unregister_search_source()` /
  `unregister_home_feed_source()` / `unregister_site_mirrors()` /
  `unregister_monitor_site()` for every
  `item_id` a module owned, so disabling/removing the module removes all of
  it, live, no restart.
- **The search bar asks for it automatically.** `GET /api/search/sources` is
  the one list of "which sources exist right now" (built-ins plus every
  `register_search_source()` registration, assembled by
  `web/source_policy.py`'s `search_sources()`), and the WebUI derives
  everything from it: the search fan-out in `app.js`'s `doSearch()`, the
  source chips under the search field, the result section (its own heading,
  in the user's source order), the on/off row in Settings → Sources, the
  Seerr "find streams" modal. Nothing to opt into — registering the search
  half *is* the opt-in. This used to be a known gap: those five consumers
  each hardcoded the built-in site ids, so a module source was reachable by
  pasted URL but never asked a keyword.
- **Still not automatic — read this before assuming a registered source is
  "done":**
  - **The classic home page still has its rows in the template.**
    `register_home_feed_source()` fills the *new* home page (Settings →
    General → "Use the new home page"); with the classic one selected your
    source has no carousel there, because that page's eleven rows are
    hardcoded markup in `templates/index.html`. A `dashboard_widget_template`
    is the workaround for classic-home users.
  - Advanced Search (`static/advanced_search.js`) is a TMDB discovery page,
    not a site fan-out, so it has no per-source list to join.

See **`example_content_source/`** for a complete, offline-safe reference
that registers a whole demo streaming site (`example-source.invalid`, one
series, three episodes, no network calls anywhere) using all five
registrations above together.

## Image hosts (`register_image_hosts`)

Every `poster_url` your module hands back — from `register_home_feed_source`,
`register_search_source`, a CineInfo source, wherever — is never linked to
directly. `_poster_proxy()` (`web/routes/image_proxy.py`) rewrites it to
`/api/img?url=...` before it reaches the client, so the browser (and mobile
devices behind an ISP DNS block) always talks to *this* server, never to
`mysite.example` directly. That proxy only fetches from an allowlist —
`image.tmdb.org`, `cdn.myanimelist.net`, the built-in sites' own CDNs, and
nothing else by default. A third-party module's own image host isn't on that
list, so **without this call every poster your module returns 403s at the
proxy and never loads**, even though the module itself is otherwise working.

This also covers the download-queue hub: a title with no TMDB entry (or no
TMDB key configured at all) gets its poster from your `Provider`'s own
`series_cls(url=...).poster_url` instead — resolved once in the background
and cached, never blocking `/api/queue`'s ~2s poll — and that fallback goes
through the exact same `_poster_proxy()` -> allowlist path. Nothing to call
for this specifically; it works automatically for any `register_provider`
source once its image host is registered here.

```python
from mediaforge.web.routes.image_proxy import register_image_hosts

def register(app):
    register_thirdparty(item_id="my_source", label="My Source", ...)

    # Exact hosts, e.g. the site itself:
    register_image_hosts("my_source", hosts=("mysite.example",))

    # Or a whole CDN by suffix, if posters come from a varying/unknown
    # subdomain (img1.cdn.mysite.example, img7.cdn.mysite.example, ...):
    register_image_hosts("my_source", domains=("cdn.mysite.example",))

    # Both together are fine -- pass whichever (or both) your site needs.
```

- **`hosts`**: exact hostnames, matched case-insensitively (`www.` ignored on
  both sides).
- **`domains`**: matched by suffix, so `cdn.mysite.example` also allows
  `img1.cdn.mysite.example` — never a substring match, so
  `cdn.mysite.example.attacker.tld` is still rejected. Use this instead of
  `hosts` when the poster CDN's exact subdomain isn't fixed.
- At least one of the two is required; calling it again with the same
  `item_id` replaces what was previously registered.
- This only widens *which host the proxy is willing to fetch from* — the
  independent SSRF check (`stream_proxy.is_safe_url`, which resolves the
  host's DNS and rejects anything pointing at an internal/loopback address)
  still runs on every fetch regardless of who added the host, exactly like a
  built-in host.
- **Uninstall is automatic**, same `item_id`-based cleanup as everywhere
  else: `unregister_module()` calls `unregister_image_hosts()` for you.

See **`example_content_source/`**, which also calls this for its demo poster
host.

## Hosters (`register_hoster`)

The video-hoster layer (VOE, Vidoza, Filemoon, ...) that `get_direct_link_for()`
dispatches to is normally auto-discovered from `extractors/provider/*.py` —
but that directory lives inside MediaForge's own source tree, not
`~/.mediaforge/thirdparties/`, so a module can't add a file there the way it
adds a template or a route. `register_hoster()` (`mediaforge/extractors/
__init__.py`) is the external equivalent, called from your own `register(app)`:

```python
from mediaforge.extractors import register_hoster

def _get_direct_link(url):
    ...  # same contract as a get_direct_link_from_<provider> function
    return direct_url

def register(app):
    register_thirdparty(item_id="my_hoster_mod", label="My Hoster", ...)
    register_hoster(
        "my_hoster_mod",
        name="MyHoster",
        get_direct_link=_get_direct_link,
        headers={"Referer": "https://myhoster.example/"},
        host_patterns=("myhoster.example", "myhoster.cc"),
    )
```

This adds `name` to `config.SUPPORTED_PROVIDERS` (so it's actually offered,
not just resolvable), wires `get_direct_link`/`get_preview_image` into the
same `provider_functions` dict the auto-discovered extractors populate, merges
`host_patterns` into `HOST_PROVIDER_MAP` (so a mislabeled/mirrored embed still
resolves to your extractor by its actual domain, same as the built-in hosters
— see `extractors/__init__.py`'s `provider_for_url()`), and merges `headers`
into `config.PROVIDER_HEADERS_D`/`PROVIDER_HEADERS_W` (only if the hoster name
isn't already present — never overwrites a built-in hoster's headers). All of
this takes effect immediately, no restart, including
`web/runtime_state.py`'s `WORKING_PROVIDERS` (what Settings and the provider
picker actually show users), which is refreshed as part of the call.
Uninstalling the module (`unregister_module()`) calls `unregister_hoster()`
for you, same `item_id`-based cleanup as everywhere else in this document.

## Cancellable downloads (`cancel_event`)

A provider's `episode_cls.download(cancel_event=...)` is handed a
`threading.Event` by the queue worker. Honour it — the user pressing **Cancel**
in the queue must stop your download *now*, not at the end of the file:

```python
def download(self, cancel_event=None):
    for chunk in stream:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Download cancelled")   # the string the worker books as "cancelled"
        ...
```

Two follow-ups the built-in pipeline does and yours should too:

```python
# 1. Clean up your own partial files. yt-dlp deliberately keeps .part/.ytdl/
#    fragment files behind for a later resume — a cancelled queue item is not
#    a later resume, so they are just garbage in the user's library folder.
from mediaforge.models.common.common import cleanup_partial_downloads
cleanup_partial_downloads(output_path, reason="cancelled")

# 2. If you open the captcha browser, hand it the same event, or patchright
#    keeps solving a captcha for a download that no longer exists (up to the
#    full 5 min solve timeout).
from mediaforge.playwright import captcha
captcha.set_cancel_event(cancel_event)      # thread-local: call it in the thread that solves
try:
    ...
finally:
    captcha.clear_cancel_event()
```

`captcha.cancel_requested()` is available for your own polling loops. Both
solvers raise `RuntimeError("Download cancelled")` once the event is set, and
close their browser on the way out.

## Notification channels (`register_notification_channel`)

`extra_settings` (see "Richer settings fields" above) lets you add one more
*toggle* to an existing notification pill (e.g. Discord). It does not let you
add a whole new *channel* that fires when a download completes, errors,
AutoSync finds new episodes, etc. — that's what this hook is for:

```python
from mediaforge.web.thirdparties.registry import register_notification_channel

def _send(title, body, event, username=None, status=None, episode_count=0,
          errors=None, is_movie=False):
    if get_setting("my_channel_enabled", "0") != "1":
        return
    # send asynchronously (own thread), same as every built-in notify_* —
    # don't block the request/worker that triggered this.
    ...

def register(app):
    register_thirdparty(item_id="my_channel", label="My Channel", ...)
    register_notification_channel("my_channel", _send)
```

`web/notifications.py`'s `notify_all()` — already called by `queue_worker.py`
(on_completed/on_errors/on_cancelled) and `autosync_worker.py`
(on_autosync/on_sync_hold/on_sync_resume) — calls every registered channel
with the exact same keyword arguments it passes its six built-in ones
(WebPush/Telegram/Pushover/ntfy/WhatsApp/Discord), each isolated in its own
try/except so one broken channel never blocks another or the notification
itself. Do your own enabled/preference check inside `_send` (as above) —
registering here does not imply "always on". Removed automatically on
disable/uninstall, same `item_id`-keyed cleanup as the other hooks in this
document.

## Lifecycle event hooks (`register_event_hook`)

For a reaction that isn't itself a notification — auto-tagging, kicking off
an external automation, updating your own module's state — hook the event
directly instead of pretending to be a notification channel:

```python
from mediaforge.web.thirdparties.registry import register_event_hook

def _on_completed(title, body, event, username=None, status=None,
                   episode_count=0, errors=None, is_movie=False):
    ...  # e.g. POST to an external webhook -- this is what covers the
         # open "Generic Outgoing Webhook" roadmap item for your own module,
         # without waiting for a built-in one

def register(app):
    register_thirdparty(item_id="my_hooks", label="My Hooks", ...)
    register_event_hook("my_hooks", "on_completed", _on_completed)
```

Fired from the same place as notification channels (`notify_all()`), with the
same events and the same keyword arguments — see `web/notifications.py`'s
module docstring for the full event list (`on_completed`, `on_errors`,
`on_cancelled`, `on_autosync`, `on_sync_hold`, `on_sync_resume`). A hook that
raises is logged and never blocks another hook, another channel, or the
notification itself. You can register more than one hook per event (e.g. one
per `item_id`); all of them run. Removed automatically on disable/uninstall.

See **`example_hooks/`** for a complete, offline-safe reference that
registers one notification channel and one `on_completed` event hook
(both just log — safe to enable, no network calls).
