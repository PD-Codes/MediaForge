"""Media Kalender -- Third Party integration.

Fully self-contained (own Blueprint, own templates/static/translations, own
service.py/db.py), following the same layout as anime_seasons -- see that
folder's routes.py docstring for the reference pattern this mirrors.

One sidebar entry ("Media Kalender") serving a single page with three
internal sections -- Meine Kalender / Meine Listen / Einstellungen -- swapped
client-side (see static/mediacalendar.js's switchMcTab()), rather than three
separate sidebar entries or Settings-host tabs. The "Einstellungen" section
talks to the *generic* per-item settings API every thirdparty already gets
for free (GET/PUT /api/settings/thirdparty/mediacalendar, wired up by
registry.py's register_generic_settings_routes() from the register_thirdparty()
call in __init__.py) -- no bespoke settings endpoint needed here.
"""

from flask import Blueprint, jsonify, redirect, render_template, request, Response, url_for
from flask_babel import gettext as _gt

from ...db import get_setting
from . import anilist_client, db as mcdb, ics_export, service, tmdb_client as tmdb
from ....config import LANG_LABELS
from ....logger import get_logger
from ...runtime_state import WORKING_PROVIDERS

logger = get_logger(__name__)

SETTING_KEY = "mediacalendar_enabled"

# English source strings static/mediacalendar.js builds its own markup with
# via _mcT()/_mcTStatic() -- see that file's docstring for why this dynamic,
# client-built content (the calendar/list editor, filter-builder accordion,
# card actions, ...) isn't run through Jinja's {{ _() }} like the rest of
# mediacalendar.html. _mcT() already has the lookup seam
# (window.MC_I18N[s] || s) built in; this is what actually populates it --
# every string here is translated via the same German catalog
# (translations/de/LC_MESSAGES/messages.po) the template strings use, then
# handed to the page as window.MC_I18N (see mediacalendar.html) so the JS
# file itself needs zero changes to pick up a translation once one exists
# in the .po file. Keep this list in sync with every _mcT("...")/
# _mcTStatic("...") call in static/mediacalendar.js -- a string missing here
# just falls back to its English source (harmless, but untranslated).
_CLIENT_STRINGS = (
    "Search movie or show...",
    "Movie",
    "TV Show",
    "No results",
    "No calendars yet -- create one to start tracking upcoming releases.",
    "Movies",
    "TV Shows",
    "Open",
    "Edit",
    "Duplicate",
    "Delete",
    "Delete this calendar?",
    "Loading...",
    "Refresh",
    "Export .ics",
    "Nothing found for the current filter.",
    "In library",
    "Requested",
    "Watched",
    "Hide",
    "Edit calendar",
    "New calendar",
    "Name",
    "Color",
    "Media types",
    "Source",
    "TMDB Discover filter",
    "From my lists",
    "Discover filter",
    "From lists",
    "My media library",
    "Genres",
    "Keywords",
    "Search keyword...",
    "Streaming providers",
    "Provider filter mode",
    "Only these providers",
    "Exclude these providers",
    "Linked lists",
    "Combine with filter",
    "Also include Discover results",
    "Adds TMDB Discover matches (genres/keywords/providers above) on top of the linked lists.",
    "Fold in / subtract lists",
    "Manually included / excluded titles",
    "Add title",
    "Always included",
    "Always excluded",
    "Pick a search result, then use the buttons that appear to add it to either list.",
    "Invalid -- not applied. Remove and re-add it from the suggestions below.",
    "Library / request status filter",
    "Library status",
    "Any",
    "Only in my library",
    "Only missing from my library",
    "Request status (Seerr)",
    "Only requested",
    "Only not requested",
    "Add as EXCLUDED? (Cancel = add as always-included)",
    "None",
    "No lists yet -- create one under \"My Lists\" first.",
    "Use these lists",
    "Fold in (positive)",
    "Subtract (negative)",
    "Please enter a name.",
    "No lists yet -- create one to curate titles or import from AniList.",
    "titles",
    "Dynamic",
    "Delete this list?",
    "Remove",
    "No titles added yet.",
    "Titles in this list",
    "Edit list",
    "New list",
    "Name & items",
    "Save first, then add titles.",
    "Dynamic filter",
    "Enable dynamic matching",
    "Also auto-match new TMDB releases against genres/keywords/providers below, in addition to manually added titles.",
    "Import from AniList",
    "AniList username",
    "Import watching + planning",
    "Save the list first, then import from AniList.",
    "Importing...",
    "Added",
    "Unmatched",
    "Lookahead (weeks)",
    "How many weeks ahead calendars resolve releases for.",
    "Lookback (weeks)",
    "How many weeks in the past calendars also resolve releases for, in addition to the forward-looking lookahead window. 0 keeps calendars showing today onward only.",
    "Cache duration",
    "How long resolved calendar results are cached before being refreshed automatically.",
    "Use my media library",
    "Lets the \"My media library\" calendar source and library-status badges read your existing library data (a connected Jellyfin/Plex server via MediaScan, and/or MediaForge's own native library scan).",
    "Reminder notifications",
    "Sends a notification (via MediaForge's configured notification channels) shortly before a release date.",
    "Reminder lead time (days)",
    "How many days in advance to send the reminder.",
    "Add to Auto Sync",
    "Searches AniWorld/S.TO/MegaKino right now and sets up regular Auto-Sync for the whole series immediately if a match is found -- does not wait for this specific release.",
    "Added to Auto Sync.",
    "No matching site found for this title.",
    "Could not add to Auto Sync.",
    # Already in the catalog from the template strings above (Cancel/Save),
    # reused here as-is -- gettext() just looks up the same msgid either way.
    "Cancel",
    "Save",
    # Release calendar (month/week view) -- see static/mediacalendar.js's
    # McCalendars module (mcCalRender* / mcCalActionsHtml / mcCalBadgesHtml).
    "Today",
    "Back",
    "Next",
    "Month",
    "Week",
    "Grid view",
    "List view",
    "more",
    "release",
    "releases",
    "No releases",
    "New",
    "In-Sync",
    "Planned Download",
    "Search in MediaForge",
    "Plan auto-download",
    "Remove planned download",
    # "Planned Downloads" tab + config modal (McPlanned module).
    "Edit planned download",
    "Pending",
    "Failed",
    "Language",
    "Download path",
    "Default path",
    "No planned downloads yet.",
    "Remove this planned download?",
    "Used once this release is found and turned into an Auto-Sync job -- checked hourly from the release date on.",
    "Adds these lists on top of whatever the Discover filter above matches -- with no genre/keyword/provider set, that's an unrestricted (large) result set, not \"nothing\". To show only these lists, switch Source to \"From my lists\" instead.",
)


