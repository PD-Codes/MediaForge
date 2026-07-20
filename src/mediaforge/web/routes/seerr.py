"""Seerr request routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

detail.integrations (connection errors, no credentials) is wired at the
Jellyseerr/Overseerr fetch below -- see registry.py. flag.integrations.seerr
(usage counter) is intentionally NOT wired -- out of scope for now.
"""

from ...config import LANG_LABELS
from ..db import get_hidden_seerr_request_ids
from ..db import get_hidden_seerr_requests
from ..db import get_setting
from ..db import hide_seerr_request
from ..db import unhide_seerr_request
from ..runtime_state import WORKING_PROVIDERS
from flask import jsonify
from flask import render_template
from flask import request
import json
from .image_proxy import _poster_proxy
from ...logger import get_logger
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events


logger = get_logger(__name__)


def _report_seerr_error(exc):
    """Submit a detail.integrations telemetry event for a failed Seerr fetch
    (see registry.py's "detail.integrations"). Only the exception class name
    is sent -- never the raw message, which echoes the configured Seerr URL
    (see the "Seerr nicht erreichbar: {e}" string below). Wrapped in its own
    try/except so a telemetry bug can never affect the requests page itself.
    """
    try:
        event = telemetry_events.build_feature_detail_event(
            "detail.integrations", action="connect", status="error",
            metadata={"integration": "seerr", "error_type": type(exc).__name__},
        )
        if event:
            telemetry_client.submit(event)
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.integrations event", exc_info=True)


