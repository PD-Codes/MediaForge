"""Anime Seasons — Third Party integration (Jikan / MyAnimeList v4 API).

Fully self-contained: its own Blueprint (own templates/ and static/ folders,
so nothing lives in the shared web/templates or web/static trees), its own
service module (service.py), and its own register(app) entry point (see
__init__.py) that plugs into ..registry for the sidebar entry and the
Integrations -> Third Party settings card. This is the reference example
for how a new thirdparties/<name>/ integration should be laid out.

Shows the last 4 anime seasons (current + 3 preceding, see .service) as a
"classic overview" grid — same visual language as the Home/Browse and
Calendar pages, enriched client-side with the same CineInfo (TMDB)/
Crunchyroll/Fernsehserien.de pills the Browse cards use (see
static/anime_seasons_view.js).
"""

from flask import Blueprint, jsonify, redirect, render_template, url_for
from flask_babel import gettext as _gt

from ...db import get_setting
from .service import SEASON_LABELS, get_season, get_season_slots, is_adult_entry, slot_for_slug
from ....logger import get_logger

logger = get_logger(__name__)

SETTING_KEY = "anime_seasons_enabled"
# Per-integration content filter, off by default -- see service.py's
# is_adult_entry(). Deliberately separate from the enable/disable toggle
# above: this only hides Hentai/Rx-rated entries from an otherwise-enabled
# listing, it doesn't gate the whole integration.
ADULT_SETTING_KEY = "anime_seasons_show_adult"

bp = Blueprint(
    "anime_seasons",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/anime_seasons/static",
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


def _show_adult() -> bool:
    return get_setting(ADULT_SETTING_KEY, "0") == "1"


def _slot_label(slot: dict) -> str:
    return f"{_gt(SEASON_LABELS[slot['season']])} {slot['year']}"


def _slot_public(slot: dict) -> dict:
    return {
        "slug":       slot["slug"],
        "year":       slot["year"],
        "season":     slot["season"],
        "is_current": slot["is_current"],
        "label":      _slot_label(slot),
    }


@bp.route("/anime-seasons")
def anime_seasons_page():
    """Serve GET /anime-seasons: the 4-tile season picker, or redirect home
    if the integration is disabled in Settings → Integrations → Third
    Party."""
    if not _enabled():
        return redirect(url_for("index"))
    slots = [_slot_public(s) for s in get_season_slots()]
    return render_template("anime_seasons.html", slots=slots)


@bp.route("/anime-seasons/<slug>")
def anime_seasons_view_page(slug):
    """Serve GET /anime-seasons/<slug>: the grid overview for one season
    (slug is e.g. "now" or "2026-spring"). Redirects to the tile picker for
    an unknown/stale slug, and home if the integration is disabled."""
    if not _enabled():
        return redirect(url_for("index"))
    slot = slot_for_slug(slug)
    if not slot:
        return redirect(url_for(".anime_seasons_page"))
    return render_template("anime_seasons_view.html", slot=_slot_public(slot))


@bp.route("/api/anime-seasons/list")
def api_anime_seasons_list():
    """Return the 4 current season descriptors (slug/year/season/label).
    Route: GET /api/anime-seasons/list. Called from
    static/anime_seasons_view.js's sibling tile page (server-rendered
    today, kept for API parity/future use)."""
    if not _enabled():
        return jsonify({"error": "disabled", "seasons": []}), 403
    return jsonify({"seasons": [_slot_public(s) for s in get_season_slots()]})


@bp.route("/api/anime-seasons/<slug>")
def api_anime_seasons_season(slug):
    """Return the (cached) anime list for one season. Route: GET
    /api/anime-seasons/<slug>. Called from
    static/anime_seasons_view.js's `loadSeasonAnime()`."""
    if not _enabled():
        return jsonify({"error": "disabled", "items": []}), 403
    slot = slot_for_slug(slug)
    if not slot:
        return jsonify({"error": "unknown_slug", "items": []}), 404
    items = get_season(slot["slug"], slot["year"], slot["season"])
    if items is None:
        return jsonify({"error": "fetch_failed", "items": []}), 502
    if not _show_adult():
        items = [item for item in items if not is_adult_entry(item)]
    return jsonify({"items": items, "season": _slot_public(slot)})
