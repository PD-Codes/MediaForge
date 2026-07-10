"""Example: Advanced Placement -- demonstrates four register_thirdparty()
capabilities not covered by the other examples in this folder:

1. ``section="syncplay"`` -- a fourth sidebar category (alongside Discover /
   Management / System) for entries that only make sense while SyncPlay
   itself is enabled. Gated exactly like the built-in SyncPlay link: this
   page's sidebar entry only renders while *both* SyncPlay
   (``syncplay_enabled`` setting) and this integration's own
   ``enabled_setting_key`` are on.
2. ``settings_host="settings"`` -- placing a settings card on the main
   Settings page's tab bar instead of Integrations/Notifications. Here it
   creates a brand-new tab (its ``settings_tab`` doesn't match one of
   Settings' existing ids), the same way ``example_new_tab/`` does for the
   Integrations page.
3. ``requires_enabled`` -- a soft, *runtime* dependency on another
   registered item (``example_own_menu``) being currently switched on, not
   just installed. Unlike the module-level ``DEPENDS_ON`` constant (checked
   once at import time), this is re-checked on every request: if someone
   later disables the dependency, this item's sidebar link disappears too,
   no restart needed. See ``web/thirdparties/registry.py``'s
   ``dependencies_satisfied()``.
4. ``auth_required="admin"`` -- every route this integration's Blueprint
   registers is wrapped with ``admin_required`` instead of the default
   ``login_required``, declaratively, without needing an entry added to
   ``app.py``'s hand-maintained admin set by hand.

Copy this folder if you're building something that (a) only makes sense
alongside SyncPlay, (b) belongs on the Settings page rather than
Integrations/Notifications, (c) depends on another integration being
switched on to actually do anything, or (d) should be admin-only. Combine
only the pieces you actually need -- these four are independent axes, not
a package deal (see the README's parameter table).
"""

from flask import Blueprint, redirect, render_template, url_for

from ...db import get_setting
from ..registry import register_thirdparty

SETTING_KEY = "example_advanced_enabled"

MODULE_NAME = "Example: Advanced Placement"
MODULE_DESCRIPTION = (
    "Demonstrates section=\"syncplay\", settings_host=\"settings\", "
    "requires_enabled and auth_required=\"admin\" together."
)
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

bp = Blueprint(
    "example_advanced",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/example_advanced/static",
)

_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2 2 7l10 5 10-5-10-5Z"></path>'
    '<path d="M2 17l10 5 10-5"></path>'
    '<path d="M2 12l10 5 10-5"></path>'
    '</svg>'
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


@bp.route("/example-advanced")
def index():
    """This integration's only page. auth_required="admin" below means
    this route (and any other this Blueprint registers) is wrapped with
    admin_required instead of login_required -- a non-admin logged-in user
    gets redirected, same as visiting /settings directly."""
    if not _enabled():
        return redirect(url_for("index"))
    return render_template("example_advanced.html")


def register(app) -> None:
    app.register_blueprint(bp)

    register_thirdparty(
        item_id="example_advanced",
        label="Example: Advanced Placement",
        endpoint="example_advanced.index",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        badges=[("Demo", "#2e51a2"), ("Advanced", "#7c3aed")],
        description=(
            "Own page under SyncPlay in the sidebar, a settings card on a "
            "brand-new Settings-page tab, a soft runtime dependency on "
            "Example: Own Menu, and admin-only routes -- see this folder's "
            "__init__.py for how each of the four is wired up."
        ),
        enable_label="Enable Example: Advanced Placement",
        enable_desc='Adds an "Example: Advanced Placement" entry under SyncPlay in the sidebar (while SyncPlay itself is enabled).',
        # 1. Fourth sidebar category -- only rendered while syncplay_enabled
        # is on, exactly like the built-in SyncPlay link (see base.html).
        section="syncplay",
        # 2. Settings page instead of Integrations/Notifications. "advanced"
        # isn't one of Settings' existing tab ids (general/design/sources/
        # downloads/autosync/network/auth/api/updates -- see registry.py's
        # _KNOWN_TABS), so this creates a brand-new tab automatically.
        settings_host="settings",
        settings_tab="advanced",
        settings_tab_label="Advanced",
        # 3. Soft runtime dependency -- this item's sidebar link disappears
        # the moment example_own_menu's own toggle is switched off, without
        # needing a restart (checked fresh on every request). Compare with
        # DEPENDS_ON in a module-level constant, which only ever runs once
        # at startup import time.
        requires_enabled=("example_own_menu",),
        # 4. Every route example_advanced's Blueprint registers (just index()
        # here) is wrapped with admin_required instead of login_required.
        # blueprint= isn't needed explicitly since it's inferred from
        # endpoint ("example_advanced.index" -> blueprint "example_advanced").
        auth_required="admin",
    )
