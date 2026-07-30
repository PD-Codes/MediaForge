"""Example Subtitle Source -- reference module for the subtitle extension point.

Shows how a third-party module adds its OWN external subtitle source to the
download path, WITHOUT any change to the core. It registers a settings-only card
on the Integrations page plus one demo source (see source.py), which is asked in
the same place as the built-in OpenSubtitles lookup: the last step of the
subtitle chain, only for languages the finished file is still missing. Safe to
enable -- the demo source never calls out and always returns an empty list.

Copy this folder into ``web/thirdparties/`` (or ship it as a module) to activate
it; the auto-discovery loader picks up any folder exposing ``register(app)``.
"""
from ..registry import register_thirdparty
from ....subtitle_sources import register_subtitle_source
from .source import fetch, ENABLED_KEY

# See ../example_integration/__init__.py for the full meaning of every
# MODULE_* constant. MODULE_DESCRIPTION_DE gives the German card description
# (de-DE); the English MODULE_DESCRIPTION is the en-US source string.
MODULE_NAME = "Example Subtitle Source"
MODULE_DESCRIPTION = ("Reference module for adding an external subtitle source to "
                      "the download path.")
MODULE_DESCRIPTION_DE = ("Referenzmodul zum Hinzufuegen einer externen "
                         "Untertitelquelle im Download-Pfad.")
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

MODULE_VERSION = "1.0.0"
# Registry contract this module targets (bumped by the core when the subtitle
# source API changes in a breaking way).
MODULE_API_VERSION = 1
MODULE_MIN_APP_VERSION = ""
MODULE_MAX_APP_VERSION = ""
MODULE_REQUIREMENTS = ()
MODULE_ID = "example_subtitle_source"
MODULE_HOMEPAGE = ""
MODULE_LICENSE = "MIT"

# The source id the core sees. It must not collide with a built-in
# (subtitle_sources.RESERVED_SOURCE_IDS -- currently {"opensubtitles"}) or with
# another module's source; both raise at registration time.
SOURCE_ID = "example_subs"


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    # A settings-only card (endpoint/icon_svg omitted: this module has no page
    # of its own). settings_host="integrations" always places it on that page's
    # shared "Third Party" tab, next to the built-in OpenSubtitles card.
    register_thirdparty(
        item_id=MODULE_ID,
        label="Example Subtitle Source",
        enabled_setting_key=ENABLED_KEY,
        badges=[("Demo", "#2e51a2"), ("Subtitles", "#0f766e")],
        description=(
            "Reference module demonstrating the subtitle source extension point: "
            "one demo source, asked in the last step of the subtitle chain and "
            "only for languages the file is still missing. Enabling it only "
            "writes a log line -- no external network calls, no subtitles."
        ),
        enable_label="Enable Example Subtitle Source",
        enable_desc="Adds a demo external subtitle source to the download path.",
        settings_host="integrations",
    )

    # One callable per source. It follows the module's master toggle itself
    # (see source.py), so a switched-off module already stops contributing.
    # item_id is what makes the source show up as a capability in the module
    # manager ("subtitle source") and what lets unregister_module() drop it on
    # uninstall, instead of leaving it registered for the rest of the process'
    # life -- always pass it.
    register_subtitle_source(MODULE_ID, SOURCE_ID, "Example Subtitle Service", fetch)
