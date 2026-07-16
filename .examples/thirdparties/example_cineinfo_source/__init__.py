"""Example CineInfo Source -- reference module for the CineInfo extension point.

Shows how a third-party module adds new CineInfo data (a "source") that layers
on top of the built-in TMDB lookup, WITHOUT any change to the core. It registers
a settings-only card under the CineInfo tab plus two demo sources -- one for each
batch form (see sources.py). Safe to enable: it only ever adds local placeholder
fields, no external network calls.

Copy this folder into ``web/thirdparties/`` (or ship it as a module) to activate
it; the auto-discovery loader picks up any folder exposing ``register(app)``.
"""
from ..registry import register_thirdparty
from ...cineinfo.registry import register_cineinfo_source
from .sources import ExampleLoopSource, ExampleBulkSource, ENABLED_KEY

# See ../example_integration/__init__.py for the full meaning of every
# MODULE_* constant. MODULE_DESCRIPTION_DE gives the German card description
# (de-DE); the English MODULE_DESCRIPTION is the en-US source string.
MODULE_NAME = "Example CineInfo Source"
MODULE_DESCRIPTION = ("Reference module for adding CineInfo sources with automatic "
                      "per-item / bulk batching.")
MODULE_DESCRIPTION_DE = ("Referenzmodul zum Hinzufuegen von CineInfo-Quellen mit "
                         "automatischem Einzel-/Bulk-Batching.")
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

MODULE_VERSION = "1.0.0"
# Registry contract this module targets (bumped by the core when the CineInfo
# source API changes in a breaking way).
MODULE_API_VERSION = 1
MODULE_MIN_APP_VERSION = ""
MODULE_MAX_APP_VERSION = ""
MODULE_REQUIREMENTS = ()
MODULE_ID = "example_cineinfo_source"
MODULE_HOMEPAGE = ""
MODULE_LICENSE = "MIT"


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    # A settings-only card in the existing CineInfo tab: endpoint/icon_svg are
    # deliberately omitted (this module has no page of its own).
    register_thirdparty(
        item_id="example_cineinfo_source",
        label="Example CineInfo Source",
        enabled_setting_key=ENABLED_KEY,
        badges=[("Demo", "#2e51a2"), ("CineInfo", "#7c3aed")],
        description=(
            "Reference module demonstrating the CineInfo source extension point: "
            "two demo sources, one per batch form (per-item vs. single request). "
            "Enabling it adds local placeholder fields to CineInfo lookups -- no "
            "external network calls."
        ),
        enable_label="Enable Example CineInfo Source",
        enable_desc="Adds two demo CineInfo sources (per-item + bulk).",
        settings_host="integrations",
        settings_tab="cineinfo",
    )

    # One source per batch form. Both follow the module's master toggle via
    # their is_enabled() (see sources.py), so nothing extra is needed on
    # uninstall/disable -- a switched-off module simply stops contributing.
    register_cineinfo_source(ExampleLoopSource())
    register_cineinfo_source(ExampleBulkSource())
