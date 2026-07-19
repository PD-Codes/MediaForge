"""Example Content Source -- reference module for adding a whole new
streaming site as a module: providers.register_provider(),
search.register_search_source(), mirrors.register_site_mirrors() and
web.uptime_monitor.register_monitor_site() together.

Settings-only (no page of its own, like example_cineinfo_source/): the
built-in search bar and URL-paste flows are the "page" this content source
uses. Safe to enable -- source.py makes no network calls at all, everything
is synthesized locally (see its docstring).

Copy this folder into ``web/thirdparties/`` (or ship it as a module) to
activate it; the auto-discovery loader picks up any folder exposing
``register(app)``.

IMPORTANT caveat (also called out in .examples/thirdparties/README.md's
"Content sources" section): this wires up URL resolution
(providers.resolve_provider(), so a pasted example-source.invalid/... URL
works everywhere a built-in one does) and the POST /api/search route
itself. It does NOT currently make the homepage's "quick search" or the
Advanced Search page's UI *ask* for this site -- static/app.js's search
functions still loop over a fixed list of built-in site ids
(aniworld/sto/filmpalast/megakino/hanime). Until that's generalized, a
registered content source's results are reachable via a direct
POST /api/search {"site": "example_source", "keyword": "..."} call (or a
module's own page, if it builds one), but not automatically from the main
search bar. This is a known, documented gap -- not something you did wrong
if your module's results don't show up there yet.
"""
from ..registry import register_thirdparty
from ...providers import Provider, register_provider
from ...search import register_search_source
from ...mirrors import register_site_mirrors
from ..uptime_monitor import register_monitor_site
from .source import (
    ExampleSourceSeries,
    ExampleSourceEpisode,
    SERIES_PATTERN,
    EPISODE_PATTERN,
    search as _search,
)

ITEM_ID = "example_content_source"
SITE_ID = "example_source"
ENABLED_KEY = "example_content_source_enabled"

# See ../example_integration/__init__.py for the full meaning of every
# MODULE_* constant.
MODULE_NAME = "Example Content Source"
MODULE_DESCRIPTION = ("Reference module for adding a whole new streaming site: "
                      "register_provider + register_search_source + register_site_mirrors "
                      "+ register_monitor_site together. No network calls -- fully offline-safe.")
MODULE_DESCRIPTION_DE = ("Referenzmodul zum Hinzufuegen einer komplett neuen Streaming-Seite: "
                         "register_provider + register_search_source + register_site_mirrors "
                         "+ register_monitor_site zusammen. Keine Netzwerkzugriffe -- komplett offline-sicher.")
MODULE_AUTHOR = "Your Name"
MODULE_ENABLED_DEFAULT = False

MODULE_VERSION = "1.0.0"
MODULE_API_VERSION = 1
MODULE_MIN_APP_VERSION = ""
MODULE_MAX_APP_VERSION = ""
MODULE_REQUIREMENTS = ()
MODULE_ID = "example_content_source"
MODULE_HOMEPAGE = ""
MODULE_LICENSE = "MIT"


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    # A settings-only card (endpoint/icon_svg omitted -- this module has no
    # page of its own, see the module docstring for why). Gets its own
    # dynamic tab, same as example_cineinfo_source/, so it shows up as a
    # sidebar sub-menu entry + overview tile on the Integrations page.
    register_thirdparty(
        item_id=ITEM_ID,
        label="Example Content Source",
        enabled_setting_key=ENABLED_KEY,
        badges=[("Demo", "#2e51a2"), ("Content source", "#7c3aed")],
        description=(
            "Reference module demonstrating the content-source extension points "
            "together: a new streaming site's URL resolution, search, domain "
            "fallback and UpTime tracking. Fully offline-safe -- no network calls."
        ),
        enable_label="Enable Example Content Source",
        enable_desc="Registers a demo streaming site (example-source.invalid) with one series, three episodes.",
        settings_host="integrations",
        settings_tab="example_content_source",
        settings_tab_label="Example Content Source",
        overview_description="Reference module: a whole demo streaming site, fully offline (no network calls).",
        overview_icon_svg=(
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="12" r="10"></circle>'
            '<path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"></path></svg>'
        ),
    )

    # 1. URL resolution: a pasted https://example-source.invalid/serie/... URL
    #    now resolves through providers.resolve_provider() exactly like a
    #    built-in site's URL would.
    register_provider(ITEM_ID, Provider(
        name="ExampleSource",
        series_pattern=SERIES_PATTERN,
        episode_pattern=EPISODE_PATTERN,
        series_cls=ExampleSourceSeries,
        episode_cls=ExampleSourceEpisode,
    ))

    # 2. Search bar backend: POST /api/search {"site": "example_source", ...}
    #    (see the module docstring for the current frontend-wiring caveat).
    register_search_source(ITEM_ID, site_id=SITE_ID, search_fn=_search, label="Example Source")

    # 3. Domain fallback -- purely illustrative here (example-source.invalid
    #    and its "mirror" both resolve to nothing real), but shows up as a
    #    real, editable card under Settings -> Sources -> "Domain fallback
    #    (mirrors)" exactly like a real site's would.
    register_site_mirrors(
        ITEM_ID, SITE_ID,
        ["example-source.invalid", "example-source-mirror.invalid"],
        label="Example Source",
    )

    # 4. UpTime tracking -- also illustrative (the probe will report this
    #    site as unreachable, since .invalid never resolves); shows the
    #    demo site as its own card on the UpTime dashboard. A real module
    #    points url/expected_domain/body_markers/expected_headers at the
    #    actual site, the same fields a built-in _MONITOR_SITES entry has.
    register_monitor_site(
        ITEM_ID, SITE_ID, "Example Source",
        url="https://example-source.invalid",
        expected_domain="example-source.invalid",
        body_markers=["example"],
        expected_headers={"server": "cloudflare"},
        enabled_setting_key=ENABLED_KEY,
        tracked_by_default=False,  # off by default: it will always show "down"
    )
