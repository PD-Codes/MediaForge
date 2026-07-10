"""Example: Attach Tab — the smallest possible "settings-only" integration.

Pattern demonstrated: an extension with *no page and no sidebar item at
all* -- just a settings card, appended into an EXISTING tab/pill that
already renders its own content by hand. No Blueprint, no routes.py, no
templates/, no static/: the generic ``GET/PUT /api/settings/thirdparty/
<item_id>`` pair (registered once, shared by every integration) is enough
to read and write this card's fields, so there's nothing left to write.

Concretely: this appends one extra toggle into the Notifications page's
existing "ntfy" pill (``settings_host="notifications",
settings_tab="ntfy"``) -- as if this were, say, a companion extension that
piggybacks an extra option onto ntfy notifications without being a
notification channel itself. Compare with ``example_new_tab/`` next to
this folder, which is the same idea but creates a *brand-new* tab instead
of attaching to an existing one; and with ``example_own_menu/`` /
``example_integration/``, which both add a sidebar item and a page.

Because there's no Blueprint here, this integration's `register(app)` has
nothing to do with `app` at all beyond the shared registry call -- that's
expected and fine, `app` is simply unused.
"""

from ..registry import register_thirdparty

SETTING_KEY = "example_attach_tab_enabled"

# The four MODULE_* constants the admin Modulmanager page (/extensions)
# reads off every thirdparty's __init__.py -- see web/thirdparties/
# __init__.py's docstring, and example_integration/__init__.py for the
# fuller explanation. Change MODULE_AUTHOR when you copy this folder.
MODULE_NAME = "Example: Attach Tab"
MODULE_DESCRIPTION = "Settings-only reference -- no page, no Blueprint, just a card attached into the existing ntfy notification pill."
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app). No
    app.register_blueprint(...) call here -- there is no Blueprint,
    because there's no page or API route to serve."""
    register_thirdparty(
        item_id="example_attach_tab",
        label="Example: Attach Tab",
        # No endpoint / icon_svg -- omitting both (rather than setting only
        # one, which raises ValueError) means "no page, no sidebar item".
        enabled_setting_key=SETTING_KEY,
        badges=[("Demo", "#2e51a2"), ("Attach tab", "#0ea5e9")],
        description=(
            "Settings-only reference: no page, no sidebar item, just a "
            "card appended into the Notifications page's existing \"ntfy\" "
            "pill. Demonstrates that a card can live entirely inside "
            "someone else's tab instead of getting its own."
        ),
        enable_label="Enable Example: Attach Tab",
        enable_desc="Adds one extra demo toggle into the existing ntfy notification card.",
        extra_settings=[{
            "key": "example_attach_tab_demo_field",
            "label": "Demo toggle (attached to ntfy)",
            "description": "Just a demo field -- not read anywhere. Shows up inside the ntfy pill, not in a tab of its own.",
            "type": "toggle",
            "default": "0",
        }],
        # settings_tab="ntfy" matches an id notifications.html already
        # renders by hand (see registry.py's _KNOWN_TABS) -- that's what
        # makes this attach instead of creating a new pill.
        settings_host="notifications",
        settings_tab="ntfy",
    )
