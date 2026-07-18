"""Integration settings routes (Crunchyroll, Fernsehserien, MediaPlayer, MediaScan).

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.integrations.crunchyroll/fernsehserien/seerr/
# mediascan (usage counters) and detail.integrations (per-integration
# connection errors, no credentials) -- see telemetry/registry.py.
# Registry-only for now.
"""

from ..db import get_mediascan_count
from ..db import get_mediascan_ids
from ..db import get_mediascan_last_updated
from ..db import get_setting
from ..db import set_setting
from ..mediascan import _mediascan_status
from ..mediascan import _mediascan_status_lock
from ..mediascan import _run_mediascan
from ..mediascan import _trigger_mediaplayer_refresh
from ..queue_worker import _normalize_media_url
from ..queue_worker import _validate_server_url
from flask import jsonify
from flask import render_template
from flask import request
import json
import threading
from ...logger import get_logger


logger = get_logger(__name__)


_PLEX_CLIENT_ID = "mediaforge-downloader"
_PLEX_PRODUCT   = "MediaForge"


def register_integrations_routes(app):
    """Register all integration-settings routes (Crunchyroll, Fernsehserien,
    MediaPlayer/Jellyfin/Plex, MediaScan) on the given Flask app."""
    @app.route("/integrations")
    def integrations_page():
        """Render the Integrations settings page. Route: GET /integrations."""
        return render_template("integrations.html")
    @app.route("/api/settings/crunchyroll", methods=["GET"])
    def api_settings_crunchyroll_get():
        """Return the stored Crunchyroll settings. Route: GET /api/settings/crunchyroll."""
        return jsonify({
            "enabled":            get_setting("crunchyroll_enabled",            "0"),
            "email":              get_setting("crunchyroll_email",              ""),
            "has_password":       bool(get_setting("crunchyroll_password",      "")),
            "locale":             get_setting("crunchyroll_locale",             "de-DE"),
            "anon":               get_setting("crunchyroll_anon",               "0"),
            "profile_id":         get_setting("crunchyroll_profile_id",         ""),
            "show_providers":     get_setting("crunchyroll_show_providers",     "1"),
            "calendar_simulcast": get_setting("crunchyroll_calendar_simulcast", "0"),
            "calendar_watchlist": get_setting("crunchyroll_calendar_watchlist", "0"),

            "calendar_lists":     get_setting("crunchyroll_calendar_lists",     "0"),
            "calendar_release":   get_setting("crunchyroll_calendar_release",   "0"),
        })
    @app.route("/api/settings/crunchyroll", methods=["PUT"])
    def api_settings_crunchyroll_put():
        """Persist Crunchyroll settings from the settings UI.

        Route: PUT /api/settings/crunchyroll. Called from static/integrations.js's
        `saveCrunchyrollSettings()` (credentials/locale) and `saveCrunchyrollOptions()`
        (toggles only).
        """
        data = request.get_json(silent=True) or {}

        # Simple values (toggles, email, locale). The password is handled
        # specially below so we never overwrite a stored secret with a blank.
        for key in ["enabled", "email", "locale", "anon", "profile_id", "show_providers",
                    "calendar_simulcast", "calendar_watchlist", "calendar_lists", "calendar_release"]:
            if key in data:
                set_setting("crunchyroll_" + key, str(data[key]))

        # Password: only update when a non-empty value is sent. An explicit
        # ``clear_password: true`` wipes it (logout/forget).
        if data.get("clear_password"):
            set_setting("crunchyroll_password", "")
        elif "password" in data and str(data["password"]).strip():
            set_setting("crunchyroll_password", str(data["password"]).strip())

        # Credentials/locale may have changed — force a fresh login next call.
        try:
            from .. import crunchyroll_service
            crunchyroll_service.invalidate_client()
        except Exception:
            logger.debug("[Crunchyroll] could not invalidate client", exc_info=True)

        # Drop the cached CR calendar targets so toggling simulcast/watchlist/lists
        # takes effect on the next /api/calendar call instead of after the TTL.
        from .calendar_routes import reset_cr_targets
        reset_cr_targets()

        return jsonify({"ok": True})
    @app.route("/api/settings/crunchyroll/test", methods=["POST"])
    def api_settings_crunchyroll_test():
        """Validate Crunchyroll credentials from the settings UI.

        Route: POST /api/settings/crunchyroll/test. Called from
        static/integrations.js's `testCrunchyroll()`.

        Uses the values posted in the body when present, otherwise falls back to
        the stored settings (so the user can re-test without retyping a saved
        password).
        """
        data = request.get_json(silent=True) or {}
        anon = str(data.get("anon", get_setting("crunchyroll_anon", "0"))) == "1"
        email = str(data.get("email", get_setting("crunchyroll_email", "")) or "").strip()
        locale = str(data.get("locale", get_setting("crunchyroll_locale", "de-DE")) or "de-DE").strip()
        password = str(data.get("password", "") or "").strip()
        if not password:
            password = get_setting("crunchyroll_password", "") or ""
        profile_id = str(data.get("profile_id", get_setting("crunchyroll_profile_id", "")) or "").strip()
        try:
            from .. import crunchyroll_service
            result = crunchyroll_service.test_connection(email, password, locale, anon, profile_id)
        except Exception as exc:
            logger.debug("[Crunchyroll] test endpoint error: %s", exc)
            result = {"ok": False, "error": "unknown", "detail": str(exc)}
        return jsonify(result)
    @app.route("/api/settings/crunchyroll/profiles", methods=["GET"])
    def api_settings_crunchyroll_profiles():
        """Return the account's Crunchyroll profiles for the settings selector.

        Route: GET /api/settings/crunchyroll/profiles. Called from
        static/integrations.js's `_loadCrProfiles()`.
        """
        try:
            from .. import crunchyroll_service
            return jsonify({"profiles": crunchyroll_service.list_account_profiles()})
        except Exception as exc:
            logger.debug("[Crunchyroll] profiles endpoint error: %s", exc)
            return jsonify({"profiles": []})
    @app.route("/api/crunchyroll/availability", methods=["GET"])
    def api_crunchyroll_availability():
        """Return whether a title is available on Crunchyroll (cached).

        Route: GET /api/crunchyroll/availability. Called from static/app.js's
        `_crProviderPill()` to power the extra "Crunchyroll" provider pill —
        useful for fresh simulcasts that TMDB's provider data hasn't picked up yet.
        """
        title = (request.args.get("title") or "").strip()
        if not title:
            return jsonify({"available": False, "reason": "no_title"})
        try:
            from .. import crunchyroll_service
            if not crunchyroll_service.is_enabled() or get_setting("crunchyroll_show_providers", "1") != "1":
                return jsonify({"available": False, "reason": "disabled"})
            return jsonify({"available": bool(crunchyroll_service.is_available(title))})
        except Exception as exc:
            logger.debug("[Crunchyroll] availability endpoint error: %s", exc)
            return jsonify({"available": False, "reason": "error"})
    @app.route("/api/settings/fernsehserien", methods=["GET"])
    def api_settings_fernsehserien_get():
        """Return the stored Fernsehserien.de settings. Route: GET /api/settings/fernsehserien."""
        return jsonify({
            "enabled":        get_setting("fernsehserien_enabled",        "0"),
            "show_providers": get_setting("fernsehserien_show_providers", "1"),
            "delay":          get_setting("fernsehserien_delay",          "1.5"),
        })
    @app.route("/api/settings/fernsehserien", methods=["PUT"])
    def api_settings_fernsehserien_put():
        """Persist Fernsehserien.de settings from the settings UI.

        Route: PUT /api/settings/fernsehserien. Called from
        static/integrations.js's `saveFernsehserienOptions()`.
        """
        data = request.get_json(silent=True) or {}
        for key in ["enabled", "show_providers", "delay"]:
            if key in data:
                set_setting("fernsehserien_" + key, str(data[key]))

        try:
            from .. import fernsehserien_service
            fernsehserien_service.invalidate_cache()
        except Exception:
            logger.debug("[Fernsehserien] could not invalidate cache", exc_info=True)

        return jsonify({"ok": True})
    @app.route("/api/settings/fernsehserien/test", methods=["POST"])
    def api_settings_fernsehserien_test():
        """Verify the fernsehserien.de scraper still works (settings UI).

        Route: POST /api/settings/fernsehserien/test. Called from
        static/integrations.js's `testFernsehserien()`.

        There are no credentials to validate — this just fetches a known,
        stable page to confirm the site isn't blocking us and the page layout
        still parses.
        """
        try:
            from .. import fernsehserien_service
            result = fernsehserien_service.test_connection()
        except Exception as exc:
            logger.debug("[Fernsehserien] test endpoint error: %s", exc)
            result = {"ok": False, "error": "unknown", "detail": str(exc)}
        return jsonify(result)
    @app.route("/api/fernsehserien/availability", methods=["GET"])
    def api_fernsehserien_availability():
        """Return the streaming provider fernsehserien.de names for a title (cached).

        Route: GET /api/fernsehserien/availability. Called from static/app.js's
        `_fsProviderPill()` to power the "Fernsehserien" provider pill in the
        detail modal — the title is matched to a page via a best-effort slug
        guess (there is no search endpoint on the site), so a miss just means
        no pill, never wrong information.
        """
        title = (request.args.get("title") or "").strip()
        if not title:
            return jsonify({"available": False, "reason": "no_title"})
        try:
            from .. import fernsehserien_service
            if (not fernsehserien_service.is_enabled()
                    or get_setting("fernsehserien_show_providers", "1") != "1"):
                return jsonify({"available": False, "reason": "disabled"})
            info = fernsehserien_service.get_provider_info(title)
            return jsonify(info)
        except Exception as exc:
            logger.debug("[Fernsehserien] availability endpoint error: %s", exc)
            return jsonify({"available": False, "reason": "error"})
    @app.route("/api/settings/mediaplayer", methods=["GET"])
    def api_settings_mediaplayer_get():
        """Return the stored MediaPlayer (Jellyfin/Plex) settings.

        Route: GET /api/settings/mediaplayer. Called from static/integrations.js's
        `loadMediaplayerSettings()`.
        """
        svc = get_setting("mediaplayer_type", "")
        token = get_setting("mediaplayer_apikey", "")
        return jsonify({
            "type":         svc,
            "url":          get_setting("mediaplayer_url",          ""),
            "plex_url":     get_setting("mediaplayer_plex_url",     ""),
            "apikey":       token,
            "has_token":    bool(token),
            "plex_section": get_setting("mediaplayer_plex_section", ""),
        })
    @app.route("/api/settings/mediaplayer", methods=["PUT"])
    def api_settings_mediaplayer_put():
        """Persist MediaPlayer (Jellyfin/Plex) settings from the settings UI.

        Route: PUT /api/settings/mediaplayer. Called from static/integrations.js's
        `saveMediaplayerSettings()`.
        """
        data = request.get_json(silent=True) or {}
        for url_key in ("url", "plex_url"):
            if url_key in data:
                try:
                    _validate_server_url(_normalize_media_url(str(data[url_key]).strip()))
                except ValueError as e:
                    return jsonify({"ok": False, "error": str(e)}), 400
        for key in ["type", "url", "plex_url", "apikey", "plex_section"]:
            if key in data:
                set_setting("mediaplayer_" + key, str(data[key]).strip())
        return jsonify({"ok": True})
    @app.route("/api/settings/mediaplayer/test", methods=["POST"])
    def api_settings_mediaplayer_test():
        """Quick connectivity test: try to reach the configured mediaplayer.

        Route: POST /api/settings/mediaplayer/test. Called from
        static/integrations.js's `testMediaplayerConnection()`.
        """
        import urllib.request as _req
        svc = get_setting("mediaplayer_type", "")
        key = get_setting("mediaplayer_apikey", "") or ""
        if not svc or not key:
            return jsonify({"ok": False, "error": "Konfiguration unvollständig"})
        try:
            if svc == "jellyfin":
                url = _normalize_media_url(get_setting("mediaplayer_url", "") or "")
                if not url:
                    return jsonify({"ok": False, "error": "Server-URL fehlt"})
                r = _req.Request(f"{url}/System/Info/Public", headers={"X-Emby-Token": key})
                with _req.urlopen(r, timeout=8) as resp:
                    info = json.loads(resp.read())
                return jsonify({"ok": True, "name": info.get("ServerName", "Jellyfin")})
            elif svc == "plex":
                url = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "")
                if not url:
                    return jsonify({"ok": False, "error": "Server-URL fehlt"})
                r = _req.Request(
                    f"{url}/?X-Plex-Token={key}",
                    headers={"Accept": "application/json"},
                )
                with _req.urlopen(r, timeout=8) as resp:
                    info = json.loads(resp.read())
                friendly = info.get("MediaContainer", {}).get("friendlyName", "Plex")
                return jsonify({"ok": True, "name": friendly})
            else:
                return jsonify({"ok": False, "error": "Unbekannter Typ"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    @app.route("/api/settings/mediaplayer/scan-status", methods=["GET"])
    def api_mediaplayer_scan_status():
        """Poll whether the configured media server is currently scanning its library.

        Route: GET /api/settings/mediaplayer/scan-status. Called from
        static/integrations.js's `triggerMediaScan()` poll loop.
        """
        import urllib.request as _req
        svc = get_setting("mediaplayer_type", "")
        key = get_setting("mediaplayer_apikey", "") or ""
        if not svc or not key:
            return jsonify({"scanning": False, "error": "Kein Mediaplayer konfiguriert"})
        try:
            if svc == "jellyfin":
                url = _normalize_media_url(get_setting("mediaplayer_url", "") or "")
                if not url:
                    return jsonify({"scanning": False, "error": "Keine URL"})
                r = _req.Request(
                    f"{url}/ScheduledTasks",
                    headers={"X-Emby-Token": key, "Accept": "application/json"},
                )
                with _req.urlopen(r, timeout=8) as resp:
                    tasks = json.loads(resp.read())
                scan_keywords = ("scan", "refresh", "bibliothek", "library", "medien")
                scanning = any(
                    t.get("State") == "Running"
                    and any(kw in t.get("Name", "").lower() for kw in scan_keywords)
                    for t in tasks
                )
                running = [t.get("Name") for t in tasks if t.get("State") == "Running"]
                logger.debug("Jellyfin scan-status poll: scanning=%s running_tasks=%s", scanning, running)
                return jsonify({"scanning": scanning})

            elif svc == "plex":
                url = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "")
                if not url:
                    return jsonify({"scanning": False, "error": "Keine URL"})
                section = (get_setting("mediaplayer_plex_section", "") or "").strip()
                req_url = (
                    f"{url}/library/sections/{section}?X-Plex-Token={key}"
                    if section else
                    f"{url}/library/sections?X-Plex-Token={key}"
                )
                r = _req.Request(req_url, headers={"Accept": "application/json"})
                with _req.urlopen(r, timeout=8) as resp:
                    data = json.loads(resp.read())
                dirs = data.get("MediaContainer", {}).get("Directory", [])
                if isinstance(dirs, dict):
                    dirs = [dirs]
                scanning = any(d.get("refreshing") in (1, True, "1", "true") for d in dirs)
                logger.debug("Plex scan-status poll: refreshing=%s dirs=%s", scanning, [d.get("title","?") + "=" + str(d.get("refreshing")) for d in dirs])
                return jsonify({"scanning": scanning})

            return jsonify({"scanning": False, "error": "Unbekannter Typ"})
        except Exception as e:
            return jsonify({"scanning": False, "error": str(e)})
    @app.route("/api/settings/mediaplayer/scan", methods=["POST"])
    def api_mediaplayer_scan():
        """Manually trigger a library scan/refresh on the configured media server.

        Route: POST /api/settings/mediaplayer/scan. Called from
        static/integrations.js's `triggerMediaScan()`.
        """
        svc = get_setting("mediaplayer_type", "")
        url = _normalize_media_url(get_setting("mediaplayer_url", "") or "")
        key = get_setting("mediaplayer_apikey", "") or ""
        if not svc or not key:
            return jsonify({"ok": False, "error": "Kein Mediaplayer konfiguriert"}), 400
        if svc == "plex":
            url = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "") or url
        if not url:
            return jsonify({"ok": False, "error": "Keine Server-URL konfiguriert"}), 400
        try:
            _trigger_mediaplayer_refresh()
            label = "Jellyfin" if svc == "jellyfin" else "Plex"
            return jsonify({"ok": True, "message": f"{label} Mediascan wurde ausgelöst"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    @app.route("/api/settings/mediaplayer/plex-pin", methods=["POST"])
    def api_mediaplayer_plex_pin_create():
        """Create a Plex OAuth pin and return {id, code, auth_url}.

        Route: POST /api/settings/mediaplayer/plex-pin. This endpoint is shared
        by both the MediaPlayer and MediaScan settings tabs; called from
        static/integrations.js's `startPlexOAuth()` and `startMsPlexOAuth()`.
        """
        import urllib.request as _req
        try:
            req = _req.Request(
                "https://plex.tv/api/v2/pins?strong=true",
                data=b"",
                method="POST",
                headers={
                    "X-Plex-Client-Identifier": _PLEX_CLIENT_ID,
                    "X-Plex-Product":           _PLEX_PRODUCT,
                    "Accept":                   "application/json",
                },
            )
            with _req.urlopen(req, timeout=10) as resp:
                pin = json.loads(resp.read())
            pin_id   = pin["id"]
            pin_code = pin["code"]
            auth_url = (
                f"https://app.plex.tv/auth#?"
                f"clientID={_PLEX_CLIENT_ID}"
                f"&code={pin_code}"
                f"&context%5Bdevice%5D%5Bproduct%5D={_PLEX_PRODUCT.replace(' ', '+')}"
            )
            return jsonify({"ok": True, "id": pin_id, "code": pin_code, "auth_url": auth_url})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    @app.route("/api/settings/mediaplayer/plex-pin/<int:pin_id>", methods=["GET"])
    def api_mediaplayer_plex_pin_poll(pin_id):
        """Poll Plex for the auth token of a pin. Returns {token} once authorized.

        Route: GET /api/settings/mediaplayer/plex-pin/<pin_id>. Shared by the
        MediaPlayer and MediaScan tabs; called from static/integrations.js's
        `startPlexOAuth()` and `startMsPlexOAuth()` poll loops.
        """
        import urllib.request as _req
        try:
            req = _req.Request(
                f"https://plex.tv/api/v2/pins/{pin_id}",
                headers={
                    "X-Plex-Client-Identifier": _PLEX_CLIENT_ID,
                    "Accept":                   "application/json",
                },
            )
            with _req.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            token = data.get("authToken") or ""
            if token:
                # Persist the token automatically
                set_setting("mediaplayer_apikey", token)
            return jsonify({"ok": True, "token": token, "authorized": bool(token)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    @app.route("/api/settings/mediaplayer/plex-libraries", methods=["GET"])
    def api_mediaplayer_plex_libraries():
        """Fetch library sections from the configured Plex server.

        Route: GET /api/settings/mediaplayer/plex-libraries. Called from
        static/integrations.js's `loadPlexLibraries()`.
        """
        import urllib.request as _req
        url   = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "")
        token = get_setting("mediaplayer_apikey", "") or ""
        if not url or not token:
            return jsonify({"ok": False, "error": "Plex nicht konfiguriert", "libraries": []})
        try:
            req = _req.Request(
                f"{url}/library/sections?X-Plex-Token={token}",
                headers={"Accept": "application/json"},
            )
            with _req.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            dirs = data.get("MediaContainer", {}).get("Directory", [])
            libs = [{"id": str(d["key"]), "title": d["title"], "type": d.get("type", "")}
                    for d in dirs]
            return jsonify({"ok": True, "libraries": libs})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "libraries": []})
    @app.route("/api/settings/mediascan", methods=["GET"])
    def api_settings_mediascan_get():
        """Return the stored MediaScan settings plus live scan status.

        Route: GET /api/settings/mediascan. Called from static/integrations.js's
        `loadMediascanSettings()`.
        """
        with _mediascan_status_lock:
            status_snap = dict(_mediascan_status)
        last_ts      = get_mediascan_last_updated()
        count        = get_mediascan_count()
        enabled      = get_setting("mediascan_enabled",    "0") == "1"
        source       = get_setting("mediascan_source",     "") or ""
        jf_url_raw   = get_setting("mediascan_jf_url",     "") or ""
        jf_key       = get_setting("mediascan_jf_apikey",  "") or ""
        plex_url_raw = get_setting("mediascan_plex_url",   "") or ""
        plex_section = get_setting("mediascan_plex_section","") or ""
        plex_token   = get_setting("mediaplayer_apikey",   "") or ""  # shared
        has_tmdb     = bool(get_setting("cineinfo_tmdb_api_key", "") or "")
        # Strip scheme for display in frontend inputs
        def _strip(u): return (u or "").replace("https://","").replace("http://","")
        return jsonify({
            "enabled":       enabled,
            "source":        source,
            "jf_url":        _strip(jf_url_raw),
            "jf_apikey":     jf_key,
            "jf_ssl":        jf_url_raw.startswith("https://"),
            "plex_url":      _strip(plex_url_raw),
            "plex_ssl":      plex_url_raw.startswith("https://"),
            "plex_section":  plex_section,
            "has_plex_token": bool(plex_token),
            "plex_token_masked": (plex_token[:4] + "••••" + plex_token[-4:]) if len(plex_token) >= 8 else "",
            "has_tmdb":      has_tmdb,
            "last_updated":  last_ts,
            "count":         count,
            "scan_running":  status_snap["running"],
            "scan_started":  status_snap["started_at"],
            "scan_finished": status_snap["finished_at"],
            "scan_count":    status_snap["count"],
            "scan_total":    status_snap["total"],
            "scan_error":    status_snap["error"],
            "scan_source":   status_snap["source"],
        })
    @app.route("/api/settings/mediascan", methods=["PUT"])
    def api_settings_mediascan_put():
        """Persist MediaScan settings from the settings UI.

        Route: PUT /api/settings/mediascan. Called from static/integrations.js's
        `saveMediascanSettings()`.
        """
        data    = request.get_json(silent=True) or {}
        enabled = "1" if data.get("enabled") else "0"
        source  = str(data.get("source") or "").strip()
        # Validate URLs before persisting
        if "jf_url" in data:
            ssl = data.get("jf_ssl", False)
            raw = (data["jf_url"] or "").strip().lstrip("http://").lstrip("https://")
            jf_url_full = (("https://" if ssl else "http://") + raw) if raw else ""
            try:
                _validate_server_url(jf_url_full)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        else:
            jf_url_full = None
        if "plex_url" in data:
            ssl = data.get("plex_ssl", False)
            raw = (data["plex_url"] or "").strip().lstrip("http://").lstrip("https://")
            plex_url_full = (("https://" if ssl else "http://") + raw) if raw else ""
            try:
                _validate_server_url(plex_url_full)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 400
        else:
            plex_url_full = None
        set_setting("mediascan_enabled", enabled)
        set_setting("mediascan_source",  source)
        if jf_url_full is not None:
            set_setting("mediascan_jf_url", jf_url_full)
        if "jf_apikey" in data:
            set_setting("mediascan_jf_apikey", str(data["jf_apikey"] or "").strip())
        if plex_url_full is not None:
            set_setting("mediascan_plex_url", plex_url_full)
        if "plex_section" in data:
            set_setting("mediascan_plex_section", str(data["plex_section"] or "").strip())
        return jsonify({"ok": True})
    @app.route("/api/settings/mediascan/refresh", methods=["POST"])
    def api_mediascan_refresh():
        """Kick off a MediaScan library refresh in a background thread.

        Route: POST /api/settings/mediascan/refresh. Called from
        static/integrations.js's `triggerMediascanRefresh()`.
        """
        with _mediascan_status_lock:
            if _mediascan_status["running"]:
                return jsonify({"ok": False, "error": "Scan laeuft bereits"})
        source = get_setting("mediascan_source", "") or ""
        if not source or source == "folders":
            return jsonify({"ok": False, "error": "Keine Mediathek-Quelle konfiguriert"})
        t = threading.Thread(target=_run_mediascan, args=(source,), daemon=True)
        t.start()
        return jsonify({"ok": True})
    @app.route("/api/settings/mediascan/status", methods=["GET"])
    def api_mediascan_status():
        """Return the current MediaScan run status plus the cached count.

        Route: GET /api/settings/mediascan/status. Called from
        static/integrations.js's `_startMediascanPoll()`.
        """
        with _mediascan_status_lock:
            snap = dict(_mediascan_status)
        last_ts = get_mediascan_last_updated()
        count   = get_mediascan_count()
        return jsonify({
            "running":      snap["running"],
            "started_at":   snap["started_at"],
            "finished_at":  snap["finished_at"],
            "count":        snap["count"],
            "total":        snap["total"],
            "error":        snap["error"],
            "source":       snap["source"],
            "last_updated":  last_ts,
            "cached_count":  count,
        })
    @app.route("/api/settings/mediascan/plex-libraries", methods=["GET"])
    def api_mediascan_plex_libraries():
        """Fetch library sections from the Plex server configured for MediaScan.

        Route: GET /api/settings/mediascan/plex-libraries. Called from
        static/integrations.js's `loadMsPlexLibraries()`.
        """
        import urllib.request as _req
        url   = _normalize_media_url(get_setting("mediascan_plex_url", "") or "")
        token = get_setting("mediaplayer_apikey", "") or ""  # shared token
        if not url or not token:
            return jsonify({"ok": False, "error": "Plex nicht konfiguriert", "libraries": []})
        try:
            req = _req.Request(
                f"{url}/library/sections?X-Plex-Token={token}",
                headers={"Accept": "application/json"},
            )
            with _req.urlopen(req, timeout=8) as resp:
                import json as _j
                data = _j.loads(resp.read())
            dirs = data.get("MediaContainer", {}).get("Directory", [])
            libs = [{"id": str(d["key"]), "title": d["title"], "type": d.get("type", "")}
                    for d in dirs]
            return jsonify({"ok": True, "libraries": libs})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "libraries": []})
    @app.route("/api/mediascan/library", methods=["GET"])
    def api_mediascan_library():
        """Return the cached MediaScan library IDs/titles used for availability matching.

        Route: GET /api/mediascan/library. Called from static/app.js to build
        the set of already-downloaded/owned TMDB/IMDB IDs shown in the browse UI.
        """
        enabled = get_setting("mediascan_enabled", "0") == "1"
        source  = get_setting("mediascan_source",  "") or ""
        if not enabled or not source or source == "folders":
            return jsonify({"enabled": False, "source": source,
                            "tmdb_ids": [], "imdb_ids": [], "titles": []})
        ids = get_mediascan_ids()
        return jsonify({
            "enabled":  True,
            "source":   source,
            "tmdb_ids": ids["tmdb_ids"],
            "imdb_ids": ids["imdb_ids"],
            "titles":   ids["titles"],
        })
    @app.route("/api/mediascan/debug", methods=["GET"])
    def api_mediascan_debug():
        """Return first 50 cache entries for diagnosing match issues.

        Route: GET /api/mediascan/debug. No frontend caller found (Confirmed via
        grep of static/*.js and templates/) — this looks like a developer-only
        diagnostic endpoint, hit directly by URL.
        """
        from ..db import get_db as _get_db
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT tmdb_id, imdb_id, tvdb_id, title, media_type FROM mediascan_cache"
                " ORDER BY id LIMIT 50"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM mediascan_cache").fetchone()["n"]
        finally:
            conn.close()
        return jsonify({
            "total": total,
            "sample": [{"tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"],
                         "tvdb_id": r["tvdb_id"], "title": r["title"],
                         "media_type": r["media_type"]} for r in rows],
        })

    @app.route("/api/settings/jellyfin-nfo", methods=["GET"])
    def api_settings_jellyfin_nfo_get():
        return jsonify({
            "enabled": get_setting("jellyfin_nfo_enabled", "0"),
            "create_series": get_setting("jellyfin_nfo_create_series", "1"),
            "create_season": get_setting("jellyfin_nfo_create_season", "1"),
            "create_episode": get_setting("jellyfin_nfo_create_episode", "1"),
            "create_movie": get_setting("jellyfin_nfo_create_movie", "1"),
            "meta_plot": get_setting("jellyfin_nfo_meta_plot", "1"),
            "meta_genres": get_setting("jellyfin_nfo_meta_genres", "1"),
            "meta_rating": get_setting("jellyfin_nfo_meta_rating", "1"),
            "meta_fsk": get_setting("jellyfin_nfo_meta_fsk", "1"),
            "meta_actors": get_setting("jellyfin_nfo_meta_actors", "1"),
            "meta_trailer": get_setting("jellyfin_nfo_meta_trailer", "1"),
            "meta_date": get_setting("jellyfin_nfo_meta_date", "1"),
            "meta_studio": get_setting("jellyfin_nfo_meta_studio", "1"),
            "cineinfo_configured": get_setting("cineinfo_tmdb_api_key", "") != ""
        })

    @app.route("/api/settings/jellyfin-nfo", methods=["PUT"])
    def api_settings_jellyfin_nfo_put():
        data = request.get_json(silent=True) or {}
        set_setting("jellyfin_nfo_enabled", "1" if data.get("enabled") else "0")
        set_setting("jellyfin_nfo_create_series", "1" if data.get("create_series") else "0")
        set_setting("jellyfin_nfo_create_season", "1" if data.get("create_season") else "0")
        set_setting("jellyfin_nfo_create_episode", "1" if data.get("create_episode") else "0")
        set_setting("jellyfin_nfo_create_movie", "1" if data.get("create_movie") else "0")
        set_setting("jellyfin_nfo_meta_plot", "1" if data.get("meta_plot") else "0")
        set_setting("jellyfin_nfo_meta_genres", "1" if data.get("meta_genres") else "0")
        set_setting("jellyfin_nfo_meta_rating", "1" if data.get("meta_rating") else "0")
        set_setting("jellyfin_nfo_meta_fsk", "1" if data.get("meta_fsk") else "0")
        set_setting("jellyfin_nfo_meta_actors", "1" if data.get("meta_actors") else "0")
        set_setting("jellyfin_nfo_meta_trailer", "1" if data.get("meta_trailer") else "0")
        set_setting("jellyfin_nfo_meta_date", "1" if data.get("meta_date") else "0")
        set_setting("jellyfin_nfo_meta_studio", "1" if data.get("meta_studio") else "0")
        return jsonify({"ok": True})
