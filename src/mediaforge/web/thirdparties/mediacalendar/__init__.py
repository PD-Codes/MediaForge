"""Media Kalender — registration entry point.

This is the file web/thirdparties/__init__.py's auto-discovery loader
imports: it must expose a ``register(app)`` callable, which is the only
contract a thirdparties/<name>/ folder needs to fulfil to be picked up
automatically (see the parent package's docstring). Everything this
integration needs -- its own database (db.py), its own TMDB client reusing
MediaForge's existing CineInfo credential (tmdb_client.py), its own
calendar/list resolution engine (service.py), AniList import
(anilist_client.py), .ics export (ics_export.py), routes/templates/static
(routes.py + templates/ + static/) and translations (translations/) --
lives inside this one folder; nothing outside it is touched.
"""

from .routes import bp, SETTING_KEY
from . import db as mcdb
from . import service
from ..registry import register_thirdparty

# The MODULE_* constants the admin Modulmanager page (/extensions) reads
# off every thirdparty's __init__.py -- see web/thirdparties/__init__.py's
# docstring. Purely descriptive except MODULE_ENABLED_DEFAULT, which only
# ever applies once, the first time this module is discovered.
# MODULE_DESCRIPTION_DE overrides MODULE_DESCRIPTION for admins with the
# UI set to German -- MODULE_DESCRIPTION itself stays the English fallback.
MODULE_NAME = "Media Kalender"
MODULE_DESCRIPTION = "Personal release calendars, curated lists (with AniList import), .ics export and reminders, built on the existing CineInfo TMDB connection."
MODULE_DESCRIPTION_DE = "Persönliche Veröffentlichungskalender, kuratierte Listen (mit AniList-Import), .ics-Export und Erinnerungen, basierend auf der bestehenden CineInfo-TMDB-Anbindung."
MODULE_AUTHOR = "PD Codes"
MODULE_ENABLED_DEFAULT = False

# Simple calendar-grid icon, consistent stroke style with the other
# sidebar icons in base.html.
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="18" rx="2"></rect>'
    '<line x1="16" y1="2" x2="16" y2="6"></line>'
    '<line x1="8" y1="2" x2="8" y2="6"></line>'
    '<line x1="3" y1="10" x2="21" y2="10"></line>'
    '</svg>'
)


def register(app) -> None:
    """Called once by web/thirdparties/discover_and_register(app)."""
    app.register_blueprint(bp)

    try:
        mcdb.init_db()
    except Exception:
        # Don't block the whole app from starting over a DB init hiccup
        # (e.g. a read-only config dir on first boot) -- routes.py's page
        # route retries init_db() on every visit, and record_module_status
        # (via discover_and_register's own try/except) will still show
        # this integration as loaded; a broken DB will simply surface as
        # per-request errors instead of a hard startup failure.
        from ....logger import get_logger
        get_logger(__name__).exception("[MediaCalendar] init_db failed during registration")

    service.start_background_worker()
    service.start_planned_download_worker()
    service.start_calendar_refresh_worker()

    register_thirdparty(
        item_id="mediacalendar",
        label="Media Kalender",
        endpoint="mediacalendar.media_calendar_page",
        icon_svg=_ICON_SVG,
        enabled_setting_key=SETTING_KEY,
        section="discover",
        badges=[("TMDB", "#01b4e4"), ("Calendar", "#555555")],
        description=(
            "Personal release calendars built from saved TMDB filters, curated "
            "lists (including AniList import), .ics export and optional reminder "
            "notifications. Reuses the already-configured CineInfo TMDB "
            "connection -- requires a TMDB API key under Settings -> "
            "Integrations -> CineInfo."
        ),
        enable_label="Enable Media Calendar",
        enable_desc=(
            'Adds a "Media Kalender" entry under Discover in the sidebar. '
            "Requires a configured CineInfo TMDB key -- without one, the page "
            "shows a notice instead of results."
        ),
        extra_settings=[
            {
                "key": "mediacalendar_lookahead_weeks",
                "label": "Lookahead (weeks)",
                "description": "How many weeks ahead calendars resolve releases for.",
                "type": "number",
                "default": "8",
            },
            {
                "key": "mediacalendar_lookback_weeks",
                "label": "Lookback (weeks)",
                "description": "How many weeks in the past calendars also resolve releases for, in addition to the forward-looking lookahead window. 0 keeps calendars showing today onward only.",
                "type": "number",
                "default": "0",
            },
            {
                "key": "mediacalendar_cache_hours",
                "label": "Cache duration",
                "description": "How long resolved calendar results are cached before being refreshed automatically.",
                "type": "select",
                "options": [["6", "6h"], ["12", "12h"], ["24", "24h"], ["48", "48h"]],
                "default": "12",
            },
            {
                "key": "mediacalendar_use_library",
                "label": "Use my media library",
                "description": 'Lets the "My media library" calendar source and library-status badges read your existing library data (a connected Jellyfin/Plex server via MediaScan, and/or MediaForge\'s own native library scan).',
                "type": "toggle",
                "default": "1",
            },
            {
                "key": "mediacalendar_notify_enabled",
                "label": "Reminder notifications",
                "description": "Sends a notification (via MediaForge's configured notification channels) shortly before a release date.",
                "type": "toggle",
                "default": "0",
            },
            {
                "key": "mediacalendar_notify_lead_days",
                "label": "Reminder lead time (days)",
                "description": "How many days in advance to send the reminder.",
                "type": "number",
                "default": "1",
            },
        ],
    )
