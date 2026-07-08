# Third-party integrations — how the plug-in system works

This document explains `src/mediaforge/web/thirdparties/`, MediaForge's
plug-in system for optional, self-contained features (Crunchyroll-style
external-API integrations, extra Discover pages, etc.). It also explains
`example_integration/` next to this file — a complete, working, heavily
commented reference implementation you can copy as a starting point.

Read this file top to bottom once; after that it should work as a checklist.

## The contract, in one sentence

Every folder under `src/mediaforge/web/thirdparties/` that contains an
`__init__.py` exposing a `register(app)` function is imported and wired up
automatically when the app starts. Nothing else in the codebase — not
`app.py`, not `base.html`, not `integrations.html` — needs to change.

## What "automatically" means, concretely

At startup, `web/thirdparties/__init__.py`'s `discover_and_register(app)`:

1. Lists every subfolder of `web/thirdparties/` (skipping ones starting
   with `_`, so `__pycache__` etc. are ignored).
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
web/thirdparties/<your_integration>/
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
sidebar and the Integrations settings page. Its parameters:

| Parameter              | Meaning                                                                                     |
|-------------------------|-----------------------------------------------------------------------------------------------|
| `item_id`               | Unique key, e.g. `"example_integration"`. Used in URLs, DOM ids, and the settings key prefix. |
| `label`                 | English source string for the sidebar link / card title (translated via gettext at render time). |
| `endpoint`               | Blueprint-qualified Flask endpoint for your main page, e.g. `"example_integration.index"`.    |
| `icon_svg`                | Raw `<svg>...</svg>` markup for the sidebar icon (use `stroke="currentColor"`).              |
| `enabled_setting_key`       | The `app_settings` DB key that turns you on/off, e.g. `"example_integration_enabled"`.       |
| `badges`                     | List of `(text, css_color)` tuples shown as small pills on your settings card.                |
| `description`                  | Hint text shown at the top of your settings card.                                          |
| `enable_label` / `enable_desc`  | Label/description for the card's enable toggle. `enable_label` defaults to `"Enable {label}"`. |

## Settings and the sidebar

- The sidebar entry (under "Discover") only appears while
  `enabled_setting_key` is `"1"` in the database. It disappears again the
  moment it's turned off — no restart needed, this is checked fresh on
  every request.
- The Integrations → Third Party tab automatically gets a collapsible card
  for you (title, badges, description, one enable toggle) — you don't
  write any HTML or JS for this. The toggle reads/writes through the
  shared `GET/PUT /api/settings/thirdparty/<item_id>` endpoint, which maps
  straight to `enabled_setting_key`.
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
src/mediaforge/web/thirdparties/<your_integration>/babel.cfg -o messages.pot
src/mediaforge/web/thirdparties/<your_integration>`, then `pybabel update`
against your `.po`. In practice, for a small integration, hand-editing the
`.po` file directly (like `example_integration/translations/...` does) is
usually faster than running the extract/update pipeline.

## Packaging

`pyproject.toml`'s `[tool.setuptools.package-data]` and `MANIFEST.in`
already contain recursive globs for `web/thirdparties/**` covering
`*.py`, `*.html`, `*.css`, `*.js`, `*.po`, `*.mo`, and `babel.cfg`. A new
integration folder is included in a built wheel/installer automatically —
you don't need to add anything there either.

## How to actually create a new integration

1. Copy `example_integration/` (next to this README) to
   `src/mediaforge/web/thirdparties/<your_name>/`.
2. Rename the Blueprint name (`"example_integration"` → `"<your_name>"`)
   everywhere it appears: `routes.py`'s `Blueprint(...)` call, every
   `url_for(...)` reference in the templates, `static_url_path`, and the
   `item_id`/`enabled_setting_key`/`endpoint` values passed to
   `register_thirdparty(...)` in `__init__.py`.
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

## Two reference implementations to read

- `example_integration/` (next to this README) — minimal, deliberately
  simple, exists purely to be copied. Every file is commented to explain
  *why* it's shaped the way it is, not just what it does.
- `src/mediaforge/web/thirdparties/anime_seasons/` — a real, shipped
  integration (fetches seasonal anime listings from the Jikan/MyAnimeList
  API) that uses the same pattern at full scale: external HTTP calls with
  rate-limiting, a persistent cache, its own translation catalog, and a
  richer page (a season picker plus a card grid that reuses the app's
  existing browse-card enrichment pipeline).

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
