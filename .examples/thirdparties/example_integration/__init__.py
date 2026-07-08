"""Example Integration — registration entry point.

This is the file web/thirdparties/__init__.py's auto-discovery loader
imports: it must expose a ``register(app)`` callable, which is the only
contract a thirdparties/<name>/ folder needs to fulfil to be picked up
automatically (see the parent package's docstring, and the README next to
this folder for the full walkthrough).
"""

from .routes import bp, SETTING_KEY
from ..registry import register_thirdparty

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
    )
