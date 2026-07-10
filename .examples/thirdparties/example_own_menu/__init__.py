"""Example: Own Menu — the smallest possible "own sidebar item" integration.

Pattern demonstrated: a page of your own, reachable from a link under one
of the sidebar's three categories (Discover / Management / System). This
is the *minimum* code that gets you that — no caching layer, no extra
settings fields, one route. Compare with:

  - ``example_integration/`` next to this folder — the same pattern, but
    at "real integration" scale (caching, extra_settings, translations).
  - ``example_attach_tab/`` and ``example_new_tab/`` — the opposite case:
    a settings-only extension with *no* sidebar item at all.

See the README's "Settings placement" / folder-layout sections for how
all of these relate.
"""

from flask import Blueprint, redirect, render_template, url_for

from ...db import get_setting
from ..registry import register_thirdparty

SETTING_KEY = "example_own_menu_enabled"

# The four MODULE_* constants the admin Modulmanager page (/extensions)
# reads off every thirdparty's __init__.py -- see web/thirdparties/
# __init__.py's docstring, and example_integration/__init__.py for the
# fuller explanation. Change MODULE_AUTHOR when you copy this folder.
MODULE_NAME = "Example: Own Menu"
MODULE_DESCRIPTION = "Smallest possible \"own sidebar item\" integration -- one Blueprint, one route, one template."
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

bp = Blueprint(
    "example_own_menu",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/example_own_menu/static",
)

# Tag icon — any stroke-based <svg viewBox="0 0 24 24"> works; the sidebar
# renders it at a fixed size and inherits color via stroke="currentColor".
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M20.59 13.41 11 3.83A2 2 0 0 0 9.5 3H4a1 1 0 0 0-1 1v5.5a2 2 0 0 0 .83 1.5L13.41 20.6a2 2 0 0 0 2.83 0l4.35-4.35a2 2 0 0 0 0-2.83Z"></path>'
    '<circle cx="7.5" cy="7.5" r="1.5"></circle>'
    '</svg>'
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


@bp.route("/example-own-menu")
def index():
    """The one page this integration serves. Every page route should start
    with this same enabled-check -- the sidebar link disappears while
    disabled, but the URL itself would still resolve without this guard."""
    if not _enabled():
        return redirect(url_for("index"))
    return render_template("example_own_menu.html")


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    app.register_blueprint(bp)

    register_thirdparty(
        item_id="example_own_menu",
        label="Example: Own Menu",
        endpoint="example_own_menu.index",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        badges=[("Demo", "#2e51a2"), ("Own menu", "#7c3aed")],
        description=(
            "Minimal reference for the \"own sidebar item\" pattern -- one "
            "Blueprint, one route, one template, nothing else. Placed under "
            "Management here (section=\"management\") to show that's just a "
            "keyword, not a special integration type."
        ),
        enable_label="Enable Example: Own Menu",
        enable_desc='Adds an "Example: Own Menu" entry under Management in the sidebar.',
        # This is the one line that differs from example_integration's
        # section="discover" -- everything else about registering a sidebar
        # item is identical regardless of which of the three categories you
        # pick.
        section="management",
    )