def _client_i18n():
    """{english source: translated string} for every string in
    _CLIENT_STRINGS, in the currently active locale (see app.py's
    get_locale()) -- rendered into the page as window.MC_I18N (see
    mediacalendar.html), which static/mediacalendar.js's _mcT() already
    knows how to consult.
    """
    return {s: _gt(s) for s in _CLIENT_STRINGS}

bp = Blueprint(
    "mediacalendar",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/mediacalendar/static",
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


def _require_enabled_json():
    if not _enabled():
        return jsonify({"error": "disabled"}), 403
    return None


# --- Pages -------------------------------------------------------------

@bp.route("/media-calendar")
@bp.route("/media-calendar/<tab>")
def media_calendar_page(tab="calendars"):
    if not _enabled():
        return redirect(url_for("index"))
    if tab not in ("calendars", "lists", "planned", "settings"):
        return redirect(url_for(".media_calendar_page"))
    try:
        mcdb.init_db()
    except Exception:
        logger.exception("[MediaCalendar] init_db failed")
    return render_template(
        "mediacalendar.html",
        active_tab=tab,
        tmdb_configured=tmdb.is_configured(),
        mc_i18n=_client_i18n(),
        # Needed by shared_modals.html's series-detail modal (#modal),
        # included on this page so a release's "Search in MediaForge"
        # action (openAniSearchModal -> openSeries -> this modal) has a
        # working language/provider picker -- see app.py's index() route,
        # which populates the exact same two context vars for the same
        # modal on the home page.
        lang_labels=LANG_LABELS,
        sto_lang_labels={"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"},
        supported_providers=WORKING_PROVIDERS,
    )


# --- Status ---------------------------------------------------------------

@bp.route("/api/media-calendar/status")
def api_status():
    return jsonify({
        "enabled": _enabled(),
        "tmdb_configured": tmdb.is_configured(),
    })


# --- Calendars --------------------------------------------------------------

@bp.route("/api/media-calendar/calendars")
def api_calendars_list():
    if (r := _require_enabled_json()) is not None:
        return r
    return jsonify({"calendars": mcdb.list_calendars()})


@bp.route("/api/media-calendar/calendars", methods=["POST"])
def api_calendars_create():
    if (r := _require_enabled_json()) is not None:
        return r
    data = request.get_json(force=True, silent=True) or {}
    if not (data.get("name") or "").strip():
        return jsonify({"error": "name_required"}), 400
    calendar_id = mcdb.save_calendar(data)
    return jsonify({"id": calendar_id, "calendar": mcdb.get_calendar(calendar_id)})


@bp.route("/api/media-calendar/calendars/<int:calendar_id>")
def api_calendar_get(calendar_id):
    if (r := _require_enabled_json()) is not None:
        return r
    calendar = mcdb.get_calendar(calendar_id)
    if not calendar:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"calendar": calendar})


