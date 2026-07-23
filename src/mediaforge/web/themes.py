"""Theme packs — installable CSS-only skins for the MediaForge WebUI.

A theme pack is a folder under ``~/.mediaforge/themes/<folder>/`` containing a
``theme.json`` manifest plus stylesheets and assets (fonts, images). Themes are
deliberately *data, not code*: no Python, no JavaScript, no HTML. That single
restriction is what makes the whole feature safe to open up — a theme can
restyle checkboxes, inputs, the calendar, animations and typography by
overriding the CSS custom properties from variables.css (and any component
rules), but it can never run in the browser or in the server process. The
validator below enforces this with an extension whitelist; anything outside it
refuses to install.

Distribution rides on the existing module store (web/thirdparties/store.py):
the store index gained a ``type`` field ("module" | "theme"), and entries typed
"theme" are downloaded, checksum- and signature-verified through exactly the
same pipeline as modules, then land here instead of in the thirdparties
folder. Because a theme is inert, install/upgrade/uninstall are always applied
live — there is no blueprint to replace, so the "restart required" dance the
module store needs never applies to themes.

Manifest (theme.json)::

    {
      "id": "example_theme",           // stable identity, matches store id
      "name": "Example Theme",
      "version": "1.0.0",
      "author": "Jane Doe",
      "description": {"en": "...", "de": "..."},
      "stylesheets": ["theme.css"],    // load order, relative paths
      "preview": "preview.svg",        // optional, shown in the picker
      "supports": {"dark": true, "light": true},
      "min_app_version": "",           // optional, same gate as modules
      "max_app_version": ""
    }

Selection is two-layered, mirroring how the rest of the appearance system
works: the admin sets an instance-wide default (``app_settings`` key
``theme_pack_active``, rendered server-side into base.html so first paint is
already themed), and every user may override it client-side (localStorage
``aw-themepack``, applied by a synchronous bootstrap script in <head> before
first paint — same pattern as the existing dark/light ``aw-theme`` key).
Dark/light stays orthogonal: a theme's CSS targets ``[data-theme="dark"]`` /
``[data-theme="light"]`` selectors, and whatever a theme does not override
falls back to the built-in tokens from variables.css, which always load first.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from ..config import MEDIAFORGE_CONFIG_DIR

logger = logging.getLogger(__name__)

# Where installed theme packs live: next to the database, image_cache and the
# thirdparties folder — i.e. in the data dir, never inside the source tree, so
# a self-update or a new Docker image never wipes them. Unlike modules this
# path is NOT appended to any package __path__: themes are not importable and
# must never become so.
THEMES_DIR = Path(MEDIAFORGE_CONFIG_DIR) / "themes"

MANIFEST_NAME = "theme.json"

# The app_settings key holding the admin's instance-wide default theme folder.
# Empty / missing / stale (folder gone) all mean "built-in look".
ACTIVE_THEME_KEY = "theme_pack_active"

# The id the UI uses for "no theme pack, built-in MediaForge look". Reserved so
# a store entry can never shadow it.
BUILTIN_THEME_ID = "default"

# Everything a theme may ship. CSS + fonts + images + the manifest itself and
# harmless documentation. Notably absent, and deliberately so: .js (script in
# every visitor's browser), .html (same thing with extra steps), .py (script in
# the *server*), .svg is allowed because themes legitimately need vector
# assets, but it is served with a Content-Security-Policy that blocks script
# execution — see routes/themes.py.
ALLOWED_EXTENSIONS = frozenset({
    ".css",
    ".woff", ".woff2", ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico", ".svg",
    ".json", ".md", ".txt",
    ".sig",  # MODULE.sig — the store's signature file, verified at install
})

# Folder names that can never be theme packs: the staging area, and the id the
# UI reserves for the built-in look.
RESERVED_THEME_NAMES = frozenset({"_pending", BUILTIN_THEME_ID})

# A theme manifest is a small file; anything bigger is not a manifest.
MAX_MANIFEST_BYTES = 64 * 1024

# Hard cap for a single stylesheet read into the bundle. CSS bigger than this
# is either generated garbage or an attack on the server's memory, not a skin.
MAX_STYLESHEET_BYTES = 2 * 1024 * 1024

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def themes_dir() -> Path:
    """The folder installed theme packs live in, created on first use."""
    THEMES_DIR.mkdir(parents=True, exist_ok=True)
    return THEMES_DIR


def _norm_rel_path(raw: str) -> str | None:
    """A manifest-supplied relative path, or None if it tries anything funny.

    Same paranoia as the store's archive extraction: absolute paths and ``..``
    would let a manifest reference (and the bundle route read) files outside
    the theme's own folder.
    """
    rel = str(raw or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    return rel


def read_manifest(folder: Path) -> tuple[dict | None, str]:
    """Parse ``<folder>/theme.json`` -> (manifest, "") or (None, reason)."""
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return None, f"no {MANIFEST_NAME}"
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None, f"{MANIFEST_NAME} larger than {MAX_MANIFEST_BYTES} bytes"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable {MANIFEST_NAME}: {exc}"
    if not isinstance(data, dict):
        return None, f"{MANIFEST_NAME} is not a JSON object"
    return data, ""


def validate_theme_dir(folder: Path) -> list[str]:
    """Every reason this folder is not a valid theme pack (empty list = valid).

    Called at install time (refusing the package) and again at discovery
    (marking a hand-copied folder broken instead of serving it). The rules are
    the whole security story of the feature, so they are enforced in one place:

    - a parseable manifest with a well-formed id and at least one stylesheet;
    - every file's extension on the whitelist — no scripts, no markup;
    - every manifest-referenced path inside the folder and actually present.
    """
    errors: list[str] = []
    manifest, reason = read_manifest(folder)
    if manifest is None:
        return [reason]

    theme_id = str(manifest.get("id") or "").strip()
    if not _ID_RE.match(theme_id):
        errors.append("manifest 'id' must be lowercase letters/digits/_/- (max 64 chars)")

    sheets = manifest.get("stylesheets")
    if not isinstance(sheets, list) or not sheets:
        errors.append("manifest 'stylesheets' must be a non-empty list")
        sheets = []

    for raw in sheets:
        rel = _norm_rel_path(raw)
        if rel is None:
            errors.append(f"stylesheet path escapes the theme folder: {raw!r}")
            continue
        if not rel.lower().endswith(".css"):
            errors.append(f"stylesheet is not a .css file: {raw!r}")
            continue
        if not (folder / rel).is_file():
            errors.append(f"stylesheet listed but missing: {rel}")

    preview = manifest.get("preview")
    if preview:
        rel = _norm_rel_path(preview)
        if rel is None or not (folder / rel).is_file():
            errors.append(f"preview listed but missing or invalid: {preview!r}")

    for path in folder.rglob("*"):
        if path.is_symlink():
            # A symlink can point anywhere on the server; the asset route would
            # then happily serve /etc/passwd with a text/plain header.
            errors.append(f"symlink not allowed in a theme: {path.relative_to(folder)}")
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            errors.append(
                f"file type not allowed in a theme: {path.relative_to(folder)} "
                f"(themes may only contain CSS, fonts and images)")
    return errors


# ---------------------------------------------------------------------------
# Discovery — cached because the context processor asks on every request.
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"themes": None, "at": 0.0}
_CACHE_TTL = 5.0  # seconds — cheap freshness; install/uninstall invalidate explicitly


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["themes"] = None


def installed_themes(refresh: bool = False) -> list[dict]:
    """Every theme folder on disk, valid or not — invalid ones carry their
    reasons so the Modulmanager can say *why* instead of hiding them.

    Shape per entry::

        {"id", "folder", "name", "version", "author", "description": {en,de},
         "supports": {"dark": bool, "light": bool}, "stylesheets": [...],
         "preview": "rel/path" | "", "valid": bool, "errors": [...],
         "signature": {tier, signed, valid, signer, reason}}
    """
    with _CACHE_LOCK:
        if not refresh and _CACHE["themes"] is not None and time.monotonic() - _CACHE["at"] < _CACHE_TTL:
            return _CACHE["themes"]

    themes: list[dict] = []
    root = themes_dir()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for folder in entries:
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        if folder.name in RESERVED_THEME_NAMES:
            continue
        themes.append(_theme_entry(folder))

    with _CACHE_LOCK:
        _CACHE["themes"] = themes
        _CACHE["at"] = time.monotonic()
    return themes


def _theme_entry(folder: Path) -> dict:
    manifest, reason = read_manifest(folder)
    errors = validate_theme_dir(folder)
    manifest = manifest or {}

    description = manifest.get("description") or {}
    if isinstance(description, str):
        description = {"en": description}
    supports = manifest.get("supports") or {}
    if not isinstance(supports, dict):
        supports = {}

    # The proven trust tier, same mechanism as installed modules: MODULE.sig
    # verified against the keys this build ships. signing.py hashes files
    # generically, so it works unchanged on a folder of CSS.
    try:
        from .thirdparties.signing import verify_module
        signature = verify_module(
            folder,
            module_id=str(manifest.get("id") or folder.name),
            version=str(manifest.get("version") or "0.0.0"),
        )
    except Exception:  # pragma: no cover - verification must never break listing
        signature = {"tier": "unverified", "signed": False, "valid": False,
                     "signer": "", "reason": "verification unavailable"}

    stylesheets = []
    for raw in (manifest.get("stylesheets") or []):
        rel = _norm_rel_path(raw)
        if rel and (folder / rel).is_file():
            stylesheets.append(rel)

    preview = _norm_rel_path(manifest.get("preview") or "") or ""
    if preview and not (folder / preview).is_file():
        preview = ""

    return {
        "id": str(manifest.get("id") or folder.name),
        "folder": folder.name,
        "name": str(manifest.get("name") or folder.name),
        "version": str(manifest.get("version") or "0.0.0"),
        "author": str(manifest.get("author") or ""),
        "description": description if isinstance(description, dict) else {},
        "supports": {
            "dark": bool(supports.get("dark", True)),
            "light": bool(supports.get("light", True)),
        },
        "stylesheets": stylesheets,
        "preview": preview,
        "valid": not errors,
        "errors": errors,
        "signature": signature,
    }


def _safe_theme_folder(folder: str) -> str | None:
    """A caller-supplied theme folder name, or None if it is anything but a
    plain directory name. Blocks empty, ``_``-prefixed (staging), dot names
    (``.``/``..`` — a ``..`` here would let rmtree eat the whole data dir) and
    separators, and belt-and-braces-verifies the joined path resolves to a
    direct child of the themes dir."""
    folder = (folder or "").strip()
    if (not folder or folder.startswith(("_", ".")) or "/" in folder
            or "\\" in folder or folder in RESERVED_THEME_NAMES):
        return None
    try:
        root = themes_dir().resolve()
        if (root / folder).resolve().parent != root:
            return None
    except OSError:
        return None
    return folder


def theme_by_folder(folder: str) -> dict | None:
    """The installed theme living in ``themes/<folder>/``, or None."""
    folder = _safe_theme_folder(folder)
    if folder is None:
        return None
    for theme in installed_themes():
        if theme["folder"] == folder:
            return theme
    return None


def themes_by_id() -> dict[str, dict]:
    """Installed themes keyed by manifest id — what the store's catalog joins
    against to decide installed/update_available for type=theme entries."""
    return {t["id"]: t for t in installed_themes()}


# ---------------------------------------------------------------------------
# Active theme (admin-set instance default)
# ---------------------------------------------------------------------------

def active_theme() -> dict | None:
    """The instance-default theme entry, or None for the built-in look.

    A stale setting (theme uninstalled, folder renamed, files broken) resolves
    to None rather than erroring: the UI must always be able to render, and
    "your theme is gone, here is the default" is the only sane failure mode.
    """
    from .db import get_setting

    folder = (get_setting(ACTIVE_THEME_KEY, "") or "").strip()
    if not folder or folder == BUILTIN_THEME_ID:
        return None
    theme = theme_by_folder(folder)
    if theme is None or not theme["valid"]:
        return None
    return theme


def set_active_theme(folder: str) -> tuple[bool, str]:
    """Set the instance default. ``""`` or ``"default"`` = built-in look."""
    from .db import set_setting

    folder = (folder or "").strip()
    if not folder or folder == BUILTIN_THEME_ID:
        set_setting(ACTIVE_THEME_KEY, "")
        return True, ""
    theme = theme_by_folder(folder)
    if theme is None:
        return False, f"no such theme: {folder}"
    if not theme["valid"]:
        return False, "theme is invalid: " + "; ".join(theme["errors"])
    set_setting(ACTIVE_THEME_KEY, folder)
    return True, ""


# ---------------------------------------------------------------------------
# CSS bundle — one <link> per theme, whatever the manifest declares
# ---------------------------------------------------------------------------

def bundle_css(folder: str) -> tuple[str | None, str]:
    """Concatenate the theme's declared stylesheets -> (css, etag).

    One HTTP request instead of N, and — more importantly — one *stable URL
    shape* (``/themes/<folder>/bundle.css``) that base.html's bootstrap script
    can construct from a folder name alone, without knowing the manifest.
    Relative url(...) references inside the CSS keep working because the bundle
    is served from inside the theme's own URL prefix.
    """
    theme = theme_by_folder(folder)
    if theme is None or not theme["valid"] or not theme["stylesheets"]:
        return None, ""
    root = themes_dir() / theme["folder"]
    parts: list[str] = []
    newest = 0.0
    total = 0
    for rel in theme["stylesheets"]:
        path = root / rel
        try:
            stat = path.stat()
            if stat.st_size > MAX_STYLESHEET_BYTES:
                logger.warning("[Themes] %s/%s exceeds %d bytes — skipped",
                               folder, rel, MAX_STYLESHEET_BYTES)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[Themes] cannot read %s/%s: %s", folder, rel, exc)
            continue
        newest = max(newest, stat.st_mtime)
        total += stat.st_size
        parts.append(f"/* --- {rel} --- */\n{text}")
    if not parts:
        return None, ""
    # Unquoted — the route sets it via response.set_etag(), which quotes it.
    etag = f'{theme["version"]}-{int(newest)}-{total}'
    return "\n\n".join(parts), etag


# ---------------------------------------------------------------------------
# Install / uninstall — the store's theme-typed entries land here
# ---------------------------------------------------------------------------

def install_theme_from_staged(staged: Path) -> tuple[bool, str]:
    """Move a verified, staged theme folder into the live themes dir.

    Called by store.install() after download, checksum, signature and
    validate_theme_dir() have all passed on the staged copy. Replacing an
    existing version is done live: a theme is inert data, nothing in the
    process holds it open beyond the duration of a single asset request.
    """
    target = themes_dir() / staged.name
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staged), str(target))
    except OSError as exc:
        # Windows can hold a font file open mid-request; extremely short window,
        # but if it happens the staged copy stays put and the admin can retry.
        logger.exception("[Themes] could not move staged theme '%s' live", staged.name)
        return False, f"could not install theme: {exc}"
    invalidate_cache()
    logger.info("[Themes] installed theme pack '%s'", target.name)
    return True, ""


def uninstall_theme(folder: str) -> dict:
    """Delete an installed theme — live, no restart.

    If it was the instance default, the default reverts to the built-in look
    first, so no request between deletion and the next page load ever links a
    stylesheet that is gone.
    """
    folder = _safe_theme_folder(folder)
    if folder is None:
        return {"ok": False, "error": "invalid theme folder"}
    target = themes_dir() / folder
    if not target.is_dir():
        return {"ok": False, "error": f"no such theme: {folder}"}

    from .db import get_setting, set_setting
    if (get_setting(ACTIVE_THEME_KEY, "") or "").strip() == folder:
        set_setting(ACTIVE_THEME_KEY, "")

    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.exception("[Themes] could not delete theme '%s'", folder)
        return {"ok": False, "error": f"could not delete theme: {exc}"}
    invalidate_cache()
    logger.info("[Themes] uninstalled theme pack '%s'", folder)
    return {"ok": True, "error": None, "restart_required": False}
