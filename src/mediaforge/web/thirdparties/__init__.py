"""Auto-discovery for third-party integrations.

Every subfolder here that exposes a ``register(app)`` callable in its
``__init__.py`` is imported and registered automatically by
:func:`discover_and_register`, called once from app.py's ``create_app()``.

Adding a new integration means creating a new subfolder — nothing in
app.py, base.html or integrations.html needs to change. See
``web/thirdparties/anime_seasons/`` for a full worked example (its own
Blueprint with its own templates/static, its own service module, its own
translations/ catalog, and one ``register_thirdparty(...)`` call into
``registry.py`` for the sidebar entry + settings card), and
``web/thirdparties/registry.py`` for the shared sidebar/settings-card hook
every integration plugs into. A from-scratch, heavily-commented template
lives outside the installed package at ``.examples/thirdparties/`` in the
repo root.

Translations are modular too: :func:`discover_translation_dirs` is a plain
filesystem scan (no imports) so it can run very early — before Flask-Babel
is initialized in app.py — and feed ``BABEL_TRANSLATION_DIRECTORIES``. Any
subfolder with its own ``translations/<locale>/LC_MESSAGES/messages.mo``
gets merged into the app's translation catalog automatically; an
integration that adds no new strings simply has no translations/ folder.
"""

import importlib
import pkgutil
from pathlib import Path

from .registry import register_generic_settings_routes
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


def discover_and_register(app) -> None:
    """Import every subpackage of web/thirdparties/ and call its
    register(app), then wire up the shared settings-toggle API once."""
    package_dir = Path(__file__).parent
    names = sorted(
        name for _finder, name, is_pkg in pkgutil.iter_modules([str(package_dir)])
        if is_pkg and not name.startswith("_")
    )
    for name in names:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception:
            logger.exception("[Thirdparties] Failed to import '%s'", name)
            continue
        register_fn = getattr(module, "register", None)
        if not callable(register_fn):
            logger.warning("[Thirdparties] '%s' has no register(app) callable — skipped", name)
            continue
        try:
            register_fn(app)
            logger.info("[Thirdparties] Registered integration: %s", name)
        except Exception:
            logger.exception("[Thirdparties] register(app) failed for '%s'", name)

    # Shared enable/disable API for the simple "just a toggle" settings card
    # every registered thirdparty gets automatically — see registry.py.
    register_generic_settings_routes(app)
