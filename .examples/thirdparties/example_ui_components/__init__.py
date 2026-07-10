"""Example: UI Components — a live gallery of MediaForge's core UI pieces.

Not a "how to register a thirdparty" example like the other four folders
next to this one (those cover sidebar/settings placement) -- this one is
purely a visual reference for the CSS classes and JS helpers already built
into MediaForge's core (loaded globally via base.html), so a new
integration's own templates look native instead of reinventing badges,
buttons, toggles, etc. from scratch.

Every component shown on this page is a *core* class (defined in
web/static/*.css, most already loaded globally by base.html -- see this
folder's routes.py for exactly which ones need an explicit <link>), not
something this example folder invents. Copy the markup straight out of
templates/example_ui_components.html.
"""

from flask import Blueprint, redirect, render_template, url_for

from ...db import get_setting
from ..registry import register_thirdparty

SETTING_KEY = "example_ui_components_enabled"

MODULE_NAME = "Example: UI Components"
MODULE_DESCRIPTION = "Live gallery of core badges/pills/toggles/buttons/etc. for plugin authors to copy from."
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

bp = Blueprint(
    "example_ui_components",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/example_ui_components/static",
)

# Puzzle-piece icon -- same stroke convention as every other sidebar icon.
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 1 0-3.214 3.214c.446.166.855.497.925.968a.979.979 0 0 1-.276.837l-1.61 1.61a2.404 2.404 0 0 1-1.705.707c-.617 0-1.234-.235-1.705-.707l-1.568-1.568a1.026 1.026 0 0 0-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 1 1-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 0 0-.289-.877l-1.568-1.568A2.402 2.402 0 0 1 1.998 12c0-.617.236-1.234.706-1.704L4.23 8.77c.24-.24.581-.353.917-.303.515.077.877.528 1.073 1.01a2.5 2.5 0 1 0 3.259-3.259c-.482-.196-.933-.558-1.01-1.073-.05-.336.062-.676.303-.917l1.525-1.525A2.402 2.402 0 0 1 12 2c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 1 1 3.237 3.237c-.464.18-.894.527-.967 1.02Z"></path>'
    '</svg>'
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


@bp.route("/example-ui-components")
def index():
    if not _enabled():
        return redirect(url_for("index"))
    return render_template("example_ui_components.html")


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    app.register_blueprint(bp)

    register_thirdparty(
        item_id="example_ui_components",
        label="Example: UI Components",
        endpoint="example_ui_components.index",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        section="management",
        badges=[("Demo", "#2e51a2"), ("Reference", "#0ea5e9")],
        description=(
            "Not a real integration -- a live, copy-from-able gallery of "
            "MediaForge's existing core UI classes (badges, pills, toggles, "
            "the +/- number stepper, buttons, empty states, ...), so new "
            "integrations reuse the same look instead of writing new CSS."
        ),
        enable_label="Enable Example: UI Components",
        enable_desc='Adds an "Example: UI Components" entry under Management in the sidebar.',
    )