def register_seerr_routes(app):
    """Register all Jellyseerr/Overseerr request-browsing and moderation routes
    (list/approve/decline/hide) on the given Flask app."""
    @app.route("/api/seerr/requests")
    def api_seerr_requests():
        """Return a paginated, deduplicated list of pending/approved Seerr requests.

        Route: GET /api/seerr/requests. Called from static/seerr.js's
        `seerrFetchPage()`.

        Fetches pending+approved requests for both TV and movies from
        Jellyseerr/Overseerr in parallel, merges and de-dupes them by request
        id, drops anything already hidden by the current user or already fully
        available, then enriches the current page with TMDB detail (title,
        poster, season count, etc.) fetched in parallel per item.
        """
        from flask import session as flask_session
        import urllib.request as _urllib
        import urllib.parse as _urlparse
        from concurrent.futures import ThreadPoolExecutor, as_completed

        seerr_url = (get_setting("seerr_url") or "").rstrip("/")
        seerr_key = get_setting("seerr_api_key") or ""
        if not seerr_url or not seerr_key:
            return jsonify({"error": "Seerr nicht konfiguriert"}), 400

        def seerr_get(path, params=None):
            url = seerr_url + path
            if params:
                url += "?" + _urlparse.urlencode(params)
            req = _urllib.Request(url, headers={"X-Api-Key": seerr_key})
            with _urllib.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        take = min(int(request.args.get("take", 20)), 50)
        skip = max(int(request.args.get("skip", 0)), 0)

        # Fetch pending + approved for both TV and movies in parallel (4 requests)
        def fetch_filter(f, media_type):
            return seerr_get("/api/v1/request", {
                "filter": f, "mediaType": media_type,
                "take": 500, "skip": 0,
                "sort": "added", "sortDirection": "desc",
            })

        def getReleaseDate(tmdb_id, mediaType):
            if mediaType == "tv":
                return seerr_get("/api/v1/tv/"+str(tmdb_id))["firstAirDate"] or ""
            if mediaType == "movie":
                return seerr_get("/api/v1/movie/"+str(tmdb_id))["releaseDate"] or ""
            return ""

        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                fut_tv_pending    = ex.submit(fetch_filter, "pending",  "tv")
                fut_tv_approved   = ex.submit(fetch_filter, "approved", "tv")
                fut_mv_pending    = ex.submit(fetch_filter, "pending",  "movie")
                fut_mv_approved   = ex.submit(fetch_filter, "approved", "movie")
                tv_pending    = fut_tv_pending.result().get("results", [])
                tv_approved   = fut_tv_approved.result().get("results", [])
                mv_pending    = fut_mv_pending.result().get("results", [])
                mv_approved   = fut_mv_approved.result().get("results", [])
        except Exception as e:
            _report_seerr_error(e)
            return jsonify({"error": f"Seerr nicht erreichbar: {e}"}), 502

        # Tag each item with its media type so we know which detail endpoint to call
        for r in tv_pending + tv_approved:
            r.setdefault("_media_type", "tv")
        for r in mv_pending + mv_approved:
            r.setdefault("_media_type", "movie")

        # Merge + de-duplicate by request id, sort newest first
        seen = set()
        merged = []
        for r in tv_pending + tv_approved + mv_pending + mv_approved:
            if r["id"] not in seen:
                seen.add(r["id"])
                merged.append(r)
        # Keep only truly pending (1) or approved-but-not-yet-available (2)
        # Also exclude items where the media itself is already fully available (media.status == 5)
        uid = flask_session.get("user_id", 0)
        hidden_ids = get_hidden_seerr_request_ids(uid)
        merged = [
            r for r in merged
            if r.get("status") in (1, 2)
            and r.get("media", {}).get("status") != 5
            and r["id"] not in hidden_ids
        ]
        merged.sort(key=lambda r: r.get("createdAt", ""), reverse=True)

        total_all = len(merged)
        items = merged[skip: skip + take]

        # Fetch detail pages in parallel (TV → /tv/{id}, Movie → /movie/{id})
        def fetch_detail(req):
            media = req.get("media") or {}
            tmdb_id = media.get("tmdbId")
            media_type = req.get("_media_type", "tv")
            if not tmdb_id:
                return tmdb_id, media_type, {}
            try:
                endpoint = f"/api/v1/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
                return tmdb_id, media_type, seerr_get(endpoint)
            except Exception:
                return tmdb_id, media_type, {}

        detail_map = {}  # (tmdb_id, media_type) → details
        with ThreadPoolExecutor(max_workers=6) as ex:
            for tmdb_id, media_type, details in ex.map(fetch_detail, items):
                if tmdb_id:
                    detail_map[(tmdb_id, media_type)] = details

        result = []
        for req in items:
            media = req.get("media") or {}
            tmdb_id = media.get("tmdbId")
            media_type = req.get("_media_type", "tv")
            det = detail_map.get((tmdb_id, media_type), {})
            is_movie = media_type == "movie"
            # TV uses "name"/"firstAirDate"/"numberOfSeasons"; Movie uses "title"/"releaseDate"
            title = (det.get("title") if is_movie else det.get("name")) or det.get("originalTitle") or det.get("originalName") or f"TMDB #{tmdb_id}"
            year = ((det.get("releaseDate") if is_movie else det.get("firstAirDate")) or "")[:4]

            result.append({
                "id": req["id"],
                "status": req.get("status"),
                "downloadStatus": req["media"]["status"],
                "createdAt": req.get("createdAt"),
                "requestedBy": (req.get("requestedBy") or {}).get("displayName", ""),
                "tmdbId": tmdb_id,
                "mediaType": media_type,
                "isMovie": is_movie,
                "title": title,
                "posterPath": det.get("posterPath") or "",
                "posterUrl": _poster_proxy("https://image.tmdb.org/t/p/w342" + det["posterPath"]) if det.get("posterPath") else "",
                "backdropUrl": _poster_proxy("https://image.tmdb.org/t/p/w780" + det["backdropPath"]) if det.get("backdropPath") else "",
                "overview": det.get("overview") or "",
                "firstAirDate": year,
                "numberOfSeasons": det.get("numberOfSeasons") or 0,
                "requestedSeasons": sorted(
                    s["seasonNumber"] for s in (req.get("seasons") or [])
                    if isinstance(s, dict) and s.get("seasonNumber") is not None
                ),
                # TMDB ID will be ID for /api/v1/tv/<id>
                "releaseDate":getReleaseDate(tmdb_id, media_type),
            })

        return jsonify({"requests": result, "total": total_all, "skip": skip, "take": take})
    @app.route("/seerr")
    def seerr_page():
        """Render the Seerr requests page. Route: GET /seerr."""
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        return render_template(
            "seerr.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )
    @app.route("/api/seerr/requests/<int:req_id>/approve", methods=["POST"])
    def api_seerr_approve(req_id):
        """Approve a pending Seerr request so it starts being fulfilled upstream.

        Route: POST /api/seerr/requests/<req_id>/approve. Called from
        static/seerr.js's `seerrStartDownload()`, right before a download is
        queued for a request that was still pending.
        """
        import requests as _req
        seerr_url = (get_setting("seerr_url") or "").rstrip("/")
        seerr_key = get_setting("seerr_api_key") or ""
        if not seerr_url or not seerr_key:
            return jsonify({"error": "Seerr nicht konfiguriert"}), 400
        try:
            # Seerr (Jellyseerr/Overseerr) requires a CSRF token even for API-key requests.
            # Use a session so cookies (including the CSRF cookie) persist across requests.
            session = _req.Session()
            session.headers.update({"X-Api-Key": seerr_key})

            # Step 1: GET to receive cookies/CSRF token from Seerr.
            # Try the Next.js CSRF endpoint first, then fall back to a regular API endpoint.
            csrf_token = ""
            for csrf_path in ["/api/auth/csrf", "/api/v1/settings/public"]:
                try:
                    pre = session.get(f"{seerr_url}{csrf_path}", timeout=10)
                    # Next.js csrf endpoint returns {"csrfToken": "..."}
                    if csrf_path == "/api/auth/csrf" and pre.ok:
                        csrf_token = pre.json().get("csrfToken", "")
                    if not csrf_token:
                        # Double-submit cookie pattern: XSRF-TOKEN or CSRF-TOKEN cookie
                        csrf_token = (
                            session.cookies.get("XSRF-TOKEN")
                            or session.cookies.get("CSRF-TOKEN")
                            or session.cookies.get("csrf_token")
                            or ""
                        )
                    if csrf_token:
                        break
                except Exception:
                    pass

            logger.debug("Seerr CSRF token obtained: %s", "yes" if csrf_token else "no")

            if csrf_token:
                session.headers.update({
                    "X-CSRF-Token": csrf_token,
                    "X-XSRF-TOKEN": csrf_token,
                })

            # Step 2: POST to the approve endpoint.
            resp = session.post(
                f"{seerr_url}/api/v1/request/{req_id}/approve",
                json={},
                timeout=10,
            )
            logger.info("Seerr approve req %s → %s", req_id, resp.status_code)
            if not resp.ok:
                body = resp.text[:300]
                logger.warning("Seerr approve req %s failed: %s %s", req_id, resp.status_code, body)
                return jsonify({"error": f"Seerr {resp.status_code}: {body}"}), 502
            return jsonify({"ok": True})
        except Exception as e:
            logger.warning("Seerr approve req %s error: %s", req_id, e)
            return jsonify({"error": str(e)}), 502
    @app.route("/api/seerr/requests/<int:req_id>/decline", methods=["POST"])
    def api_seerr_decline(req_id):
        """Decline a pending Seerr request.

        Route: POST /api/seerr/requests/<req_id>/decline. Called from
        static/seerr.js's `seerrDeclineRequest()`.
        """
        import requests as _req
        seerr_url = (get_setting("seerr_url") or "").rstrip("/")
        seerr_key = get_setting("seerr_api_key") or ""
        if not seerr_url or not seerr_key:
            return jsonify({"error": "Seerr nicht konfiguriert"}), 400
        try:
            session = _req.Session()
            session.headers.update({"X-Api-Key": seerr_key})

            # Fetch CSRF token (same pattern as approve)
            csrf_token = ""
            for csrf_path in ["/api/auth/csrf", "/api/v1/settings/public"]:
                try:
                    pre = session.get(f"{seerr_url}{csrf_path}", timeout=10)
                    if csrf_path == "/api/auth/csrf" and pre.ok:
                        csrf_token = pre.json().get("csrfToken", "")
                    if not csrf_token:
                        csrf_token = (
                            session.cookies.get("XSRF-TOKEN")
                            or session.cookies.get("CSRF-TOKEN")
                            or session.cookies.get("csrf_token")
                            or ""
                        )
                    if csrf_token:
                        break
                except Exception:
                    pass

            if csrf_token:
                session.headers.update({
                    "X-CSRF-Token": csrf_token,
                    "X-XSRF-TOKEN": csrf_token,
                })

            resp = session.post(
                f"{seerr_url}/api/v1/request/{req_id}/decline",
                json={},
                timeout=10,
            )
            logger.info("Seerr decline req %s → %s", req_id, resp.status_code)
            if not resp.ok:
                body = resp.text[:300]
                logger.warning("Seerr decline req %s failed: %s %s", req_id, resp.status_code, body)
                return jsonify({"error": f"Seerr {resp.status_code}: {body}"}), 502
            return jsonify({"ok": True})
        except Exception as e:
            logger.warning("Seerr decline req %s error: %s", req_id, e)
            return jsonify({"error": str(e)}), 502
    @app.route("/api/seerr/requests/<int:req_id>/hide", methods=["POST"])
    def api_seerr_hide(req_id):
        """Hide a Seerr request from the current user's request list (per-user).

        Route: POST /api/seerr/requests/<req_id>/hide. Called from
        static/seerr.js's `seerrHideCard()`.
        """
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        data = request.get_json(silent=True) or {}
        title = str(data.get("title", "")).strip()
        poster_url = str(data.get("posterUrl", "")).strip()
        hide_seerr_request(uid, req_id, title, poster_url)
        return jsonify({"ok": True})
    @app.route("/api/seerr/requests/<int:req_id>/unhide", methods=["POST"])
    def api_seerr_unhide(req_id):
        """Un-hide a previously hidden Seerr request for the current user.

        Route: POST /api/seerr/requests/<req_id>/unhide. Called from
        static/seerr.js's `seerrUnhide()`.
        """
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        unhide_seerr_request(uid, req_id)
        return jsonify({"ok": True})
    @app.route("/api/seerr/hidden")
    def api_seerr_hidden():
        """List the Seerr requests the current user has hidden.

        Route: GET /api/seerr/hidden. Called from static/seerr.js's
        `seerrOpenHiddenModal()`.
        """
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        items = get_hidden_seerr_requests(uid)
        return jsonify({"hidden": items})
