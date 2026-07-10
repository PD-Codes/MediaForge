"""Example: New Tab — a settings-only integration that gets its own tab.

Pattern demonstrated: like ``example_attach_tab/`` next to this folder, no
page and no sidebar item -- but here ``settings_tab`` does *not* match any
of the ids the host page already renders by hand (see registry.py's
_KNOWN_TABS), so ``resolve_dynamic_tabs()`` creates a brand-new tab for it
automatically instead of appending into an existing one. No template edit
needed either way -- that's the whole point of the two functions being
generic.

Use this pattern for a settings-only extension that's conceptually its own
thing (e.g. a webhook integration, a second API key someone might want to
configure) but doesn't warrant a page of its own to browse/interact with.
If it *did* need a page, see ``example_own_menu/`` / ``example_integration/``
instead.
"""

from ..registry import register_thirdparty

SETTING_KEY = "example_new_tab_enabled"

# The four MODULE_* constants the admin Modulmanager page (/extensions)
# reads off every thirdparty's __init__.py -- see web/thirdparties/
# __init__.py's docstring, and example_integration/__init__.py for the
# fuller explanation. Change MODULE_AUTHOR when you copy this folder.
MODULE_NAME = "Example: New Tab"
MODULE_DESCRIPTION = "Settings-only reference -- no page, no Blueprint, gets a brand-new tab on the Integrations page."
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app). Same as
    example_attach_tab/: no Blueprint, no page, so `app` goes unused here
    too."""
    register_thirdparty(
        item_id="example_new_tab",
        label="Example: New Tab",
        enabled_setting_key=SETTING_KEY,
        badges=[("Demo", "#2e51a2"), ("New tab", "#f59e0b")],
        description=(
            "Settings-only reference: no page, no sidebar item, but its "
            "own brand-new tab on the Integrations page (rather than "
            "landing in the shared \"Third Party\" tab, or attaching into "
            "an existing one like example_attach_tab/ does)."
        ),
        enable_label="Enable Example: New Tab",
        enable_desc="Adds a dedicated \"Example: New Tab\" tab to the Integrations page.",
        extra_settings=[
            {
                "key": "example_new_tab_webhook_url",
                "label": "Webhook URL",
                "description": "Just a demo field -- not read anywhere. A real integration might POST an event here.",
                "type": "text",
                "placeholder": "https://example.com/webhook",
            },
            {
                "key": "example_new_tab_verbose",
                "label": "Verbose logging",
                "description": "Another demo field, to show a tab can mix field types freely.",
                "type": "toggle",
                "default": "0",
            },
        ],
        settings_host="integrations",
        # Doesn't match "seerr"/"mediaplayer"/"cineinfo"/"thirdparty"/
        # "syncplay"/"uptime" -- any id you make up here gets its own new
        # tab, titled from settings_tab_label below.
        settings_tab="example_new_tab",
        settings_tab_label="Example: New Tab",
    )