@bp.route("/api/media-calendar/calendars/<int:calendar_id>", methods=["PUT"])
def api_calendar_update(calendar_id):
    if (r := _require_enabled_json()) is not None:
        return r
    if not mcdb.get_calendar(calendar_id):
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if not (data.get("name") or "").strip():
        return jsonify({"error": "name_required"}), 400
    mcdb.save_calendar(data, calendar_id=calendar_id)
    mcdb.clear_cache(calendar_id)
    return jsonify({"calendar": mcdb.get_calendar(calendar_id)})


@bp.route("/api/media-calendar/calendars/<int:calendar_id>", methods=["DELETE"])
def api_calendar_delete(calendar_id):
    if (r := _require_enabled_json()) is not None:
        return r
    mcdb.delete_calendar(calendar_id)
    return jsonify({"ok": True})


@bp.route("/api/media-calendar/calendars/<int:calendar_id>/duplicate", methods=["POST"])
def api_calendar_duplicate(calendar_id):
    if (r := _require_enabled_json()) is not None:
        return r
    new_id = mcdb.duplicate_calendar(calendar_id)
    if new_id is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"id": new_id, "calendar": mcdb.get_calendar(new_id)})


@bp.route("/api/media-calendar/calendars/<int:calendar_id>/releases")
def api_calendar_releases(calendar_id):
    if (r := _require_enabled_json()) is not None:
        return r
    if not mcdb.get_calendar(calendar_id):
        return jsonify({"error": "not_found"}), 404
    force = request.args.get("refresh") == "1"
    result = service.resolve_calendar(calendar_id, force_refresh=force)
    status = 200 if result["error"] is None else 502
    return jsonify(result), status


@bp.route("/api/media-calendar/calendars/<int:calendar_id>/ics")
def api_calendar_ics(calendar_id):
    if not _enabled():
        return jsonify({"error": "disabled"}), 403
    calendar = mcdb.get_calendar(calendar_id)
    if not calendar:
        return jsonify({"error": "not_found"}), 404
    result = service.resolve_calendar(calendar_id, force_refresh=False)
    ics_text = ics_export.build_ics(calendar["name"], result["releases"])
    filename = f"mediacalendar-{calendar_id}.ics"
    return Response(
        ics_text, mimetype="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Lists -----------------------------------------------------------------

@bp.route("/api/media-calendar/lists")
def api_lists_list():
    if (r := _require_enabled_json()) is not None:
        return r
    return jsonify({"lists": mcdb.list_lists()})


@bp.route("/api/media-calendar/lists", methods=["POST"])
def api_lists_create():
    if (r := _require_enabled_json()) is not None:
        return r
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name_required"}), 400
    list_id = mcdb.create_list(name)
    return jsonify({"id": list_id, "list": mcdb.get_list(list_id)})


@bp.route("/api/media-calendar/lists/<int:list_id>")
def api_list_get(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    list_row = mcdb.get_list(list_id)
    if not list_row:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"list": list_row})


