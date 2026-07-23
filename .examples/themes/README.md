# MediaForge Theme Packs

A **theme pack** restyles the whole MediaForge WebUI — fonts, colors,
animations, checkboxes, inputs, number steppers, the calendar, background
images and anything else CSS can reach. Themes are deliberately **CSS + assets
only**: no JavaScript, no HTML, no Python. That is the security contract that
makes them safe to install — a theme can change how MediaForge *looks*, never
what it *does*.

Theme packs are distributed through the same Module Store as modules (the
store index entry simply carries `"type": "template"` — `"theme"` is accepted
as an alias), installed and removed in the **Module Manager**, and selected
under **Settings → Design → Theme Packs**. Unlike modules, themes never need a restart: install, update and
uninstall all apply live.

## Anatomy of a theme pack

```
example_theme/
├── theme.json        # manifest (required)
├── theme.css         # token layer (referenced from the manifest)
├── components.css    # optional additional stylesheets, any names
├── preview.svg       # optional preview shown in the Module Manager
└── fonts/…, img/…    # optional assets, referenced relatively from the CSS
```

### theme.json

```json
{
  "id": "example_theme",
  "name": "Example Theme",
  "version": "1.0.0",
  "author": "You",
  "description": {"en": "…", "de": "…"},
  "stylesheets": ["theme.css", "components.css"],
  "preview": "preview.svg",
  "supports": {"dark": true, "light": true},
  "min_app_version": "",
  "max_app_version": ""
}
```

- `id` — stable identity, lowercase `a-z0-9_-`, matches the store entry's id.
- `stylesheets` — load order. All listed files are concatenated and served as
  one bundle (`/themes/<folder>/bundle.css`). Relative `url(...)` references
  inside the CSS resolve inside the theme folder.
- `supports` — informational: which of the two base modes (dark/light) the
  theme provides overrides for. Whatever a theme does not override falls back
  to MediaForge's built-in look.
- `min_app_version` / `max_app_version` — optional compatibility gate, same
  semantics as modules.

### Allowed file types

`.css`, fonts (`.woff .woff2 .ttf .otf`), images
(`.png .jpg .jpeg .webp .gif .avif .ico .svg`), `.json`, `.md`, `.txt` and the
store's `MODULE.sig` signature file. **Anything else — especially `.js`,
`.html`, `.py` — makes the pack invalid** and it will refuse to install (and a
hand-copied folder containing such files is listed as *Invalid* in the Module
Manager and never served).

## How theming works

`base.html` loads the active theme's bundle **after every core stylesheet**,
so a theme wins the cascade at equal specificity. Two layers are useful:

1. **Token layer** — override the CSS custom properties from
   `web/static/variables.css` under `[data-theme="dark"]` and
   `[data-theme="light"]`. This recolors the entire app (accent, surfaces,
   text, borders, inputs, shadows, radii, transition timing) in a few lines,
   and the user's dark/light toggle keeps working inside your theme.
2. **Component layer** — restyle concrete controls with the same selectors the
   core sheets use: `.chb-main` (every checkbox, including ones modules add),
   `input[type=…]` / `select` / `textarea` (forms.css), `.num-input-wrap`
   (number steppers), `.btn` / `.btn-primary` (buttons.css), the calendar
   classes (calendar.css), `.settings-section` cards, and so on. Custom
   `@keyframes` animations are fine — they are plain CSS.

Fonts can be shipped in the pack (`@font-face` with a relative `src:
url(fonts/MyFont.woff2)`) — no external requests needed, which also keeps
themes working offline.

## Trying this example locally

Copy `example_theme/` into the theme folder shown in **Module Manager → Module
Store → Theme folder** (`~/.mediaforge/themes/` by default), reload the page,
and pick it under **Settings → Design → Theme Packs**. No restart needed.

## Publishing to a store

Zip the folder (the zip must contain exactly one top-level folder named like
the entry's `folder`) and add it to a store index with `"type": "template"`
(`"theme"` works as an alias; entries without a `type` are modules):

```json
{
  "id": "example_theme",
  "type": "template",
  "category": "",
  "name": "Example Theme",
  "version": "1.0.0",
  "download_url": "packages/example_theme-1.0.0.zip",
  "sha256": "…",
  "trust": "unverified"
}
```

`category` is an optional free-form grouping label (shown in the store row's
meta line); `signed_by` may name who packaged/signed the entry — both are
display-only, the trust badge always comes from the package's own signature.

Signing works exactly like modules (`MODULE.sig`, Ed25519, hashed over every
file) — a signed theme shows the same Official/Verified badge in the Module
Manager. Unsigned themes require the admin to allow unverified installs, again
exactly like modules.

## Selection model

- **Admin** sets the instance-wide default (Settings → Design, or "Set as
  default" on the theme's card in the Module Manager). Stored server-side —
  every user and every device sees it, including the login page.
- **Each user** may override it for their own device (Settings → Design →
  "Your theme", stored in the browser like the dark/light choice). A stale
  override — the theme got uninstalled — silently falls back to the instance
  default.
