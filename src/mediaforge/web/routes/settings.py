"""General settings routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from .. import runtime_state as _runtime_state
from .. import selfupdate
from ... import mirrors as _mirrors
from ...config import LANG_LABELS
from ..autosync_worker import _normalize_sync_times
from ..autosync_worker import _parse_sync_days
from ..db import add_custom_path
from ..db import add_language_group
from ..db import clear_tmdb_cache
from ..db import count_language_group_users
from ..db import get_custom_paths
from ..db import get_language_groups
from ..db import get_setting
from ..db import remove_custom_path
from ..db import remove_language_group
from ..db import set_setting
from ..db import update_custom_path
from ..db import update_language_group
from ..language_groups import SELECTABLE_LANGUAGES
from ..language_groups import group_languages_json
from ..language_groups import is_group_ref
from ..language_groups import lang_separation_enabled
from ..language_groups import resolve_chain
from ..dns_patch import _DNS_PRESETS
from ..dns_patch import _apply_dns_patch
from .. import dns_patch
from ..queue_worker import _validate_server_url
from ..runtime_state import SYNC_ADAPTIVE_PAUSE_MAP
from ..runtime_state import SYNC_ADAPTIVE_UNIT_MAP
from ..runtime_state import SYNC_RETRY_MAP
from ..runtime_state import SYNC_SCHEDULE_MAP
from ..runtime_state import WORKING_PROVIDERS
from ..settings_migration import _apply_captcha_env
from ..uptime_monitor import _MONITOR_SITES
from ..uptime_monitor import _probe_site
from flask import jsonify
from flask import render_template
from flask import request
import json
import os
import secrets
import threading
from ..request_context import get_current_user_info as _get_current_user_info
from ...logger import get_logger


logger = get_logger(__name__)


def _language_group_error(language):
    """Reason a language value can't be used, or None if it's fine.

    Mirrors routes/autosync.py's check: a group needs per-language folders (see
    language_groups.lang_separation_enabled) and has to still exist.
    """
    if not is_group_ref(language):
        return None
    if not lang_separation_enabled():
        return "Sprachgruppen benötigen die Einstellung 'Sprachen in Ordner trennen'."
    if not resolve_chain(language):
        return f"Unknown language group: {language}"
    return None


def _normalize_default_sites(value):
    """Return a validated, de-duplicated CSV of supported site keys."""
    raw = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    sites = []
    for item in raw:
        site = str(item).strip().lower()
        if site in _mirrors.SITE_LABELS and site not in sites:
            sites.append(site)
    return ",".join(sites)


def register_settings_routes(app):
    """Register the settings page and all General/Sync/DNS/CaptchaBrowser/
    SSO/CineInfo/legacy-import/custom-paths/API-key settings API routes on
    the Flask app."""
    @app.route("/settings")
    def settings_page():
        """Serve GET /settings: render the settings page, passing the
        display path of the legacy .env file for the migration banner."""
        from pathlib import Path
        import platform

        env_path = Path.home() / ".mediaforge" / ".env"
        if platform.system() != "Windows":
            display = "~/.mediaforge/.env"
        else:
            display = str(env_path)
        return render_template("settings.html", env_path=display)
    @app.route("/api/user/language", methods=["POST"])
    def api_user_language():
        """Serve POST /api/user/language: save the current user's UI language
        preference (EN/DE) to the session (and to the DB if logged in).
        Called from templates/base.html's and templates/settings.html's
        inline language-switcher scripts."""
        from flask import session as _sess
        from ..db import set_user_language as _set_lang
        data = request.get_json(force=True, silent=True) or {}
        lang = data.get("language", "en")
        if lang not in ("en", "de"):
            return jsonify({"error": "Unsupported language"}), 400
        _sess["ui_language"] = lang
        _sess["_lang_synced"] = True
        uid = _sess.get("user_id")
        if uid and uid > 0:
            _set_lang(uid, lang)
        return jsonify({"ok": True, "language": lang})
    @app.route("/api/settings", methods=["GET"])
    def api_settings():
        """Serve GET /api/settings: return the full combined settings blob
        (downloads, sync, CineInfo, Crunchyroll, fernsehserien, home-page
        source order, etc.), falling back to environment variables and then
        defaults for any value not yet stored in the DB. Called from many
        frontend files via `fetch('/api/settings')`, e.g. static/app.js,
        static/autosync.js, and static/integrations.js's `_getSettings()`."""
        from pathlib import Path

        raw = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            resolved = str(p)
        else:
            resolved = str(Path.home() / "Downloads")
        lang_separation      = get_setting("lang_separation")      or os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0")
        disable_english_sub  = get_setting("disable_english_sub")  or os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0")
        movie_subfolder      = get_setting("movie_subfolder")      or get_setting("filmpalast_movie_subfolder", "0")
        sync_schedule        = get_setting("sync_schedule")        or os.environ.get("MEDIAFORGE_SYNC_SCHEDULE", "0")
        sync_mode            = get_setting("sync_mode")            or os.environ.get("MEDIAFORGE_SYNC_MODE", "interval")
        sync_days            = get_setting("sync_days")            or os.environ.get("MEDIAFORGE_SYNC_DAYS", "0,1,2,3,4,5,6")
        sync_times           = get_setting("sync_times")           or os.environ.get("MEDIAFORGE_SYNC_TIMES", "06:00")
        sync_language               = get_setting("sync_language")               or os.environ.get("MEDIAFORGE_SYNC_LANGUAGE", "German Dub")
        sync_provider               = get_setting("sync_provider")               or os.environ.get("MEDIAFORGE_SYNC_PROVIDER", "VOE")
        sync_path_unavailable_action = get_setting("sync_path_unavailable_action") or os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip")
        sync_error_retries   = int(get_setting("sync_error_retries") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
        sync_error_retry_time = get_setting("sync_error_retry_time") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRY_TIME", "5min")
        sync_adaptive_enabled     = get_setting("sync_adaptive_enabled")     or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_ENABLED", "0")
        sync_adaptive_pause_after = get_setting("sync_adaptive_pause_after") or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER", "4w")
        sync_adaptive_retry_value = int(get_setting("sync_adaptive_retry_value") or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_VALUE", "2"))
        sync_adaptive_retry_unit  = get_setting("sync_adaptive_retry_unit")  or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_UNIT", "days")
        history_retention_days = get_setting("history_retention_days") or os.environ.get("MEDIAFORGE_HISTORY_RETENTION_DAYS", "30")
        download_language    = get_setting("download_language")    or os.environ.get("MEDIAFORGE_LANGUAGE", "German Dub")
        download_provider    = get_setting("download_provider")    or os.environ.get("MEDIAFORGE_PROVIDER", "VOE")
        naming_template      = get_setting("naming_template")      or os.environ.get("MEDIAFORGE_NAMING_TEMPLATE", "{title} ({year}) [imdbid-{imdbid}]/Season {season}/{title} S{season}E{episode}.mkv")
        download_rate_limit  = int(get_setting("download_rate_limit") or os.environ.get("MEDIAFORGE_DOWNLOAD_RATE_LIMIT", "0"))
        download_window_enabled = get_setting("download_window_enabled") or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_ENABLED", "0")
        download_window_start   = get_setting("download_window_start")   or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_START", "22:00")
        download_window_end     = get_setting("download_window_end")     or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_END", "06:00")
        web_base_url         = get_setting("web_base_url")         or os.environ.get("MEDIAFORGE_WEB_BASE_URL", "")
        debug_forced         = os.environ.get("MEDIAFORGE_DEBUG_FORCED", "0")
        debug_mode           = get_setting("debug_mode")           or os.environ.get("MEDIAFORGE_DEBUG_MODE", "0")
        if debug_forced == "1":
            debug_mode = "1"
        media_stats_enabled  = get_setting("media_stats_enabled")  or os.environ.get("MEDIAFORGE_MEDIA_STATS_ENABLED", "0")
        web_console          = get_setting("web_console")          or os.environ.get("MEDIAFORGE_WEB_CONSOLE", "0")
        
        # A default may point at a group that has since been deleted (deletion
        # is only blocked while jobs/queue items use it), or language separation
        # may have been switched off, which disables groups entirely. Either way
        # hand the frontend a language it can actually preselect rather than a
        # value no dropdown can show. (Turning the setting off in the UI also
        # writes a real language back, same as it already does for "All
        # Languages" — this only covers the paths that don't.)
        language_groups = get_language_groups()
        _usable_groups = (
            {f"group:{g['id']}" for g in language_groups}
            if lang_separation == "1" else set()
        )
        if is_group_ref(sync_language) and sync_language not in _usable_groups:
            sync_language = "German Dub"
        if is_group_ref(download_language) and download_language not in _usable_groups:
            download_language = "German Dub"

        tray_mode            = get_setting("tray_mode", "0")
        autostart_enabled    = get_setting("autostart_enabled", "0")
        open_browser_on_startup = get_setting("open_browser_on_startup", "1")
        is_docker            = os.path.exists("/.dockerenv") or os.environ.get("MEDIAFORGE_DOCKER") == "1"

        return jsonify(
            {
                "download_path":             resolved,
                "lang_separation":           lang_separation,
                "disable_english_sub":       disable_english_sub,
                "filmpalast_movie_subfolder": movie_subfolder,
                "movie_subfolder":            movie_subfolder,
                "sync_schedule":             sync_schedule,
                "sync_mode":                 sync_mode,
                "sync_days":                 sync_days,
                "sync_times":                sync_times,
                "sync_language":              sync_language,
                # Every page with a language dropdown reads /api/settings
                # already, so the groups ride along instead of costing each of
                # them a second request (app.js, autosync.js, settings.js).
                "language_groups":           language_groups,
                "sync_provider":              sync_provider,
                "sync_path_unavailable_action": sync_path_unavailable_action,
                "sync_error_retries":         sync_error_retries,
                "sync_error_retry_time":      sync_error_retry_time,
                "sync_adaptive_enabled":      sync_adaptive_enabled,
                "sync_adaptive_pause_after":  sync_adaptive_pause_after,
                "sync_adaptive_retry_value":  sync_adaptive_retry_value,
                "sync_adaptive_retry_unit":   sync_adaptive_retry_unit,
                "history_retention_days":     history_retention_days,
                "download_language":         download_language,
                "download_provider":         download_provider,
                "naming_template":           naming_template,
                "download_rate_limit":       download_rate_limit,
                "download_window_enabled":   download_window_enabled,
                "download_window_start":     download_window_start,
                "download_window_end":       download_window_end,
                "web_base_url":              web_base_url,
                "debug_mode":                debug_mode,
                "debug_forced":              debug_forced,
                "media_stats_enabled":       media_stats_enabled,
                "web_console":               web_console,
                "tray_mode":                 tray_mode,
                "autostart_enabled":         autostart_enabled,
                "open_browser_on_startup":   open_browser_on_startup,
                "is_docker":                 is_docker,
                "syncplay_enabled":          get_setting("syncplay_enabled", "0"),
                "auto_update_enabled":       get_setting("auto_update_enabled", "0"),
                "auto_update_days":          get_setting("auto_update_days", "0,1,2,3,4,5,6"),
                "auto_update_time":          get_setting("auto_update_time", "03:00"),
                "seerr_url":                 get_setting("seerr_url", ""),
                "seerr_api_key":             get_setting("seerr_api_key", ""),
                "seerr_configured":          bool(get_setting("seerr_url", "").strip() and get_setting("seerr_api_key", "").strip()),
                "dns_mode":                  get_setting("dns_mode", "system"),
                "dns_server":                get_setting("dns_server", ""),
                "browser_persistent_profile": get_setting("browser_persistent_profile", "0"),
                "captcha_adblock":            get_setting("captcha_adblock",         "1"),
                "captcha_adtab_guard":        get_setting("captcha_adtab_guard",     "1"),
                "captcha_overlay_removal":    get_setting("captcha_overlay_removal", "1"),
                "captcha_ua_sync":            get_setting("captcha_ua_sync",         "1"),
                "captcha_webgl_spoof":        get_setting("captcha_webgl_spoof",     "0"),
                "captcha_manual":             get_setting("captcha_manual",          "0"),
                "captcha_visible":            get_setting("captcha_visible",         "0"),
                "captcha_timeout":            get_setting("captcha_timeout",         ""),
                "cineinfo": {
                    "tmdb_api_key":   get_setting("cineinfo_tmdb_api_key",   ""),
                    "country":        get_setting("cineinfo_country",        "DE"),
                    "show_providers": get_setting("cineinfo_show_providers", "1"),
                    "show_genres":    get_setting("cineinfo_show_genres",    "0"),
                    "show_fsk":       get_setting("cineinfo_show_fsk",       "1"),
                    "show_rating":    get_setting("cineinfo_show_rating",    "0"),
                    "show_recommendations": get_setting("cineinfo_show_recommendations", "1"),
                    "show_trailer":   get_setting("cineinfo_show_trailer",   "1"),
                    "show_hover_rating": get_setting("cineinfo_show_hover_rating", "0"),
                    "show_hover_genres": get_setting("cineinfo_show_hover_genres", "0"),
                    "show_hover_fsk": get_setting("cineinfo_show_hover_fsk", "0"),
                    "advanced_search": get_setting("cineinfo_advanced_search", "0"),
                    "calendar":        get_setting("cineinfo_calendar",        "0"),
                    "calendar_seerr":  get_setting("cineinfo_calendar_seerr",  "0"),
                    "calendar_mediathek": get_setting("cineinfo_calendar_mediathek", "0"),
                    "calendar_refresh_interval": get_setting("cineinfo_calendar_refresh_interval", "24"),
                },
                "crunchyroll": {
                    "enabled":            get_setting("crunchyroll_enabled",            "0"),
                    "email":              get_setting("crunchyroll_email",              ""),
                    # Never echo the stored password back to the client; only
                    # report whether one is set so the UI can show a placeholder.
                    "has_password":       bool(get_setting("crunchyroll_password",      "")),
                    "locale":             get_setting("crunchyroll_locale",             "de-DE"),
                    "anon":               get_setting("crunchyroll_anon",               "0"),
                    "profile_id":         get_setting("crunchyroll_profile_id",         ""),
                    "show_providers":     get_setting("crunchyroll_show_providers",     "1"),
                    "calendar_simulcast": get_setting("crunchyroll_calendar_simulcast", "0"),
                    "calendar_watchlist": get_setting("crunchyroll_calendar_watchlist", "0"),

                    "calendar_lists":     get_setting("crunchyroll_calendar_lists",     "0"),
                    "calendar_release":   get_setting("crunchyroll_calendar_release",   "0"),
                },
                "fernsehserien": {
                    "enabled":        get_setting("fernsehserien_enabled",        "0"),
                    "show_providers": get_setting("fernsehserien_show_providers", "1"),
                    "delay":          get_setting("fernsehserien_delay",          "1.5"),
                },
                "sources": {
                    "order": get_setting("home_source_order", "aniworld,sto,filmpalast,megakino,hanime"),
                    "section_order": {
                        "aniworld": get_setting("home_section_order_aniworld", "new,popular"),
                        "sto":      get_setting("home_section_order_sto",      "new,popular"),
                        "megakino": get_setting("home_section_order_megakino", "new_movies,popular_movies,new_series,popular_series"),
                        "hanime":   get_setting("home_section_order_hanime",   "new,trending"),
                    },
                    "sections": {
                        "aniworld": {
                            "new":     get_setting("source_show_new_aniworld",     "1"),
                            "popular": get_setting("source_show_popular_aniworld", "1"),
                        },
                        "sto": {
                            "new":     get_setting("source_show_new_sto",     "1"),
                            "popular": get_setting("source_show_popular_sto", "1"),
                        },
                        "megakino": {
                            "new_movies":     get_setting("source_show_new_movies_megakino",     "1"),
                            "popular_movies": get_setting("source_show_popular_movies_megakino", "1"),
                            "new_series":     get_setting("source_show_new_series_megakino",     "1"),
                            "popular_series": get_setting("source_show_popular_series_megakino", "1"),
                        },
                        "hanime": {
                            "new":        get_setting("source_show_new_hanime",        "1"),
                            "trending":   get_setting("source_show_trending_hanime",   "1"),
                            # Content-type filters (applied per item within the New/
                            # Trending lists, not separate sections like the two above).
                            "censored":   get_setting("source_show_censored_hanime",   "1"),
                            "uncensored": get_setting("source_show_uncensored_hanime", "1"),
                        },
                    },
                    "enabled": {
                        "aniworld":   get_setting("source_enabled_aniworld",   "1"),
                        "sto":        get_setting("source_enabled_sto",        "1"),
                        "filmpalast": get_setting("source_enabled_filmpalast", "1"),
                        "megakino":   get_setting("source_enabled_megakino",   "1"),
                        "hanime":     get_setting("source_enabled_hanime",     "0"),
                    },
                    "hide_disabled_in_search": get_setting("sources_hide_in_search", "0"),
                },
                # Hoster order + fallback. "available" is every hoster with a
                # working extractor; "order" is the user's ranking of them,
                # which queue_worker.py walks when the hoster picked for a
                # download fails. See runtime_state.get_provider_fallback_chain().
                "providers": {
                    "available": list(_runtime_state.WORKING_PROVIDERS),
                    "order": _runtime_state.get_provider_order(),
                    "fallback_enabled": "1" if _runtime_state.is_provider_fallback_enabled() else "0",
                },
                # Per-site domain fallback (s.to -> serienstream.to -> origin IP).
                # See mediaforge/mirrors.py.
                "mirrors": {
                    "sites": [
                        {
                            "id": _site,
                            "label": _mirrors.SITE_LABELS.get(_site, _site),
                            "canonical": _mirrors.canonical_host(_site),
                            "hosts": _mirrors.get_mirrors(_site),
                            "active": _mirrors.active_host(_site),
                            "default": list(_default_hosts),
                        }
                        for _site, _default_hosts in _mirrors.DEFAULT_SITE_MIRRORS.items()
                    ],
                },
            }
        )
    @app.route("/api/console", methods=["GET"])
    def api_console():
        """Serve GET /api/console: read-only tail of the live console output
        (admin only).

        Gated behind the ``web_console`` setting so the buffer is never exposed
        unless the feature is explicitly enabled. Called from
        static/settings.js's `_webConsolePoll()`.
        """
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        enabled = (get_setting("web_console") or os.environ.get("MEDIAFORGE_WEB_CONSOLE", "0")) == "1"
        if not enabled:
            return jsonify({"enabled": False, "lines": [], "seq": 0, "partial": "", "first_seq": 0})
        try:
            after = int(request.args.get("after", 0))
        except (TypeError, ValueError):
            after = 0
        from ..console_capture import get_console_output
        out = get_console_output(after)
        out["enabled"] = True
        return jsonify(out)
    @app.route("/api/settings/seerr", methods=["PUT"])
    def api_settings_seerr():
        """Serve PUT /api/settings/seerr: save the Jellyseerr/Overseerr URL
        and API key. Called from static/integrations.js's
        `saveSeerrSettings()`."""
        data = request.get_json(silent=True) or {}
        seerr_url = str(data.get("seerr_url", "")).strip()
        try:
            _validate_server_url(seerr_url)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        set_setting("seerr_url", seerr_url)
        set_setting("seerr_api_key", str(data.get("seerr_api_key", "")).strip())
        return jsonify({"ok": True})
    @app.route("/api/settings/sso", methods=["GET"])
    def api_settings_sso_get():
        """Serve GET /api/settings/sso: return current SSO / OIDC
        configuration from DB (secrets masked). Called from
        static/settings.js's `loadSsoSettings()`."""
        return jsonify({
            "sso_enabled":        get_setting("web_sso", "0"),
            "force_sso":          get_setting("web_force_sso", "0"),
            "oidc_issuer_url":    get_setting("oidc_issuer_url", ""),
            "oidc_client_id":     get_setting("oidc_client_id", ""),
            "oidc_client_secret": "***" if get_setting("oidc_client_secret", "") else "",
            "oidc_display_name":  get_setting("oidc_display_name", "SSO"),
            "oidc_admin_user":    get_setting("oidc_admin_user", ""),
            "oidc_admin_subject": get_setting("oidc_admin_subject", ""),
        })
    @app.route("/api/settings/sso", methods=["PUT"])
    def api_settings_sso_put():
        """Serve PUT /api/settings/sso: save SSO / OIDC configuration to DB
        and apply immediately. Called from static/settings.js's
        `saveSsoSettings()`."""
        data = request.get_json(silent=True) or {}

        def _save(db_key, env_key, default=""):
            val = str(data.get(db_key, "")).strip()
            set_setting(db_key, val)
            os.environ[env_key] = val

        sso_enabled = "1" if data.get("sso_enabled") else "0"
        force_sso   = "1" if data.get("force_sso")   else "0"
        set_setting("web_sso",       sso_enabled); os.environ["MEDIAFORGE_WEB_SSO"]       = sso_enabled
        set_setting("web_force_sso", force_sso);   os.environ["MEDIAFORGE_WEB_FORCE_SSO"] = force_sso

        _save("oidc_issuer_url",    "MEDIAFORGE_OIDC_ISSUER_URL")
        _save("oidc_client_id",     "MEDIAFORGE_OIDC_CLIENT_ID")
        _save("oidc_display_name",  "MEDIAFORGE_OIDC_DISPLAY_NAME")
        _save("oidc_admin_user",    "MEDIAFORGE_OIDC_ADMIN_USER")
        _save("oidc_admin_subject", "MEDIAFORGE_OIDC_ADMIN_SUBJECT")

        # Secret: only overwrite if a real value was sent (not the "***" placeholder)
        secret = str(data.get("oidc_client_secret", "")).strip()
        if secret and secret != "***":
            set_setting("oidc_client_secret", secret)
            os.environ["MEDIAFORGE_OIDC_CLIENT_SECRET"] = secret

        return jsonify({"ok": True, "restart_required": True})
    @app.route("/api/settings/cineinfo", methods=["GET"])
    def api_settings_cineinfo_get():
        """Serve GET /api/settings/cineinfo: return the CineInfo/TMDB
        configuration. No confirmed direct frontend caller was found — the
        combined GET /api/settings response (which nests the same fields
        under "cineinfo") is what static/integrations.js actually reads via
        `_getSettings()`."""
        return jsonify({
            "tmdb_api_key":   get_setting("cineinfo_tmdb_api_key",   ""),
            "country":        get_setting("cineinfo_country",        "DE"),
            "show_providers": get_setting("cineinfo_show_providers", "1"),
            "show_genres":    get_setting("cineinfo_show_genres",    "0"),
            "show_fsk":       get_setting("cineinfo_show_fsk",       "1"),
            "show_rating":    get_setting("cineinfo_show_rating",    "0"),
            "show_recommendations": get_setting("cineinfo_show_recommendations", "1"),
            "show_trailer":   get_setting("cineinfo_show_trailer",   "1"),
            "show_hover_rating": get_setting("cineinfo_show_hover_rating", "0"),
            "show_hover_genres": get_setting("cineinfo_show_hover_genres", "0"),
            "show_hover_fsk": get_setting("cineinfo_show_hover_fsk", "0"),
            # Order of the provider-pill sources (TMDB, Crunchyroll,
            # Fernsehserien.de and any module-registered pill, addressed as
            # "ext:<name>"). The frontend treats it as a preference, not a
            # whitelist: unlisted sources are still tried, after the listed
            # ones. See static/app.js's _pillSources().
            "provider_order": get_setting("cineinfo_provider_order", "tmdb,crunchyroll,fernsehserien"),
            "advanced_search": get_setting("cineinfo_advanced_search", "0"),
            "calendar":        get_setting("cineinfo_calendar",        "0"),
            "calendar_seerr":  get_setting("cineinfo_calendar_seerr",  "0"),
            "calendar_mediathek": get_setting("cineinfo_calendar_mediathek", "0"),
            "calendar_refresh_interval": get_setting("cineinfo_calendar_refresh_interval", "24"),
        })
    @app.route("/api/settings/cineinfo", methods=["PUT"])
    def api_settings_cineinfo_put():
        """Serve PUT /api/settings/cineinfo: save CineInfo/TMDB settings, and
        clear the TMDB lookup cache if the API key or country changed (both
        affect lookup results). Called from static/integrations.js's
        `saveCineinfoSettings()` and `saveCineinfoDisplayOptions()`."""
        data = request.get_json(silent=True) or {}
        old_key = get_setting("cineinfo_tmdb_api_key", "")
        old_country = get_setting("cineinfo_country", "DE")

        for key in ["tmdb_api_key", "country", "show_providers",
                    "show_genres", "show_fsk", "show_rating", "show_recommendations", "show_trailer",
                    "show_hover_rating", "show_hover_genres", "show_hover_fsk", "advanced_search",
                    "provider_order",
                    "calendar", "calendar_seerr", "calendar_mediathek", "calendar_refresh_interval"]:
            if key in data:
                set_setting("cineinfo_" + key, str(data[key]))

        new_key = get_setting("cineinfo_tmdb_api_key", "")
        new_country = get_setting("cineinfo_country", "DE")
        if new_key != old_key or new_country != old_country:
            clear_tmdb_cache()

        return jsonify({"ok": True})
    @app.route("/api/settings/env-file", methods=["GET"])
    def api_settings_env_file_get():
        """Serve GET /api/settings/env-file: check whether the legacy .env
        file still exists and has been migrated. Called from
        static/settings.js's `checkEnvFileBanner()` IIFE."""
        from pathlib import Path as _Path
        env_path = _Path.home() / ".mediaforge" / ".env"
        return jsonify({
            "exists":   env_path.exists(),
            "migrated": get_setting("env_migrated") == "1",
        })
    @app.route("/api/settings/env-file", methods=["DELETE"])
    def api_settings_env_file_delete():
        """Serve DELETE /api/settings/env-file: delete the legacy .env file
        after migration. Called from static/settings.js's `deleteEnvFile()`."""
        import os as _os
        from pathlib import Path as _Path
        env_path = _Path.home() / ".mediaforge" / ".env"
        if not env_path.exists():
            return jsonify({"ok": True, "message": "Datei existiert nicht mehr"})
        try:
            env_path.unlink()
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    @app.route("/api/settings/legacy-import", methods=["GET"])
    def api_settings_legacy_import_status():
        """Serve GET /api/settings/legacy-import: report whether data from a
        previous AniWorld install can be imported. Called from
        templates/settings.html's inline legacy-import banner script."""
        from ...legacy_import import detect_legacy
        status = detect_legacy()
        status["dismissed"] = get_setting("legacy_import_dismissed", "0") == "1"
        return jsonify(status)
    @app.route("/api/settings/legacy-import", methods=["POST"])
    def api_settings_legacy_import_run():
        """Serve POST /api/settings/legacy-import: manually import data from
        a previous AniWorld install (~/.aniworld).

        Non-destructive: an existing database is never overwritten (data is
        imported automatically on first start before the DB is created). This
        endpoint fills in any auxiliary files that are still missing. Called
        from templates/settings.html's inline legacy-import banner script."""
        from ...legacy_import import detect_legacy, run_import
        status = detect_legacy()
        if not status["legacy_exists"]:
            return jsonify({"ok": False, "error": "no_legacy_dir"}), 404
        summary = run_import(overwrite=False)
        summary["ok"] = True
        summary["db_replaced"] = False
        summary["restart_required_for_db"] = status["new_has_db"] and status["legacy_has_db"]
        return jsonify(summary)
    @app.route("/api/settings/legacy-import/dismiss", methods=["POST"])
    def api_settings_legacy_import_dismiss():
        """Serve POST /api/settings/legacy-import/dismiss: permanently hide
        the legacy-import card (already imported / not wanted). Called from
        templates/settings.html's inline legacy-import banner dismiss
        handler."""
        set_setting("legacy_import_dismissed", "1")
        return jsonify({"ok": True})
    @app.route("/api/settings/dns", methods=["PUT"])
    def api_settings_dns():
        """Serve PUT /api/settings/dns: save the DNS mode/server and apply
        the DNS patch immediately (no restart needed). Called from
        static/settings.js's `saveDnsSettings()`."""
        data = request.get_json(silent=True) or {}
        mode   = str(data.get("dns_mode",   "system")).strip()
        server = str(data.get("dns_server", "")).strip()

        valid_modes = {"system", "cloudflare", "google", "quad9", "custom"}
        if mode not in valid_modes:
            return jsonify({"error": f"Ungültiger Modus: {mode}"}), 400
        if mode == "custom" and not server:
            return jsonify({"error": "Benutzerdefinierter DNS-Server fehlt"}), 400

        set_setting("dns_mode",   mode)
        set_setting("dns_server", server)

        # Apply patch immediately (no restart needed)
        if mode == "system":
            _apply_dns_patch(None, mode=None)
        else:
            target = _DNS_PRESETS.get(mode) or server
            _apply_dns_patch(target, mode=mode)

        return jsonify({"ok": True, "active_server": dns_patch._active_dns_server})
    @app.route("/api/settings/dns/test", methods=["GET"])
    def api_dns_test():
        """
        Serve GET /api/settings/dns/test: test the current DNS configuration by:
          1. Reporting which DNS mode / server is active in memory.
          2. Resolving each hostname via the patched socket (covers ffmpeg etc.,
             shown for information only — not used to judge reachability).
          3. Making a HEAD request to each site via GLOBAL_SESSION (niquests/DoH)
             and comparing the response headers against that site's known CDN
             signature (see _MONITOR_SITES) so we know we actually reached the
             correct site (not a block page) — headers instead of IP because
             Cloudflare/DDoS-Guard rotate edge IPs constantly.

        Called from static/settings.js's `runDnsTest()`.
        """
        _saved_mode = get_setting("dns_mode", "system")
        _saved_server = get_setting("dns_server", "")

        # Reachability + site-identity check for every trackable source site.
        # Verification priority inside _probe_site:
        #   1. Response headers match the site's known CDN signature, else
        #   2. (fallback GET) body contains a known marker or final URL stayed
        #      on the expected domain (handles CDN challenge pages).
        results = {}
        for _sid, (label, url, expected_domain, markers, headers) in _MONITOR_SITES.items():
            results[label] = _probe_site(url, expected_domain, markers, expected_headers=headers, timeout=10)

        return jsonify({
            "dns_mode":          _saved_mode,
            "dns_server_saved":  _saved_server,
            "dns_active_server": dns_patch._active_dns_server,
            "sites":             results,
        })
    @app.route("/api/settings/browser", methods=["PUT"])
    def api_settings_browser():
        """Serve PUT /api/settings/browser: save browser-persistence and
        captcha-solver behavior settings (admin only). Called from
        static/settings.js's `saveCaptchaSettings()`."""
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}

        def _b(key):
            return "1" if data.get(key) else "0"

        if "persistent_profile" in data:
            set_setting("browser_persistent_profile", _b("persistent_profile"))
        for key, db_key in (
            ("adblock",         "captcha_adblock"),
            ("adtab_guard",     "captcha_adtab_guard"),
            ("overlay_removal", "captcha_overlay_removal"),
            ("ua_sync",         "captcha_ua_sync"),
            ("webgl_spoof",     "captcha_webgl_spoof"),
            ("manual",          "captcha_manual"),
            ("visible",         "captcha_visible"),
        ):
            if key in data:
                set_setting(db_key, _b(key))
        if "timeout" in data:
            raw = str(data.get("timeout", "")).strip()
            if raw == "":
                set_setting("captcha_timeout", "")
            else:
                try:
                    v = int(raw)
                except (ValueError, TypeError):
                    return jsonify({"error": "invalid timeout"}), 400
                set_setting("captcha_timeout", str(max(10, min(1800, v))))

        # Persistent profile env (applies live on the next captcha)
        if get_setting("browser_persistent_profile", "0") == "1":
            os.environ["MEDIAFORGE_PERSISTENT_PROFILE"] = "1"
            os.environ.pop("MEDIAFORGE_NO_PERSISTENT_PROFILE", None)
        else:
            os.environ.pop("MEDIAFORGE_PERSISTENT_PROFILE", None)
        _apply_captcha_env()
        return jsonify({"ok": True})
    @app.route("/api/browser/profile/clear", methods=["POST"])
    def api_browser_profile_clear():
        """Serve POST /api/browser/profile/clear: delete the persistent
        Chromium profile directory (admin only). Called from
        static/settings.js's `clearBrowserProfile()`."""
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        # Refuse while a captcha solve is active — Chromium holds the profile open.
        try:
            from ...playwright import captcha as _cap
            with _cap._active_sessions_lock:
                busy = bool(_cap._active_sessions)
        except Exception:
            busy = False
        if busy:
            return jsonify({"error": "captcha_running"}), 409
        import shutil
        from pathlib import Path as _P
        prof = os.environ.get("MEDIAFORGE_BROWSER_PROFILE") or str(_P.home() / ".mediaforge" / "browser-profile")
        try:
            p = _P(prof)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
            return jsonify({"ok": True, "path": str(p)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    @app.route("/api/restart", methods=["POST"])
    def api_restart():
        """Serve POST /api/restart: trigger a self-restart of the app (admin
        only), requeuing any in-flight downloads first so they resume
        afterwards. Called from static/settings.js's `restartApp()` and
        static/selfupdate.js's post-update restart flow."""
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        try:
            result = selfupdate.start_restart()
        except selfupdate.UpdateError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("[Restart] start failed")
            return jsonify({"error": str(exc)}), 500

        # Requeue running downloads so they resume after the restart.
        try:
            from ..db import get_db
            _c = get_db()
            try:
                _c.execute("UPDATE download_queue SET status = 'queued' WHERE status = 'running'")
                _c.commit()
            finally:
                _c.close()
        except Exception:
            logger.warning("[Restart] could not requeue download queue", exc_info=True)

        def _exit_soon():
            import time as _t
            _t.sleep(1.5)
            logger.info("[Restart] exiting for restart helper")
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True, name="restart-exit").start()
        return jsonify(result)
    @app.route("/api/settings", methods=["PUT"])
    def api_settings_update():
        """Serve PUT /api/settings: update any subset of the general/sync/
        download/debug/source settings sent in the request body (each field
        is only touched if present). Called from many `save*()` functions in
        static/settings.js (e.g. `saveLangSeparation()`), each of which PUTs
        just the fields it owns."""
        data = request.get_json(silent=True) or {}
        logger.info("[Settings] PUT /api/settings received data: %r", data)
        if "download_path" in data:
            val = str(data["download_path"]).strip()
            set_setting("download_path", val)
            os.environ["MEDIAFORGE_DOWNLOAD_PATH"] = val
        # Set below when language separation is switched off while fallback
        # groups are still in use — those jobs stop working, and silently
        # letting them fail on the next run would be the worse answer.
        _lang_sep_warning = None
        if "lang_separation" in data:
            val = "1" if data["lang_separation"] else "0"
            if val == "0":
                _in_use = count_language_group_users()
                if _in_use:
                    _lang_sep_warning = (
                        f"{_in_use} Auto-Sync-Job(s)/Download(s) verwenden eine Sprachgruppe. "
                        "Ohne Sprachtrennung funktionieren Sprachgruppen nicht — "
                        "diese Einträge schlagen beim nächsten Lauf fehl."
                    )
            set_setting("lang_separation", val)
            os.environ["MEDIAFORGE_LANG_SEPARATION"] = val
        if "disable_english_sub" in data:
            val = "1" if data["disable_english_sub"] else "0"
            set_setting("disable_english_sub", val)
            os.environ["MEDIAFORGE_DISABLE_ENGLISH_SUB"] = val
        if "filmpalast_movie_subfolder" in data or "movie_subfolder" in data:
            raw_val = data.get("movie_subfolder") if "movie_subfolder" in data else data.get("filmpalast_movie_subfolder")
            val = "1" if raw_val else "0"
            set_setting("movie_subfolder", val)
            set_setting("filmpalast_movie_subfolder", val)
            os.environ["MEDIAFORGE_MOVIE_SUBFOLDER"] = val
            os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = val
            os.environ["MEGAKINO_MOVIE_SUBFOLDER"] = val
        if "sync_schedule" in data:
            sched = str(data["sync_schedule"])
            if sched != "0" and sched not in SYNC_SCHEDULE_MAP:
                return jsonify({"error": f"Invalid sync_schedule: {sched}"}), 400
            set_setting("sync_schedule", sched)
            os.environ["MEDIAFORGE_SYNC_SCHEDULE"] = sched
        if "sync_mode" in data:
            smode = str(data["sync_mode"]).strip().lower()
            if smode not in ("interval", "weekly"):
                return jsonify({"error": "Invalid sync_mode: must be 'interval' or 'weekly'"}), 400
            set_setting("sync_mode", smode)
            os.environ["MEDIAFORGE_SYNC_MODE"] = smode
        if "sync_days" in data:
            days = _parse_sync_days(data["sync_days"], default="")
            if not days:
                return jsonify({"error": "Invalid sync_days: select at least one weekday"}), 400
            days_str = ",".join(str(d) for d in sorted(days))
            set_setting("sync_days", days_str)
            os.environ["MEDIAFORGE_SYNC_DAYS"] = days_str
        if "sync_times" in data:
            times_str = _normalize_sync_times(data["sync_times"])
            if not times_str:
                return jsonify({"error": "Invalid sync_times: provide at least one HH:MM time"}), 400
            set_setting("sync_times", times_str)
            os.environ["MEDIAFORGE_SYNC_TIMES"] = times_str
        if "sync_language" in data:
            lang = str(data["sync_language"])
            valid_langs = (
                set(LANG_LABELS.values())
                | set(SELECTABLE_LANGUAGES)
                | {"All Languages"}
            )
            if is_group_ref(lang):
                # A group is only a valid default while it still exists and can
                # actually run — otherwise new sync jobs inherit a setting that
                # fails on their first run.
                _err = _language_group_error(lang)
                if _err:
                    return jsonify({"error": _err}), 400
            elif lang not in valid_langs:
                return jsonify({"error": f"Invalid sync_language: {lang}"}), 400
            set_setting("sync_language", lang)
            os.environ["MEDIAFORGE_SYNC_LANGUAGE"] = lang
        if "sync_provider" in data:
            prov = str(data["sync_provider"])
            if prov not in WORKING_PROVIDERS:
                return jsonify({"error": f"Invalid sync_provider: {prov}"}), 400
            set_setting("sync_provider", prov)
            os.environ["MEDIAFORGE_SYNC_PROVIDER"] = prov
        if "sync_path_unavailable_action" in data:
            action = str(data["sync_path_unavailable_action"]).strip().lower()
            if action not in ("skip", "hold"):
                return jsonify({"error": "Invalid sync_path_unavailable_action: must be 'skip' or 'hold'"}), 400
            set_setting("sync_path_unavailable_action", action)
            os.environ["MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION"] = action
        if "sync_error_retries" in data:
            try:
                retries = int(data["sync_error_retries"])
                if retries < 0 or retries > 10:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid sync_error_retries: must be integer between 0 and 10"}), 400
            set_setting("sync_error_retries", str(retries))
            os.environ["MEDIAFORGE_SYNC_ERROR_RETRIES"] = str(retries)
        if "sync_error_retry_time" in data:
            retry_time = str(data["sync_error_retry_time"])
            if retry_time not in SYNC_RETRY_MAP:
                return jsonify({"error": f"Invalid sync_error_retry_time: {retry_time}"}), 400
            set_setting("sync_error_retry_time", retry_time)
            os.environ["MEDIAFORGE_SYNC_ERROR_RETRY_TIME"] = retry_time
        if "sync_adaptive_enabled" in data:
            val = "1" if data["sync_adaptive_enabled"] else "0"
            set_setting("sync_adaptive_enabled", val)
            os.environ["MEDIAFORGE_SYNC_ADAPTIVE_ENABLED"] = val
        if "sync_adaptive_pause_after" in data:
            pause_after = str(data["sync_adaptive_pause_after"])
            if pause_after not in SYNC_ADAPTIVE_PAUSE_MAP:
                return jsonify({"error": f"Invalid sync_adaptive_pause_after: {pause_after}"}), 400
            set_setting("sync_adaptive_pause_after", pause_after)
            os.environ["MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER"] = pause_after
        if "sync_adaptive_retry_value" in data:
            try:
                rv = int(data["sync_adaptive_retry_value"])
                if rv < 2 or rv > 12:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid sync_adaptive_retry_value: must be integer between 2 and 12"}), 400
            set_setting("sync_adaptive_retry_value", str(rv))
            os.environ["MEDIAFORGE_SYNC_ADAPTIVE_RETRY_VALUE"] = str(rv)
        if "sync_adaptive_retry_unit" in data:
            unit = str(data["sync_adaptive_retry_unit"]).strip().lower()
            if unit not in SYNC_ADAPTIVE_UNIT_MAP:
                return jsonify({"error": "Invalid sync_adaptive_retry_unit: must be 'days', 'weeks' or 'months'"}), 400
            set_setting("sync_adaptive_retry_unit", unit)
            os.environ["MEDIAFORGE_SYNC_ADAPTIVE_RETRY_UNIT"] = unit
        if "history_retention_days" in data:
            try:
                hrd = int(data["history_retention_days"])
                if hrd < 0 or hrd > 3650:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid history_retention_days: integer 0-3650"}), 400
            set_setting("history_retention_days", str(hrd))
            os.environ["MEDIAFORGE_HISTORY_RETENTION_DAYS"] = str(hrd)
        if "download_language" in data:
            val = str(data["download_language"]).strip()
            if is_group_ref(val):
                _err = _language_group_error(val)
                if _err:
                    return jsonify({"error": _err}), 400
                # This is only the download dialog's preselection. It must not
                # reach MEDIAFORGE_LANGUAGE, which episode models use as their
                # own default and which knows nothing about groups (see
                # settings_migration._sync_db_settings_to_env).
                set_setting("download_language", val)
            else:
                set_setting("download_language", val)
                os.environ["MEDIAFORGE_LANGUAGE"] = val
        if "download_provider" in data:
            val = str(data["download_provider"]).strip()
            set_setting("download_provider", val)
            os.environ["MEDIAFORGE_PROVIDER"] = val
        if "naming_template" in data:
            val = str(data["naming_template"]).strip()
            set_setting("naming_template", val)
            os.environ["MEDIAFORGE_NAMING_TEMPLATE"] = val
        if "download_rate_limit" in data:
            try:
                rate = int(data["download_rate_limit"])
                if rate < 0 or rate > 1_000_000:
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid download_rate_limit: must be integer KB/s between 0 and 1000000"}), 400
            set_setting("download_rate_limit", str(rate))
            os.environ["MEDIAFORGE_DOWNLOAD_RATE_LIMIT"] = str(rate)
        if "download_window_enabled" in data:
            val = "1" if data["download_window_enabled"] else "0"
            set_setting("download_window_enabled", val)
            os.environ["MEDIAFORGE_DOWNLOAD_WINDOW_ENABLED"] = val

        def _valid_hhmm(s):
            try:
                h, m = (int(x) for x in str(s).split(":"))
                return 0 <= h <= 23 and 0 <= m <= 59
            except (ValueError, AttributeError):
                return False
        for _wkey, _envk in (("download_window_start", "MEDIAFORGE_DOWNLOAD_WINDOW_START"),
                             ("download_window_end", "MEDIAFORGE_DOWNLOAD_WINDOW_END")):
            if _wkey in data:
                hhmm = str(data[_wkey]).strip()
                if not _valid_hhmm(hhmm):
                    return jsonify({"error": f"Invalid {_wkey}: must be HH:MM (24h)"}), 400
                # normalise to zero-padded HH:MM
                _h, _m = (int(x) for x in hhmm.split(":"))
                hhmm = f"{_h:02d}:{_m:02d}"
                set_setting(_wkey, hhmm)
                os.environ[_envk] = hhmm
        if "web_base_url" in data:
            val = str(data["web_base_url"]).strip().rstrip("/")
            set_setting("web_base_url", val)
            os.environ["MEDIAFORGE_WEB_BASE_URL"] = val
        if "debug_mode" in data and os.environ.get("MEDIAFORGE_DEBUG_FORCED", "0") != "1":
            val = str(data["debug_mode"])
            if val.lower() in ("true", "1"): val = "1"
            else: val = "0"
            set_setting("debug_mode", val)
            os.environ["MEDIAFORGE_DEBUG_MODE"] = val
            import logging
            enabled = (val == "1")
            level = logging.DEBUG if enabled else logging.WARNING
            # Root logger — covers loggers that purely inherit its level.
            logging.getLogger().setLevel(level)
            # The app's own "mediaforge" logger has propagate=False, so changing
            # the root level alone has no effect on it. It must be toggled
            # directly — this is what actually makes debug output start/stop
            # live without a restart (both for enabling AND disabling).
            logging.getLogger("mediaforge").setLevel(level)
            # Werkzeug's dev-server request logger installs its OWN handler at
            # INFO and does NOT merely inherit the root level, so lowering root
            # to WARNING does not silence its per-request log lines. When the app
            # booted with debug_mode=1 it runs under app.run(debug=True) (the
            # Flask dev server), whose request logging would otherwise keep
            # flooding the Web Console on every ~1.5s /api/console poll even after
            # debug is switched off here. Toggle it explicitly so disabling debug
            # actually quietens it. Note: fully leaving dev-server/reloader mode
            # still needs a restart — the dev-server-vs-waitress choice in
            # app.py's run() is read from MEDIAFORGE_DEBUG_MODE once at startup.
            logging.getLogger("werkzeug").setLevel(level)
            try:
                from ...logger import set_debug_mode as _set_debug_mode
                _set_debug_mode(enabled)
            except Exception:
                pass
        if "media_stats_enabled" in data:
            val = "1" if str(data["media_stats_enabled"]).lower() in ("true", "1") else "0"
            set_setting("media_stats_enabled", val)
            os.environ["MEDIAFORGE_MEDIA_STATS_ENABLED"] = val
        if "web_console" in data:
            val = "1" if str(data["web_console"]).lower() in ("true", "1") else "0"
            set_setting("web_console", val)
            os.environ["MEDIAFORGE_WEB_CONSOLE"] = val

        if "tray_mode" in data:
            val = "1" if str(data["tray_mode"]).lower() in ("true", "1") else "0"
            set_setting("tray_mode", val)

        if "open_browser_on_startup" in data:
            val = "1" if str(data["open_browser_on_startup"]).lower() in ("true", "1") else "0"
            set_setting("open_browser_on_startup", val)

        if "autostart_enabled" in data:
            val = "1" if str(data["autostart_enabled"]).lower() in ("true", "1") else "0"
            set_setting("autostart_enabled", val)
            try:
                from ...autostart import set_autostart
                set_autostart(val == "1")
            except Exception as e:
                logger.error(f"Failed to configure autostart: {e}")
        if "syncplay_enabled" in data:
            val = "1" if str(data["syncplay_enabled"]).lower() in ("true", "1") else "0"
            set_setting("syncplay_enabled", val)
        if "auto_update_enabled" in data:
            val = "1" if data["auto_update_enabled"] else "0"
            set_setting("auto_update_enabled", val)
            os.environ["MEDIAFORGE_AUTO_UPDATE_ENABLED"] = val
        if "auto_update_days" in data:
            days = _parse_sync_days(data["auto_update_days"], default="")
            if not days:
                return jsonify({"error": "Invalid auto_update_days: select at least one weekday"}), 400
            days_str = ",".join(str(d) for d in sorted(days))
            set_setting("auto_update_days", days_str)
            os.environ["MEDIAFORGE_AUTO_UPDATE_DAYS"] = days_str
        if "auto_update_time" in data:
            t_raw = str(data["auto_update_time"]).strip()
            try:
                _h, _m = (int(x) for x in t_raw.split(":"))
                if not (0 <= _h <= 23 and 0 <= _m <= 59):
                    raise ValueError()
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid auto_update_time: must be HH:MM (24h)"}), 400
            t_norm = f"{_h:02d}:{_m:02d}"
            set_setting("auto_update_time", t_norm)
            os.environ["MEDIAFORGE_AUTO_UPDATE_TIME"] = t_norm
        # -- Sources: order & enablement (admin only) --
        _source_keys = (
            "home_source_order",
            "home_section_order_aniworld", "home_section_order_sto", "home_section_order_hanime",
            "home_section_order_megakino",
            "source_enabled_aniworld", "source_enabled_sto", "source_enabled_filmpalast",
            "source_enabled_megakino", "source_enabled_hanime",
            "source_show_new_aniworld", "source_show_popular_aniworld",
            "source_show_new_sto", "source_show_popular_sto",
            "source_show_new_hanime", "source_show_trending_hanime",
            "source_show_censored_hanime", "source_show_uncensored_hanime",
            "source_show_new_movies_megakino", "source_show_popular_movies_megakino",
            "source_show_new_series_megakino", "source_show_popular_series_megakino",
            "sources_hide_in_search",
        )
        if any(_sk in data for _sk in _source_keys):
            _su, _sadmin = _get_current_user_info()
            if not _sadmin:
                return jsonify({"error": "forbidden"}), 403
        if "home_source_order" in data:
            _valid_provs = {"aniworld", "sto", "filmpalast", "megakino", "hanime"}
            _parts = [p.strip().lower() for p in str(data["home_source_order"]).split(",") if p.strip()]
            if not _parts or any(p not in _valid_provs for p in _parts) or len(set(_parts)) != len(_parts):
                return jsonify({"error": "Invalid home_source_order"}), 400
            set_setting("home_source_order", ",".join(_parts))
        if "home_section_order_hanime" in data:
            _parts = [p.strip().lower() for p in str(data["home_section_order_hanime"]).split(",") if p.strip()]
            if sorted(_parts) != ["new", "trending"]:
                return jsonify({"error": "Invalid home_section_order_hanime: must be a permutation of new,trending"}), 400
            set_setting("home_section_order_hanime", ",".join(_parts))
        for _prov in ("aniworld", "sto"):
            _k = "home_section_order_" + _prov
            if _k in data:
                _parts = [p.strip().lower() for p in str(data[_k]).split(",") if p.strip()]
                if sorted(_parts) != ["new", "popular"]:
                    return jsonify({"error": "Invalid %s: must be a permutation of new,popular" % _k}), 400
                set_setting(_k, ",".join(_parts))
        for _prov in ("aniworld", "sto", "filmpalast", "megakino", "hanime"):
            _k = "source_enabled_" + _prov
            if _k in data:
                set_setting(_k, "1" if str(data[_k]).lower() in ("true", "1") else "0")
        for _prov in ("aniworld", "sto"):
            for _sec in ("new", "popular"):
                _k = "source_show_" + _sec + "_" + _prov
                if _k in data:
                    set_setting(_k, "1" if str(data[_k]).lower() in ("true", "1") else "0")
        for _sec in ("new", "trending", "censored", "uncensored"):
            _k = "source_show_" + _sec + "_hanime"
            if _k in data:
                set_setting(_k, "1" if str(data[_k]).lower() in ("true", "1") else "0")
        if "home_section_order_megakino" in data:
            _parts = [p.strip().lower() for p in str(data["home_section_order_megakino"]).split(",") if p.strip()]
            if sorted(_parts) != ["new_movies", "new_series", "popular_movies", "popular_series"]:
                return jsonify({"error": "Invalid home_section_order_megakino"}), 400
            set_setting("home_section_order_megakino", ",".join(_parts))
        for _sec in ("new_movies", "popular_movies", "new_series", "popular_series"):
            _k = "source_show_" + _sec + "_megakino"
            if _k in data:
                set_setting(_k, "1" if str(data[_k]).lower() in ("true", "1") else "0")
        if "sources_hide_in_search" in data:
            set_setting("sources_hide_in_search", "1" if str(data["sources_hide_in_search"]).lower() in ("true", "1") else "0")

        # -- Provider order & fallback (admin only) --
        # The order the download queue walks when a hoster fails; see
        # runtime_state.get_provider_fallback_chain() and queue_worker's
        # _build_attempt_plan().
        _provider_keys = ("provider_order", "provider_fallback_enabled")
        _mirror_keys = tuple("site_mirrors_" + s for s in _mirrors.DEFAULT_SITE_MIRRORS)
        if any(_k in data for _k in _provider_keys + _mirror_keys):
            _pu, _padmin = _get_current_user_info()
            if not _padmin:
                return jsonify({"error": "forbidden"}), 403
        if "provider_order" in data:
            raw = data["provider_order"]
            parts = raw if isinstance(raw, list) else str(raw).split(",")
            by_lower = {p.lower(): p for p in WORKING_PROVIDERS}
            order = []
            for p in parts:
                canonical = by_lower.get(str(p).strip().lower())
                if canonical and canonical not in order:
                    order.append(canonical)
            if not order:
                return jsonify({"error": "Invalid provider_order"}), 400
            set_setting("provider_order", ",".join(order))
        if "provider_fallback_enabled" in data:
            set_setting(
                "provider_fallback_enabled",
                "1" if str(data["provider_fallback_enabled"]).lower() in ("true", "1") else "0",
            )

        # -- Site mirrors (admin only) --
        # One key per site, e.g. site_mirrors_sto = "s.to,serienstream.to,186.2.175.5".
        # The canonical host is always kept first by mirrors.py itself, so a
        # user cannot accidentally drop the primary domain.
        _mirrors_changed = False
        for _site in _mirrors.DEFAULT_SITE_MIRRORS:
            _key = "site_mirrors_" + _site
            if _key not in data:
                continue
            raw = data[_key]
            parts = raw if isinstance(raw, list) else str(raw).split(",")
            hosts = []
            for h in parts:
                host = _mirrors._clean_host(h)
                if host and host not in hosts:
                    hosts.append(host)
            set_setting(_key, ",".join(hosts))
            _mirrors_changed = True
        if _mirrors_changed:
            _mirrors.invalidate_cache()  # re-read the lists + retry the primary host

        if _lang_sep_warning:
            return jsonify({"ok": True, "warning": _lang_sep_warning})
        return jsonify({"ok": True})
    @app.route("/api/custom-paths")
    def api_custom_paths():
        """Serve GET /api/custom-paths: list configured custom download
        paths. Called from several frontend files via
        `fetch('/api/custom-paths')`, e.g. static/settings.js's
        `loadCustomPaths()`, static/app.js, static/autosync.js,
        static/queue.js, static/library.js, and static/seerr.js."""
        paths = get_custom_paths()
        return jsonify(
            {
                "paths": paths,
                "site_options": [
                    {"key": key, "label": label}
                    for key, label in _mirrors.SITE_LABELS.items()
                ],
                "current_site": _mirrors.site_for_url(request.args.get("url", "")),
            }
        )
    @app.route("/api/custom-paths", methods=["POST"])
    def api_custom_paths_add():
        """Serve POST /api/custom-paths: add a named custom download path.
        Called from static/settings.js's `addCustomPath()`."""
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        path = (data.get("path") or "").strip()
        if not name or not path:
            return jsonify({"error": "name and path are required"}), 400
        default_sites = _normalize_default_sites(data.get("default_sites"))
        path_id = add_custom_path(name, path, default_sites)
        return jsonify({"ok": True, "id": path_id})
    @app.route("/api/custom-paths/<int:path_id>", methods=["PUT"])
    def api_custom_paths_update(path_id):
        """Update a custom path's optional site-default assignment.

        The app-wide endpoint wrapper also marks this endpoint admin-only.
        Keeping this guard here prevents a direct registration or future
        routing change from exposing custom-path updates to regular users.
        """
        _username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "admin access required"}), 403
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        path = data.get("path")
        default_sites = (
            _normalize_default_sites(data.get("default_sites"))
            if "default_sites" in data
            else None
        )
        update_custom_path(
            path_id,
            name=name.strip() if isinstance(name, str) else None,
            path=path.strip() if isinstance(path, str) else None,
            default_sites=default_sites,
        )
        return jsonify({"ok": True})
    @app.route("/api/custom-paths/<int:path_id>", methods=["DELETE"])
    def api_custom_paths_delete(path_id):
        """Serve DELETE /api/custom-paths/<path_id>: remove a custom download
        path by id. Called from static/settings.js's `deleteCustomPath()`."""
        ok, err = remove_custom_path(path_id)
        if not ok:
            return jsonify({"error": err}), 409
        return jsonify({"ok": True})
    # ===== Language fallback groups (Downloads tab) =====

    @app.route("/api/language-groups")
    def api_language_groups():
        """Serve GET /api/language-groups: list the configured language
        fallback groups plus the languages a group may be built from. Called
        from static/settings.js's `loadLanguageGroups()`; the group list is
        also embedded in /api/settings so the download and auto-sync pages
        don't need a second request just to fill a dropdown."""
        return jsonify({
            "groups": get_language_groups(),
            "languages": SELECTABLE_LANGUAGES,
        })

    @app.route("/api/language-groups", methods=["POST"])
    def api_language_groups_add():
        """Serve POST /api/language-groups: create a fallback group from a
        name and an ordered language list. Called from static/settings.js's
        `addLanguageGroup()`."""
        _username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "admin access required"}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        languages_json = group_languages_json(data.get("languages"))
        if not name:
            return jsonify({"error": "name is required"}), 400
        # A one-language group is pointless (it can never fall back) and an
        # empty one would resolve to "no language at all" in both workers.
        if len(json.loads(languages_json)) < 2:
            return jsonify({"error": "at least two languages are required"}), 400
        group_id = add_language_group(name, languages_json)
        return jsonify({"ok": True, "id": group_id})

    @app.route("/api/language-groups/<int:group_id>", methods=["PUT"])
    def api_language_groups_update(group_id):
        """Serve PUT /api/language-groups/<id>: rename a group or replace its
        language chain. Called from static/settings.js's `saveLanguageGroup()`."""
        _username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "admin access required"}), 403
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        languages_json = None
        if "languages" in data:
            languages_json = group_languages_json(data.get("languages"))
            if len(json.loads(languages_json)) < 2:
                return jsonify({"error": "at least two languages are required"}), 400
        if isinstance(name, str) and not name.strip():
            return jsonify({"error": "name is required"}), 400
        update_language_group(
            group_id,
            name=name.strip() if isinstance(name, str) else None,
            languages_json=languages_json,
        )
        return jsonify({"ok": True})

    @app.route("/api/language-groups/<int:group_id>", methods=["DELETE"])
    def api_language_groups_delete(group_id):
        """Serve DELETE /api/language-groups/<id>: remove a group unless a
        sync job or a waiting download still references it. Called from
        static/settings.js's `deleteLanguageGroup()`."""
        _username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "admin access required"}), 403
        ok, err = remove_language_group(group_id)
        if not ok:
            return jsonify({"error": err}), 409
        return jsonify({"ok": True})

    @app.route("/api/settings/api-key", methods=["GET"])
    def api_settings_api_key_get():
        """Serve GET /api/settings/api-key: return the current external API
        key. Called from static/settings.js's `loadApiKey()`."""
        key = get_setting("external_api_key", "")
        return jsonify({"key": key})
    @app.route("/api/settings/api-key/regenerate", methods=["POST"])
    def api_settings_api_key_regenerate():
        """Serve POST /api/settings/api-key/regenerate: generate a new
        external API key. Called from static/settings.js's
        `regenerateApiKey()`."""
        new_key = secrets.token_hex(32)
        set_setting("external_api_key", new_key)
        return jsonify({"ok": True, "key": new_key})

    # ===== Telemetry (Privacy & Telemetry tab) =====
    # See TELEMETRY_PLAN.md / TELEMETRY_IMPLEMENTATION_PLAN.md (devinfo_server
    # checkout) for the full design. Persistence goes through the existing
    # DB-first settings store via mediaforge.telemetry.settings, same pattern
    # as every other setting in this file -- no new table.

    @app.route("/api/settings/telemetry", methods=["GET"])
    def api_settings_telemetry_get():
        """Serve GET /api/settings/telemetry: current consent/enabled_keys
        state plus the full data-point registry (labels/explain text/stage),
        so the frontend confirmation dialog never needs a second,
        hand-copied source for what each data_key means. Called from
        static/telemetry.js's loadTelemetrySettings() and from base.html's
        first-run consent-dialog bootstrap (checks consent_given === null)."""
        from ...telemetry import settings as _tel
        from ...telemetry.registry import registry_export
        return jsonify({
            "install_id": _tel.get_install_id(),
            "consent_given": _tel.is_consent_given(),
            "consent_at": _tel.get_consent_at(),
            "enabled_keys": sorted(_tel.get_enabled_keys()),
            "registry": registry_export(),
        })

    def _telemetry_kick():
        """Best-effort: submit one system_info event right now, in this
        request, instead of waiting for the next app start.

        Previously the only "proof of life" event was the one-shot
        system_info sent from hooks.init_telemetry() at create_app() time --
        which meant granting consent (or raising the stage) mid-session
        produced no visible signal on the devInfo server until the app was
        restarted (the actual crash/feature/download events all check
        settings.is_key_enabled() live and never needed a restart; system_info
        was the only piece of the pipeline gated to "once per process start").
        Called after every change that could newly enable something, so the
        Settings page's Save/consent buttons are self-evidently working
        without needing a restart to confirm it. Never raises -- a failure
        here must not turn a settings change into a 500."""
        try:
            from ...telemetry import events as _tel_events
            from ...telemetry.client import get_client as _tel_get_client
            event = _tel_events.build_system_info_event()
            if event:
                _tel_get_client().submit(event)
        except Exception:
            logger.debug("[Telemetry] immediate kick-event failed", exc_info=True)

    @app.route("/api/settings/telemetry", methods=["PUT"])
    def api_settings_telemetry_put():
        """Serve PUT /api/settings/telemetry: overwrite the full set of
        enabled data_keys (not additive -- the frontend always sends the
        complete desired end state after its own confirmation dialog, see
        TELEMETRY_IMPLEMENTATION_PLAN.md §3.7). Refuses to store anything
        until consent has been granted -- the Settings page can only ever
        widen what's enabled after the first-run consent dialog said "Yes".
        Called from static/telemetry.js's saveTelemetrySettings()."""
        from ...telemetry import settings as _tel
        from ...telemetry.registry import DATA_REGISTRY
        if _tel.is_consent_given() is not True:
            return jsonify({"ok": False, "error": "Consent not given yet"}), 403
        data = request.get_json(silent=True) or {}
        requested = data.get("enabled_keys", [])
        if not isinstance(requested, list):
            return jsonify({"ok": False, "error": "enabled_keys must be a list"}), 400
        # Only persist keys the registry actually knows about -- an
        # unrecognized key would be dead weight the server-side registry
        # mirror could never explain to an admin either.
        valid = {k for k in requested if k in DATA_REGISTRY}
        # install_id has no toggle of its own (see registry.DATA_REGISTRY's
        # always_on flag) but is implicitly present whenever anything else is.
        if valid:
            valid.add("install_id")
        grew = bool(valid - _tel.get_enabled_keys())
        _tel.set_enabled_keys(valid)
        if grew:
            # Something new was just turned on (e.g. raised to a higher stage).
            # The actual downloads/watch/feature events for it were already live
            # (is_key_enabled() is checked at the moment of use, no restart ever
            # needed for those) -- this kick just makes sure the install shows up
            # on the server with an up to date enabled_keys snapshot right away,
            # rather than only after whatever action the new stage tracks next
            # happens to occur.
            _telemetry_kick()
        return jsonify({"ok": True, "enabled_keys": sorted(valid)})

    @app.route("/api/settings/telemetry/consent", methods=["POST"])
    def api_settings_telemetry_consent():
        """Serve POST /api/settings/telemetry/consent: record the first-run
        consent decision (granted=true/false). Called once from the
        first-run consent dialog (base.html), and again from the Privacy
        tab whenever the user later flips telemetry fully on/off -- both
        paths go through this same endpoint so consent_at always reflects
        the most recent explicit decision (TELEMETRY_PLAN.md §7a: withdrawal
        must be exactly as easy as granting)."""
        from ...telemetry import settings as _tel
        data = request.get_json(silent=True) or {}
        granted = bool(data.get("granted"))
        _tel.set_consent(granted)
        if granted:
            _telemetry_kick()
        return jsonify({
            "ok": True,
            "consent_given": granted,
            "consent_at": _tel.get_consent_at(),
            "enabled_keys": sorted(_tel.get_enabled_keys()),
        })

    @app.route("/api/settings/telemetry/regenerate-id", methods=["POST"])
    def api_settings_telemetry_regenerate_id():
        """Serve POST /api/settings/telemetry/regenerate-id: "Identität
        zurücksetzen" -- generate a brand-new install_id with no link kept
        to the old one (TELEMETRY_IMPLEMENTATION_PLAN.md §3.1)."""
        from ...telemetry import settings as _tel
        new_id = _tel.regenerate_install_id()
        return jsonify({"ok": True, "install_id": new_id})

    @app.route("/api/settings/telemetry/request", methods=["POST"])
    def api_settings_telemetry_request():
        """Serve POST /api/settings/telemetry/request: submit a data
        deletion/export request from inside the app ("Meine Daten
        verwalten"), forwarded to the devInfo server's
        POST /telemetry/request-from-app with the shared project-key header
        (TELEMETRY_IMPLEMENTATION_PLAN.md §3.8). install_id is attached here
        server-side from the local settings -- never typed by the user, so
        the resulting request proves it came from this actual installation."""
        from ...config import GLOBAL_SESSION
        from ...telemetry import settings as _tel
        from ...telemetry.registry import TELEMETRY_PROJECT_KEY, TELEMETRY_REQUEST_URL
        data = request.get_json(silent=True) or {}
        request_type = data.get("request_type")
        if request_type not in ("delete", "export"):
            return jsonify({"ok": False, "error": "request_type must be 'delete' or 'export'"}), 400
        payload = {
            "request_type": request_type,
            "install_id": _tel.get_install_id(),
            "submitted_username": str(data.get("username", "")).strip()[:80],
            "submitted_email": str(data.get("email", "")).strip()[:255],
        }
        try:
            resp = GLOBAL_SESSION.post(
                TELEMETRY_REQUEST_URL, json=payload,
                headers={"X-Project-Key": TELEMETRY_PROJECT_KEY}, timeout=8,
            )
            if resp.status_code >= 400:
                return jsonify({"ok": False, "error": f"devInfo server returned {resp.status_code}"}), 502
        except Exception as e:
            logger.warning("[Telemetry] request-from-app call failed: %s", e)
            return jsonify({"ok": False, "error": "devInfo server unreachable"}), 502
        return jsonify({"ok": True})

    @app.route("/api/settings/telemetry/request-status", methods=["GET"])
    def api_settings_telemetry_request_status():
        """Serve GET /api/settings/telemetry/request-status: check the
        status of this install's own delete/export requests, so "Meine
        Daten verwalten" can show "in Bearbeitung" / "Löschung
        abgeschlossen" / a download button -- entirely over the
        project-key-secured app channel, no email round-trip
        (TELEMETRY_IMPLEMENTATION_PLAN.md §3.8). On success, forwards the
        devInfo server's response array unchanged (shared contract); the
        dict shape below is only used for this proxy's own failure case."""
        from ...config import GLOBAL_SESSION
        from ...telemetry import settings as _tel
        from ...telemetry.registry import TELEMETRY_PROJECT_KEY, TELEMETRY_REQUEST_STATUS_URL
        try:
            resp = GLOBAL_SESSION.get(
                TELEMETRY_REQUEST_STATUS_URL,
                params={"install_id": _tel.get_install_id()},
                headers={"X-Project-Key": TELEMETRY_PROJECT_KEY}, timeout=8,
            )
            resp.raise_for_status()
            return jsonify(resp.json())
        except Exception as e:
            logger.warning("[Telemetry] request-status fetch failed: %s", e)
            return jsonify({"requests": [], "error": "devInfo server unreachable"}), 502
