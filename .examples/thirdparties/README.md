# Third-party integrations — how the plug-in system works

This document explains MediaForge's plug-in system for optional,
self-contained features (Crunchyroll-style external-API integrations, extra
Discover pages, etc.). It also explains `example_integration/` next to this
file — a complete, working, heavily commented reference implementation you can
copy as a starting point.

Read this file top to bottom once; after that it should work as a checklist.

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
| `settings_host` / `settings_tab`   | Which existing settings page and which tab/pill on it your card is shown on. `settings_host` is `"integrations"` (default, the classic Third Party tab), `"notifications"`, or `"settings"` (the main Settings page's tab bar). See "Settings placement" below for the full picture. |
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

## Settings placement — attach to an existing tab/pill, or create a new one

`settings_host` picks *which page* your card shows up on:

- `"integrations"` (default) — the Integrations page's tab bar.
- `"notifications"` — the Notifications page's service-pill row.
- `"settings"` — the main Settings page's tab bar.

`settings_tab` then picks *where on that page*:

- Match one of that host's existing tab/pill ids and your card is appended
  into that tab/pill's own content, below whatever it already renders by
  hand. Existing ids: `"seerr"`, `"mediaplayer"`, `"cineinfo"`,
  `"thirdparty"`, `"syncplay"`, `"uptime"` for `"integrations"`;
  `"webpush"`, `"telegram"`, `"pushover"`, `"ntfy"`, `"discord"`,
  `"whatsapp"`, `"storage"` for `"notifications"`; `"general"`, `"design"`,
  `"sources"`, `"downloads"`, `"autosync"`, `"network"`, `"auth"`, `"api"`,
  `"updates"` for `"settings"`. Example: an extension that adds one more
  toggle to the existing Discord notification pill would use
  `settings_host="notifications", settings_tab="discord"`.
- Anything else creates a brand-new tab/pill automatically, titled from
  `settings_tab_label` (defaults to `label`). No template edit needed
  either way — both cases are handled generically by
  `web/thirdparties/registry.py`'s `resolve_dynamic_tabs()`.

A brand-new tab no longer lives in a tab bar: it renders as a **sidebar
sub-menu entry** (carrying the module **"M" pill**), a tile in the page's
**overview grid**, and its own content panel — the sub-menu + overview surface
that replaced the old tab bar. Feed that tile with two optional info fields:

- `overview_description` — text shown on the overview tile (defaults to
  `description`).
- `overview_icon_svg` — icon for the tile and the sub-menu entry (defaults to
  `settings_tab_icon_svg`, then a generic placeholder).

`resolve_dynamic_tabs()` surfaces `id`, `label`, `icon_svg`, `description`,
`module_name` and `is_module` so the template can render all three places
(sub-menu link, overview tile, panel). See `example_cineinfo_source/` for a
module that registers its own dynamic tab this way.

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

Everything *else* is staged and applied at the next start (`_pending/`, see
`apply_pending_changes()`): updates, uninstalls, and store installs. Not out of
caution — out of Flask. `app.register_blueprint()` works on a running app; there
is no supported way to *un*register one, replace one, or re-run an
already-imported module's top-level code. "Swap the folder while nothing is
looking" only exists between process start and first request, so that's where it
happens. The Modulmanager shows a "restart required" banner while anything is
staged, and lets you discard it.

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
  regardless of which blueprint is rendering.

## Reusable UI components

MediaForge's core CSS is already loaded on every page (`base.html`'s
`<head>` — everything below except `settings_rows.css` needs no extra
`<link>` at all) and its class names are stable, so use them instead of
inventing new ones — a new integration then looks native for free. Enable
`example_ui_components/` (section "Management" in the sidebar once
enabled) for a live, click-through gallery of all of these with the exact
markup underneath each one; the table below is the quick-reference version.

| Component | Classes | Defined in | Notes |
|---|---|---|---|
| Badges/tags | `.badge` + `.badge-accent`/`-success`/`-warning`/`-error`/`-neutral` | `tabs-badges.css` | `<span class="badge badge-accent">Beta</span>` |
| Service pills (tab selector) | `.service-pills` (wrapper) / `.service-pill` (+ `.active`) | `settings_rows.css` | The same pill row Notifications uses per channel; see `notifications.html` |
| Toggle switch | `.toggle` (wrapper) / `.toggle-slider` | `tables.css` | `<label class="toggle"><input type="checkbox" .../><span class="toggle-slider"></span></label>`. Inside a settings card, add `class="thirdparty-toggle" data-thirdparty-id="..."` and it wires itself up for free — see `_settings_card_macro.html` / `static/extension_cards.js` |
| Number stepper (−/+) | *(none needed)* | `forms.css` + `number_input.js` | Any `<input type="number">` is auto-enhanced on page load (and for anything added to the DOM later) — no markup, no JS, of your own |
| Buttons | `.btn` + `.btn-primary`/`-secondary`/`-ghost`/`-danger`, `.btn-sm`/`-lg`, `.btn-icon` | `buttons.css` | |
| Settings row layout | `.settings-section` (card) / `.settings-row` / `-left`/`-right`/`-label`/`-desc` | `settings_rows.css` (needs its own `<link>`) | The label-left/control-right row every Settings page is built from |
| Empty state | `.empty-state` / `-icon` / `-title` / `-desc` | `feedback.css` | Centered icon+title+description for "nothing here yet" |
| Progress bar | `.progress-wrap` (track) / `.progress-bar` (fill, inline `style="width:N%"`) | `tabs-badges.css` | Prefix your own bar class instead of styling `.progress-bar` directly if several bars exist on one page at once |
| Icons | *(convention, not a class)* | — | Inline `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` — no sprite sheet, `stroke="currentColor"` is what makes it follow theme/text color automatically |

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

Seven folders here demonstrate the same contract at different scales. Start
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

register_cineinfo_source(MySource())
```

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
        # Follow your own toggle so a disabled/uninstalled module stops
        # contributing immediately — no registry cleanup needed.
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

See **`example_cineinfo_source/`** for a complete, offline-safe reference that
registers one source of *each* batch form (per-item and bulk) under the CineInfo
settings tab.