@bp.route("/api/media-calendar/lists/<int:list_id>", methods=["PUT"])
def api_list_update(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    if not mcdb.get_list(list_id):
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if not (data.get("name") or "").strip():
        return jsonify({"error": "name_required"}), 400
    mcdb.save_list_settings(list_id, data)
    return jsonify({"list": mcdb.get_list(list_id)})


@bp.route("/api/media-calendar/lists/<int:list_id>", methods=["DELETE"])
def api_list_delete(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    mcdb.delete_list(list_id)
    return jsonify({"ok": True})


@bp.route("/api/media-calendar/lists/<int:list_id>/items", methods=["POST"])
def api_list_add_item(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    if not mcdb.get_list(list_id):
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    tmdb_id, media_type = data.get("tmdb_id"), data.get("media_type")
    if not tmdb_id or media_type not in ("movie", "tv"):
        return jsonify({"error": "invalid_item"}), 400
    mcdb.add_list_item(list_id, int(tmdb_id), media_type, data.get("title") or "",
                        data.get("poster_path"), data.get("release_date"))
    return jsonify({"list": mcdb.get_list(list_id)})


@bp.route("/api/media-calendar/lists/<int:list_id>/items/<media_type>/<int:tmdb_id>", methods=["DELETE"])
def api_list_remove_item(list_id, media_type, tmdb_id):
    if (r := _require_enabled_json()) is not None:
        return r
    mcdb.remove_list_item(list_id, tmdb_id, media_type)
    return jsonify({"list": mcdb.get_list(list_id)})


@bp.route("/api/media-calendar/lists/<int:list_id>/dynamic-preview")
def api_list_dynamic_preview(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    list_row = mcdb.get_list(list_id)
    if not list_row:
        return jsonify({"error": "not_found"}), 404
    if not tmdb.is_configured():
        return jsonify({"error": "tmdb_not_configured", "releases": []}), 502
    from datetime import date, timedelta
    date_from = date.today().isoformat()
    date_to = (date.today() + timedelta(weeks=service.lookahead_weeks())).isoformat()
    try:
        releases = service.resolve_list_dynamic(list_row, date_from, date_to)
    except tmdb.TmdbError as exc:
        return jsonify({"error": str(exc), "releases": []}), 502
    return jsonify({"releases": releases})


@bp.route("/api/media-calendar/lists/<int:list_id>/anilist-import", methods=["POST"])
def api_list_anilist_import(list_id):
    if (r := _require_enabled_json()) is not None:
        return r
    if not mcdb.get_list(list_id):
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    statuses = data.get("statuses") or list(anilist_client.DEFAULT_STATUSES)
    try:
        result = anilist_client.import_to_list(list_id, username, statuses)
    except (anilist_client.AniListError, tmdb.TmdbNotConfigured) as exc:
        return jsonify({"error": str(exc)}), 502
    result["list"] = mcdb.get_list(list_id)
    return jsonify(result)


# --- TMDB helpers (for the filter-builder UI) -------------------------------

@bp.route("/api/media-calendar/tmdb/genres")
def api_tmdb_genres():
    if (r := _require_enabled_json()) is not None:
        return r
    if not tmdb.is_configured():
        return jsonify({"error": "tmdb_not_configured", "genres": []}), 502
    media_type = request.args.get("media_type", "movie")
    try:
        return jsonify({"genres": tmdb.get_genres(media_type)})
    except tmdb.TmdbError as exc:
        return jsonify({"error": str(exc), "genres": []}), 502


@bp.route("/api/media-calendar/tmdb/keywords")
def api_tmdb_keywords():
    if (r := _require_enabled_json()) is not None:
        return r
    if not tmdb.is_configured():
        return jsonify({"error": "tmdb_not_configured", "keywords": []}), 502
    query = request.args.get("q", "")
    try:
        return jsonify({"keywords": tmdb.search_keywords(query)})
    except tmdb.TmdbError as exc:
        return jsonify({"error": str(exc), "keywords": []}), 502


@bp.route("/api/media-calendar/tmdb/providers")
def api_tmdb_providers():
    if (r := _require_enabled_json()) is not None:
        return r
    if not tmdb.is_configured():
        return jsonify({"error": "tmdb_not_configured", "providers": []}), 502
    media_type = request.args.get("media_type", "movie")
    try:
        return jsonify({"providers": tmdb.get_watch_providers(media_type)})
    except tmdb.TmdbError as exc:
        return jsonify({"error": str(exc), "providers": []}), 502


@bp.route("/api/media-calendar/tmdb/search")
def api_tmdb_search():
    if (r := _require_enabled_json()) is not None:
        return r
    if not tmdb.is_configured():
        return jsonify({"error": "tmdb_not_configured", "results": []}), 502
    query = request.args.get("q", "")
    try:
        return jsonify({"results": tmdb.search_multi(query)})
    except tmdb.TmdbError as exc:
        return jsonify({"error": str(exc), "results": []}), 502


# --- Planned downloads ("auto-download once available") ---------------------
# See db.py's planned_downloads table and service.py's
# start_planned_download_worker() for the hourly re-search-until-found loop
# this just flags releases for.

@bp.route("/api/media-calendar/planned")
def api_planned_downloads_list():
    """Every planned download regardless of status -- feeds the "Planned
    Downloads" management tab (static/mediacalendar.js's McPlanned module),
    which lists them with their current status/language/path and lets the
    user edit or remove each one."""
    if (r := _require_enabled_json()) is not None:
        return r
    return jsonify({"planned": mcdb.list_all_planned_downloads()})


@bp.route("/api/media-calendar/planned/<media_type>/<int:tmdb_id>", methods=["POST"])
def api_planned_download_add(media_type, tmdb_id):
    """Flag a release for the planned-download worker, or -- called again
    with the same tmdb_id/media_type/season/episode -- edit its language/
    path config (see db.py's add_planned_download docstring for why one
    endpoint covers both "add" and "edit")."""
    if (r := _require_enabled_json()) is not None:
        return r
    data = request.get_json(silent=True) or {}
    mcdb.add_planned_download(
        tmdb_id, media_type,
        int(data.get("season_number", -1)), int(data.get("episode_number", -1)),
        data.get("title"), data.get("release_date"),
        poster_path=data.get("poster_path"),
        language=(data.get("language") or "German Dub"),
        custom_path_id=(int(data["custom_path_id"]) if data.get("custom_path_id") not in (None, "") else None),
    )
    return jsonify({"ok": True})


@bp.route("/api/media-calendar/planned/<media_type>/<int:tmdb_id>", methods=["DELETE"])
def api_planned_download_remove(media_type, tmdb_id):
    if (r := _require_enabled_json()) is not None:
        return r
    season_number = int(request.args.get("season_number", -1))
    episode_number = int(request.args.get("episode_number", -1))
    mcdb.remove_planned_download(tmdb_id, media_type, season_number, episode_number)
    return jsonify({"ok": True})


@bp.route("/api/media-calendar/autosync", methods=["POST"])
def api_add_to_autosync():
    """"Add to Auto Sync" -- the alternative to "Plan auto-download" on a
    calendar release (static/mediacalendar.js's mcCalActionsHtml
    "add-autosync" button, wired through McPlanned's shared language/path
    modal in "autosync" mode). Searches for a site match right now and
    sets up regular Auto-Sync immediately -- no planned_downloads row
    involved, see service.py's add_title_to_autosync()."""
    if (r := _require_enabled_json()) is not None:
        return r
    data = request.get_json(silent=True) or {}
    result = service.add_title_to_autosync(
        data.get("title"), data.get("language"),
        int(data["custom_path_id"]) if data.get("custom_path_id") not in (None, "") else None,
    )
    status = 200 if result.get("ok") else 409
    return jsonify(result), status


# --- Watched / hidden --------------------------------------------------------

@bp.route("/api/media-calendar/progress", methods=["POST"])
def api_progress_set():
    if (r := _require_enabled_json()) is not None:
        return r
    data = request.get_json(force=True, silent=True) or {}
    try:
        tmdb_id = int(data["tmdb_id"])
        media_type = data["media_type"]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "invalid_request"}), 400
    season_number = int(data.get("season_number", -1))
    episode_number = int(data.get("episode_number", -1))
    watched = data.get("watched")
    hidden = data.get("hidden")
    service.mark_watched(tmdb_id, media_type, season_number, episode_number, watched) \
        if watched is not None else None
    service.mark_hidden(tmdb_id, media_type, season_number, episode_number, hidden) \
        if hidden is not None else None
    return jsonify({"ok": True})
