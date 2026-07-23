"""Favourites routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import add_favourite
from ..db import get_favourites
from ..db import is_favourite
from ..db import remove_favourite
from ..db import remove_favourites_bulk
from flask import jsonify
from flask import render_template
from flask import request
from .. import runtime_state
from ..auth import get_current_user
from .image_proxy import _poster_proxy



# Allowed media_type values the client may send. Anything else is coerced to
# None so a caller can never write arbitrary strings into the column.
_ALLOWED_MEDIA_TYPES = {"movie", "series"}


def _derive_provider(series_url: str) -> str | None:
    """Return the friendly source-site name (AniWorld, SerienStream, Megakino, …)
    for a series URL, or None when no provider recognizes it. Best-effort: any
    error (unsupported URL, import issue) degrades to None so favourites still
    work for URLs the provider registry does not know."""
    try:
        from ...providers import resolve_provider

        return resolve_provider(series_url).name
    except Exception:
        return None


def register_favourites_routes(app):
    """Register the favourites page and its add/remove/list/check API endpoints."""
    @app.route("/favourites")
    def favourites_page():
        """Render the favourites page shell (data is loaded client-side).

        GET /favourites.
        """
        return render_template("favourites.html")
    @app.route("/api/favourites")
    def api_get_favourites():
        """Return the current user's favourites (or all, when auth is disabled).

        GET /api/favourites. Called from favourites.js's loadFavourites().
        """
        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            username = user.get("username") if user else None
        favs = get_favourites(added_by=username)
        for f in favs:
            # Proxy poster URLs so the client never hits source sites directly
            if f.get("poster_url") and not f["poster_url"].startswith("/api/img"):
                f["poster_url"] = _poster_proxy(f["poster_url"])
            # Backfill the source provider for legacy rows saved before the
            # metadata columns existed, so grouping/badges work retroactively
            # without a one-off data migration.
            if not f.get("provider") and f.get("series_url"):
                f["provider"] = _derive_provider(f["series_url"])
        return jsonify({"favourites": favs})
    @app.route("/api/favourites", methods=["POST"])
    def api_add_favourite():
        """Add a series/movie to the current user's favourites.

        POST /api/favourites. Called from app.js's toggleFavourite() and
        favourites.js when a poster is favourited.
        """
        data = request.get_json(silent=True) or {}
        series_url = (data.get("series_url") or "").strip()
        title = (data.get("title") or "").strip()
        raw_poster = (data.get("poster_url") or "").strip()
        # Unwrap proxy URLs so the DB always stores the original source URL
        if raw_poster.startswith("/api/img?url="):
            from urllib.parse import unquote as _unquote_fav
            raw_poster = _unquote_fav(raw_poster[len("/api/img?url="):])
        poster_url = raw_poster or None
        if not series_url or not title:
            return jsonify({"error": "series_url and title required"}), 400
        # Metadata for grouping/badges on the favourites page. media_type is
        # whitelisted; provider is derived server-side from the URL (never
        # trusted from the client); language is a short free-text label.
        media_type = (data.get("media_type") or "").strip().lower() or None
        if media_type not in _ALLOWED_MEDIA_TYPES:
            media_type = None
        language = (data.get("language") or "").strip()[:64] or None
        provider = _derive_provider(series_url)
        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            username = user.get("username") if user else None
        add_favourite(
            series_url,
            title,
            poster_url,
            username,
            media_type=media_type,
            provider=provider,
            language=language,
        )
        return jsonify({"ok": True})
    @app.route("/api/favourites", methods=["DELETE"])
    def api_remove_favourite():
        """Remove one or multiple series/movies from the current user's favourites.

        DELETE /api/favourites. Called from app.js's toggleFavourite() and
        favourites.js when favourites are removed.
        """
        data = request.get_json(silent=True) or {}
        series_url = (data.get("series_url") or "").strip()
        urls = [u.strip() for u in (data.get("urls") or []) if u and isinstance(u, str)]
        if not series_url and not urls:
            return jsonify({"error": "series_url or urls required"}), 400
        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            username = user.get("username") if user else None

        if urls:
            remove_favourites_bulk(urls, username)
        else:
            remove_favourite(series_url, username)
        return jsonify({"ok": True})
    @app.route("/api/favourites/check")
    def api_check_favourite():
        """Return whether a given series/movie URL is already favourited.

        GET /api/favourites/check. Called from app.js's
        _updateFavouriteBtn() to set the initial favourite-button state.
        """
        series_url = request.args.get("series_url", "").strip()
        if not series_url:
            return jsonify({"is_favourite": False})
        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            username = user.get("username") if user else None
        return jsonify({"is_favourite": is_favourite(series_url, username)})
