"""Anime Seasons — registration entry point.

This is the file web/thirdparties/__init__.py's auto-discovery loader
imports: it must expose a ``register(app)`` callable, which is the only
contract a thirdparties/<name>/ folder needs to fulfil to be picked up
automatically (see the parent package's docstring).
"""

from .routes import ADULT_SETTING_KEY, bp, SETTING_KEY
from ..registry import register_thirdparty

# The MODULE_* constants the admin Modulmanager page (/extensions) reads
# off every thirdparty's __init__.py -- see web/thirdparties/__init__.py's
# docstring. Purely descriptive except MODULE_ENABLED_DEFAULT, which only
# ever applies once, the first time this module is discovered.
# MODULE_DESCRIPTION_DE overrides MODULE_DESCRIPTION for admins with the
# UI set to German -- MODULE_DESCRIPTION itself stays the English fallback.
MODULE_NAME = "Anime Seasons"
MODULE_DESCRIPTION = "Seasonal anime listings from Jikan (MyAnimeList), enriched with the same provider pills as the home page."
MODULE_DESCRIPTION_DE = "Saisonale Anime-Übersichten von Jikan (MyAnimeList), angereichert mit den gleichen Anbieter-Badges wie auf der Startseite."
MODULE_AUTHOR = "PD Codes"
MODULE_ENABLED_DEFAULT = False

# Version + module-store metadata. MODULE_VERSION is this module's own
# version (bump it on every change that ships), shown as a badge on the
# Modulmanager page and, later, what the module store compares against.
# MODULE_MIN_APP_VERSION is the compatibility floor checked against
# MediaForge's own version before register(app) runs (see
# registry.check_app_compatibility()); no ceiling is declared, so any newer
# MediaForge is assumed fine until a breaking change makes it not.
MODULE_VERSION = "1.0.0"
MODULE_MIN_APP_VERSION = "1.1.0"
MODULE_ID = "anime_seasons"
MODULE_HOMEPAGE = "https://github.com/PD-Codes/MediaForge"
MODULE_LICENSE = "GPL-3.0"

# 2x2 grid icon — four quadrants standing in for the four seasons; same
# stroke-based style as every other sidebar icon in base.html.
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="7" height="7"></rect>'
    '<rect x="14" y="3" width="7" height="7"></rect>'
    '<rect x="14" y="14" width="7" height="7"></rect>'
    '<rect x="3" y="14" width="7" height="7"></rect>'
    '</svg>'
)


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    app.register_blueprint(bp)

    register_thirdparty(
        item_id="anime_seasons",
        label="Anime Seasons",
        endpoint="anime_seasons.anime_seasons_page",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        badges=[("Jikan", "#2e51a2"), ("Anime", "#555555"), ("Menu", "#7c3aed")],
        description=(
            "Fetches the current and three preceding anime seasons from Jikan "
            "(an unofficial MyAnimeList API) and shows them as a browsable "
            "overview, enriched with the same CineInfo/Crunchyroll/"
            "Fernsehserien.de pills as the home page. Clicking a title "
            "searches your configured sources, just like Advanced Search."
        ),
        enable_label="Enable Anime Seasons",
        enable_desc='Adds an "Anime Seasons" entry under Discover in the sidebar.',
        extra_settings=[{
            "key": ADULT_SETTING_KEY,
            "label": "Show adult content",
            "description": "Shows Hentai/adult-rated (MAL rating \"Rx\") entries in the season grid. Off by default.",
            "default": "0",
        }],
    )
