"""Example Integration — registration entry point.

This is the file web/thirdparties/__init__.py's auto-discovery loader
imports: it must expose a ``register(app)`` callable, which is the only
contract a thirdparties/<name>/ folder needs to fulfil to be picked up
automatically (see the parent package's docstring, and the README next to
this folder for the full walkthrough).

Depending on another integration is optional and not needed here, but if
this one *did* need e.g. anime_seasons to already be registered, this file
would also declare (see the README's "Dependencies between integrations"
section):

    DEPENDS_ON = ("anime_seasons",)
"""

from .routes import bp, SETTING_KEY
from ..registry import register_thirdparty

# The MODULE_* constants the admin Modulmanager page (/extensions) reads
# off every thirdparty's __init__.py -- see web/thirdparties/__init__.py's
# docstring. All are optional (see that docstring for the fallback when
# one is omitted); MODULE_ENABLED_DEFAULT only ever applies once, the very
# first time this module is discovered, and never overrides a value a
# user already chose. Change MODULE_AUTHOR when you copy this folder --
# "PD Codes" is only correct for MediaForge's own shipped integrations
# (anime_seasons, mediacalendar).
# MODULE_DESCRIPTION_DE/MODULE_DESCRIPTION_EN are optional per-language
# overrides of MODULE_DESCRIPTION, picked at render time based on the
# admin's current UI language -- declare only the one(s) you need.
MODULE_NAME = "Example Integration"
MODULE_DESCRIPTION = "Minimal reference integration -- own page, own settings card, no external network calls."
MODULE_DESCRIPTION_DE = "Minimale Referenzintegration -- eigene Seite, eigene Einstellungskarte, keine externen Netzwerkaufrufe."
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

# Version + module-store metadata -- see the "Versioning & module-store
# metadata" section of ../README.md. MODULE_VERSION is your module's own
# version (bump it whenever you ship a change; the future module store
# compares exactly this string). MODULE_MIN_APP_VERSION / MODULE_MAX_APP_VERSION
# declare which MediaForge versions this module works on -- they're checked
# against the running app *before* register(app) is called, and a module
# outside the range is skipped with that reason on the Modulmanager page
# rather than loading against an API it wasn't written for. Leave a bound
# empty (or omit it) for "no limit in that direction". MODULE_ID is the
# stable id the store knows this module by, independent of the folder name.
MODULE_VERSION = "1.0.0"
MODULE_MIN_APP_VERSION = "1.1.0"
MODULE_MAX_APP_VERSION = ""
MODULE_ID = "example_integration"
MODULE_HOMEPAGE = ""
MODULE_LICENSE = "MIT"

# Simple clock-face icon — stroke-based style matching every other sidebar
# icon in base.html (stroke="currentColor" so it inherits the sidebar's
# icon color automatically, in both light and dark theme).
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="9"></circle>'
    '<path d="M12 7v5l3.5 2"></path>'
    '</svg>'
)


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    app.register_blueprint(bp)

    register_thirdparty(
        item_id="example_integration",
        label="Example Integration",
        endpoint="example_integration.index",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        badges=[("Demo", "#2e51a2"), ("Menu", "#7c3aed")],
        description=(
            "A minimal, self-contained reference integration that demonstrates "
            "the thirdparties/ plug-in contract end to end: its own Blueprint, "
            "page, cached data source, and translation catalog. Safe to enable "
            "— it only ever shows local placeholder data, no external network "
            "calls."
        ),
        enable_label="Enable Example Integration",
        enable_desc='Adds an "Example Integration" entry under Discover in the sidebar.',
        # extra_settings isn't limited to booleans -- one of each type here
        # purely for reference (see the README's "Richer settings fields"
        # section); a real integration would only add the ones it needs.
        extra_settings=[{
            "key": "example_integration_display_mode",
            "label": "Display mode",
            "description": "Just a demo field -- not read anywhere in service.py.",
            "type": "select",
            "default": "grid",
            "options": [("grid", "Grid"), ("list", "List")],
        }],
        # Both left at their defaults here — spelled out for reference. A
        # real integration might use section="management" or "system"
        # instead (a different sidebar category), or settings_host=
        # "notifications" with settings_tab="discord" to add a toggle onto
        # the existing Discord pill instead of getting its own tab. Or,
        # for a settings-only integration with no page of its own, drop
        # endpoint/icon_svg above entirely and keep just these two.
        section="discover",
        settings_host="integrations",
        settings_tab="thirdparty",
        # Also available (all optional, all left unset here): priority=0
        # to reorder relative to other registered items; dashboard_widget_
        # template="a_template_in_your_own_blueprint.html" for a home-page
        # widget; provider_pill_script=url_for('example_integration.static',
        # filename='pill.js') for a detail-modal/browse-card provider pill.
        # See the README's "Dashboard widgets" / "Provider pills" sections.
    )
