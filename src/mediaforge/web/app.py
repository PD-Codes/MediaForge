"""Flask application factory for the MediaForge web UI.

Owns app-wide concerns only: Flask/Babel/CSRF/rate-limit setup, auth
wiring, DB initialization, background-worker bootstrap, security
headers, and the final login_required/admin_required wrapping pass
over every registered view. The actual page/API routes live under
web/routes/ (one module per feature) and are wired in via
register_xxx_routes(app) calls near the end of create_app().
"""

import secrets
import threading
import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from flask_babel import Babel

from ..config import LANG_LABELS
from ..logger import get_logger
from . import restart as web_restart
from . import selfupdate
from .db import (
    clear_captcha_url,
    set_captcha_url,
    init_autosync_db,
    init_favourites_db,
    init_seerr_hidden_db,
    init_custom_paths_db,
    init_queue_db,
    init_library_db,
    init_media_ignored_db,
    init_download_history_db,
    init_app_settings_db,
    get_setting,
    set_setting,
    init_tmdb_cache_db,
    evict_tmdb_cache,
    init_provider_cache_db,
    evict_provider_cache,
    init_calendar_db,
    init_browse_cache_db,
    init_notification_db,
    init_upscale_queue_db,
    init_encoding_queue_db,
    init_mediascan_db,
    init_watch_progress_db,
    init_uptime_db,
    init_devinfos_db,
    get_devinfo_posts,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers/workers extracted into dedicated modules.
# create_app / start_web_ui below reference these via these imports.
# (Each feature module below also imports whatever it needs on its own —
# this block only lists what create_app/start_web_ui use directly.)
# ---------------------------------------------------------------------------
from .runtime_state import (
    WORKING_PROVIDERS,
    _active_cancel_events,
    _active_cancel_events_lock,
    _upscale_active_cancel_events,
    _upscale_cancel_lock,
    _load_queue_paused_from_db,
)
from .dns_patch import _apply_dns_patch, _DNS_PRESETS
from .uptime_monitor import _start_uptime_monitor
from .devinfos_monitor import _start_devinfos_poller
from .queue_worker import _ensure_queue_worker
from .mediascan import _start_mediascan_scheduler
from .autosync_worker import _ensure_autosync_worker
from .upscale_worker import _ensure_upscale_worker
from .encoding_worker import _ensure_encoding_worker
from .version_info import _get_display_version, _update_cache
from .pwa_icons import _generate_pwa_icons
from .settings_migration import (
    _migrate_dotenv_to_db,
    _sync_db_settings_to_env,
    _apply_captcha_env,
)
from .tmdb_keywords_sync import _ensure_tmdb_keywords_sync_worker
from .markdown_utils import render_markdown
from ..telemetry.hooks import init_telemetry


def create_app(auth_enabled=True, sso_enabled=False, force_sso=False):
    """Build and configure the Flask app: i18n, auth/session/CSRF, DB init,
    background workers, security headers, and route registration.

    Used by: start_web_ui() below, the sole entry point that constructs
    and serves the app.
    """
    import os

    # Mirror console output into an in-memory buffer for the optional Web
    # Console. Installed as early as possible so log/print output is captured.
    try:
        from .console_capture import install_capture
        install_capture()
    except Exception:
        pass

    _generate_pwa_icons()

    app = Flask(__name__)
    app.config['TEMPLATES_AUTO_RELOAD'] = False
    app.jinja_env.filters["markdown"] = render_markdown

    # ── i18n / Flask-Babel ──────────────────────────────────────────────────
    # Translations are modular: every web/thirdparties/<name>/translations/
    # folder (if present) is merged into the catalog alongside the core one,
    # so an integration can ship its own strings without touching
    # web/translations/ at all. This has to happen *before* init_app() below
    # — that's when Flask-Babel reads BABEL_TRANSLATION_DIRECTORIES.
    from .thirdparties import apply_pending_changes, discover_translation_dirs
    # Anything the module store staged for this start (installs, upgrades,
    # removals) is applied *here*, before the very first read of
    # web/thirdparties/ -- see apply_pending_changes()'s docstring. It has to
    # be before discover_translation_dirs() in particular: Flask-Babel reads
    # BABEL_TRANSLATION_DIRECTORIES once, at init_app() below, so a module
    # installed after this line would come up without its translations.
    apply_pending_changes()
    _core_translations_dir = str((Path(__file__).parent / "translations").resolve())
    _translation_dirs = [_core_translations_dir] + discover_translation_dirs()
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = ";".join(_translation_dirs)

    babel = Babel()

    def get_locale():
        from flask import session as _sess
        # 1. Prefer language stored in session (set after DB lookup or login)
        lang = _sess.get("ui_language")
        if lang in ("en", "de"):
            return lang
        # 2. Fall back to English
        return "en"

    babel.init_app(app, locale_selector=get_locale)
    app_version = _get_display_version()
    import mediaforge.web.runtime_state as _rtstate
    _rtstate.AUTH_ENABLED = auth_enabled

    base_url = os.environ.get("MEDIAFORGE_WEB_BASE_URL", "").strip().rstrip("/")
    if base_url:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc

        # WSGI middleware that overrides scheme/host before Flask sees the request
        _inner_wsgi = app.wsgi_app

        def _proxy_wsgi(environ, start_response):
            environ["wsgi.url_scheme"] = scheme
            if host:
                environ["HTTP_HOST"] = host
            return _inner_wsgi(environ, start_response)

        app.wsgi_app = _proxy_wsgi

    if auth_enabled:
        from .auth import (
            auth_bp,
            get_current_user,
            get_or_create_secret_key,
            init_oidc,
            login_required,
            refresh_session_role,
        )
        from .db import has_any_admin, init_db, init_app_settings_db

        app.secret_key = get_or_create_secret_key()
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        app.config["PERMANENT_SESSION_LIFETIME"] = 86400  # 24 hours

        csrf = CSRFProtect()

        from .auth import limiter as _auth_limiter

        init_db()
        init_app_settings_db()

        # Generate one-time setup token if no admin exists yet
        import secrets as _secrets
        import time as _time
        if not has_any_admin():
            _setup_token = _secrets.token_urlsafe(32)
            _setup_expires = _time.time() + 1800  # 30 minutes
            app.config["SETUP_TOKEN"] = _setup_token
            app.config["SETUP_TOKEN_EXPIRES"] = _setup_expires
            _su_logger = get_logger(__name__)
            _su_logger.warning(
                "\n" + "=" * 72 + "\n"
                "  INITIAL SETUP — No admin account exists yet.\n"
                f"  Setup Token: {_setup_token}\n"
                "  Local Installation: \n"
                "  Open http://localhost:<PORT>/ in your browser and enter the setup token.\n"
                "  Docker Installation: \n"
                "  Open http://<DockerHostIP>:<HostPort>/ in your browser and enter the setup token.\n"
                "  (Alternative: Direct link with ?token=<token>)\n"
                "  Default port is 8080\n"
                "  The token is valid for 30 minutes. Restart the app afterwards.\n"
                + "=" * 72
            )

        # Check HTTPS AFTER init_db() so the DB-stored web_base_url is available as fallback
        from .db import get_setting as _get_setting
        _db_base_url = (_get_setting("web_base_url") or "").strip().rstrip("/")
        _effective_base_url = base_url or _db_base_url
        _https_forced = os.environ.get("MEDIAFORGE_HTTPS", "").lower() in ("1", "true", "yes")
        if _effective_base_url.startswith("https") or _https_forced:
            app.config["SESSION_COOKIE_SECURE"] = True
        else:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Auth is enabled but no HTTPS was detected. Session cookies will NOT be "
                "marked Secure. Set MEDIAFORGE_WEB_BASE_URL to an https:// URL or set "
                "MEDIAFORGE_HTTPS=1 (e.g. when running behind a TLS-terminating reverse proxy)."
            )
        app.register_blueprint(auth_bp)
        app.config["WTF_CSRF_TIME_LIMIT"] = None  # Session lifetime controls expiry
        csrf.init_app(app)
        _auth_limiter.init_app(app)

        @app.errorhandler(CSRFError)
        def handle_csrf_error(e):
            from flask import redirect, render_template, url_for
            # API requests get a JSON error; form submissions go back to login
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "CSRF token missing or expired"}), 400
            return render_template(
                "login.html",
                error="Your session has expired. Please log in again.",
                oidc_enabled=app.config.get("OIDC_ENABLED", False),
                oidc_display_name=app.config.get("OIDC_DISPLAY_NAME", "SSO"),
                force_sso=app.config.get("FORCE_SSO", False),
            ), 400

        if sso_enabled:
            init_oidc(app, force_sso=force_sso)
        else:
            app.config["OIDC_ENABLED"] = False
            app.config["OIDC_DISPLAY_NAME"] = "SSO"
            app.config["OIDC_ADMIN_USER"] = None
            app.config["OIDC_ADMIN_SUBJECT"] = None
            app.config["FORCE_SSO"] = False

        @app.before_request
        def _check_setup():
            if request.endpoint and request.endpoint.startswith("auth."):
                return None
            if request.endpoint == "static":
                return None
            if not app.config.get("FORCE_SSO", False) and not has_any_admin():
                return redirect(url_for("auth.setup"))
            return None

        @app.before_request
        def _refresh_role():
            return refresh_session_role()

        @app.before_request
        def _sync_ui_language():
            """Keep ui_language in session in sync with DB preference."""
            from flask import session as _sess
            uid = _sess.get("user_id")
            if uid and uid > 0:
                if "_lang_synced" not in _sess:
                    from .db import get_user_language as _get_lang
                    _sess["ui_language"] = _get_lang(uid)
                    _sess["_lang_synced"] = True

        @app.context_processor
        def _inject_auth():
            from flask import session as _sess
            from .db import get_setting as _get_setting
            from .thirdparties.registry import (
                resolve_menu_items, resolve_settings_cards, resolve_dynamic_tabs,
                resolve_provider_pill_scripts, resolve_dashboard_widgets, resolve_card,
            )
            return {
                "current_user": get_current_user(),
                "ui_language": _sess.get("ui_language", "en"),
                "auth_enabled": True,
                "oidc_enabled": app.config.get("OIDC_ENABLED", False),
                "oidc_display_name": app.config.get("OIDC_DISPLAY_NAME", "SSO"),
                "force_sso": app.config.get("FORCE_SSO", False),
                "app_version": app_version,
                "update_available": _update_cache["update_available"],
                "cineinfo_advanced_search": _get_setting("cineinfo_advanced_search", "0") == "1",
                "cineinfo_calendar": _get_setting("cineinfo_calendar", "0") == "1",
                "syncplay_enabled": _get_setting("syncplay_enabled", "0") == "1",
                "uptime_enabled": _get_setting("uptime_enabled", "0") == "1",
                # Sidebar entries per category (see web/thirdparties/registry.py's
                # section= param and base.html's per-category loops).
                "discover_menu_items": resolve_menu_items("discover"),
                "management_menu_items": resolve_menu_items("management"),
                "syncplay_menu_items": resolve_menu_items("syncplay"),
                "system_menu_items": resolve_menu_items("system"),
                # Back-compat: Integrations page's "Third Party" tab, unchanged.
                "thirdparty_cards": resolve_settings_cards("integrations", "thirdparty"),
                # Generic hooks any settings template can call directly to pull
                # in cards for one of its own tabs/pills, or to discover which
                # brand-new tabs/pills it needs to render for the rest (see
                # integrations.html / notifications.html).
                "get_settings_cards": resolve_settings_cards,
                "get_dynamic_tabs": resolve_dynamic_tabs,
                # Modulmanager (templates/extensions.html) uses this to
                # reuse _settings_card_macro.html's render_settings_card()
                # for one registered item at a time -- see registry.py's
                # resolve_card().
                "get_thirdparty_card": resolve_card,
                # Rendered as <script> tags in base.html's <head> — see
                # provider_pill_script in registry.py's register_thirdparty().
                "provider_pill_scripts": resolve_provider_pill_scripts(),
                # Rendered on index.html only, but injected globally like
                # everything else here — see dashboard_widget_template in
                # registry.py's register_thirdparty().
                "dashboard_widgets": resolve_dashboard_widgets(),
            }
    else:
        # No-auth mode still needs a secret key for flask.session
        if not app.secret_key:
            app.secret_key = secrets.token_hex(32)

        @app.before_request
        def _set_noauth_session():
            """In no-auth mode expose a virtual admin/user=0 so notification APIs work."""
            from flask import session as _sess
            if not _sess.get("user_id"):
                _sess["user_id"]   = 0
                _sess["user_role"] = "admin"
                _sess["user_name"] = "admin"

        @app.context_processor
        def _inject_no_auth():
            from flask import session as _sess
            from .db import get_setting as _get_setting
            from .thirdparties.registry import (
                resolve_menu_items, resolve_settings_cards, resolve_dynamic_tabs,
                resolve_provider_pill_scripts, resolve_dashboard_widgets, resolve_card,
            )
            return {
                "current_user": None,
                "ui_language": _sess.get("ui_language", "en"),
                "auth_enabled": False,
                "oidc_enabled": False,
                "oidc_display_name": "SSO",
                "force_sso": False,
                "app_version": app_version,
                "update_available": _update_cache["update_available"],
                "cineinfo_advanced_search": _get_setting("cineinfo_advanced_search", "0") == "1",
                "cineinfo_calendar": _get_setting("cineinfo_calendar", "0") == "1",
                "syncplay_enabled": _get_setting("syncplay_enabled", "0") == "1",
                "uptime_enabled": _get_setting("uptime_enabled", "0") == "1",
                "discover_menu_items": resolve_menu_items("discover"),
                "management_menu_items": resolve_menu_items("management"),
                "syncplay_menu_items": resolve_menu_items("syncplay"),
                "system_menu_items": resolve_menu_items("system"),
                "thirdparty_cards": resolve_settings_cards("integrations", "thirdparty"),
                "get_settings_cards": resolve_settings_cards,
                "get_dynamic_tabs": resolve_dynamic_tabs,
                # Modulmanager (templates/extensions.html) uses this to
                # reuse _settings_card_macro.html's render_settings_card()
                # for one registered item at a time -- see registry.py's
                # resolve_card().
                "get_thirdparty_card": resolve_card,
                "provider_pill_scripts": resolve_provider_pill_scripts(),
                "dashboard_widgets": resolve_dashboard_widgets(),
            }

    # Initialize download queue, custom paths and autosync (works with or without auth)
    init_queue_db()
    init_custom_paths_db()
    init_autosync_db()
    init_favourites_db()
    init_seerr_hidden_db()
    init_library_db()
    init_media_ignored_db()
    init_app_settings_db()
    init_download_history_db()
    init_tmdb_cache_db()
    init_provider_cache_db()
    init_calendar_db()

    # Periodically evict expired TMDB / provider cache entries so the tables
    # don't grow unboundedly.
    def _tmdb_cache_eviction_loop():
        import time as _t
        while True:
            _t.sleep(3600)  # run every hour
            try:
                removed = evict_tmdb_cache()
                if removed:
                    get_logger(__name__).debug("[DB] Evicted %d expired TMDB cache entries", removed)
            except Exception as exc:
                get_logger(__name__).warning("[DB] TMDB cache eviction failed: %s", exc)
            try:
                removed = evict_provider_cache()
                if removed:
                    get_logger(__name__).debug("[DB] Evicted %d expired provider cache entries", removed)
            except Exception as exc:
                get_logger(__name__).warning("[DB] Provider cache eviction failed: %s", exc)

    threading.Thread(target=_tmdb_cache_eviction_loop, daemon=True,
                     name="tmdb-cache-evict").start()

    init_browse_cache_db()
    init_notification_db()
    init_upscale_queue_db()
    init_encoding_queue_db()
    init_mediascan_db()
    init_watch_progress_db()
    init_uptime_db()
    _start_uptime_monitor()
    init_devinfos_db()
    _start_devinfos_poller()
    # Telemetry: sys.excepthook + Flask error handler + background sender
    # thread. Consent-gated (see mediaforge/telemetry/settings.py) — safe to
    # always initialize since nothing is ever sent before the user has
    # actively granted consent via the first-run dialog or Settings.
    init_telemetry(app)
    _load_queue_paused_from_db()
    # Start MediaScan 24-h background scheduler
    _start_mediascan_scheduler()

    # Auto-generate external API key on first run
    if not get_setting("external_api_key", ""):
        set_setting("external_api_key", secrets.token_hex(32))

    # Apply saved DNS setting on startup
    _saved_dns_mode   = get_setting("dns_mode", "system")
    _saved_dns_server = get_setting("dns_server", "")
    if _saved_dns_mode == "system":
        _apply_dns_patch(None, mode="system")
    else:
        _server = _DNS_PRESETS.get(_saved_dns_mode) or _saved_dns_server or None
        _apply_dns_patch(_server, mode=_saved_dns_mode)

    # Apply saved movie subfolder setting on startup
    _subfolder_val = get_setting("movie_subfolder") or get_setting("filmpalast_movie_subfolder", "0")
    os.environ["MEDIAFORGE_MOVIE_SUBFOLDER"] = _subfolder_val
    os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = _subfolder_val
    os.environ["MEGAKINO_MOVIE_SUBFOLDER"] = _subfolder_val

    # One-time migration: import .env values into DB (runs only once)
    _migrate_dotenv_to_db()

    # Apply all persistent DB settings to os.environ on startup
    _sync_db_settings_to_env()

    # Persistent captcha browser profile (opt-in) — keeps a warm cf_clearance
    # across solves.  Read at each browser launch, so this also applies live
    # after the setting is toggled in the WebUI.
    if get_setting("browser_persistent_profile", "0") == "1":
        os.environ["MEDIAFORGE_PERSISTENT_PROFILE"] = "1"

    # Apply captcha/browser toggles (ad-blocker, overlay removal, manual solve,
    # visible window, timeout, ...).  DNS routing stays hard-wired, not here.
    _apply_captcha_env()

    from .routes.library import _lib_build_scan_targets, _lib_do_scan
    # Start library file watcher (watchdog-based, event-driven rescans)
    from .library_watcher import get_watcher as _get_lib_watcher
    _lib_watcher = _get_lib_watcher()

    def _lib_watcher_scan_callback(path_key: str):
        """Called by watchdog when files change in a watched folder."""
        import time as _t
        # Find the matching target and rescan only that one
        targets = _lib_build_scan_targets()
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        for (label, cp_id, base_path) in targets:
            pk = "default" if cp_id is None else str(cp_id)
            if pk == path_key:
                _lib_do_scan([(label, cp_id, base_path)], lang_sep)
                break

    def _start_lib_watcher():
        targets = _lib_build_scan_targets()
        _lib_watcher.start(targets, _lib_watcher_scan_callback)
        # Trigger a full scan on startup so the cache is always fresh
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        _lib_do_scan(targets, lang_sep)

    # Defer watcher start slightly so Flask is fully up first
    import threading as _threading
    _threading.Timer(1.5, _start_lib_watcher).start()

    # Wire up captcha hooks
    from ..playwright import captcha as _captcha_mod
    _captcha_mod._on_captcha_start = set_captcha_url
    _captcha_mod._on_captcha_end = clear_captcha_url

    # In debug mode, Flask's reloader runs this in both the parent and child
    # process. Only start workers in the child (actual server) process
    # to avoid duplicate ffmpeg downloads.
    _debug = os.getenv("MEDIAFORGE_DEBUG_MODE", "0") == "1"
    if not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _ensure_queue_worker()
        _ensure_autosync_worker()
        _ensure_upscale_worker()
        _ensure_encoding_worker()
        _ensure_tmdb_keywords_sync_worker()
        # Auto-download mpv.exe on Windows if missing
        try:
            from ..autodeps import ensure_mpv_windows_async
            ensure_mpv_windows_async()
        except Exception:
            pass

    @app.teardown_appcontext
    def _close_db_connection(exception):
        """Close the per-request SQLite connection stashed in flask.g by db.get_db()."""
        from flask import g
        conn = g.pop("db_conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @app.after_request
    def _set_security_headers(response):
        """Add hardening headers (CSP, HSTS, clickjacking, MIME-sniffing) to
        every response, and disable caching for settings/notification/autosync
        API responses so clients never show stale state."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        # Content-Security-Policy — restricts what the browser may load/execute.
        # 'unsafe-inline' for scripts is required by theme-detection snippets in
        # templates; tightening to nonces would need a larger template refactor.
        _csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' blob: https://cdn.jsdelivr.net; "
            "worker-src 'self' blob:; "
            "media-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none';"
            "frame-src 'self' https://www.youtube.com;"
        )
        response.headers.setdefault("Content-Security-Policy", _csp)
        # HSTS — only sent when HTTPS is confirmed (SESSION_COOKIE_SECURE flag set by create_app)
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # Globally disable browser caching for dynamic settings and notification settings APIs
        if request.path.startswith("/api/settings") or request.path.startswith("/api/notif") or request.path.startswith("/api/autosync"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.before_request
    def _enforce_json_content_type():
        """Reject non-JSON POST/PUT/DELETE on API routes to prevent form-based CSRF bypass.

        This is what stands in for the CSRF token on every endpoint the
        exemption pass below strips it from, so it must cover *exactly* that
        set: everything under /api/ (which is what the exemption is keyed on)
        plus the handful of endpoints exempted by name (auth.admin_*), which
        live under /auth/admin/api/ and were previously exempt from both the
        token and this guard.
        """
        if request.method not in ("POST", "PUT", "DELETE"):
            return
        exempt = app.config.get("CSRF_EXEMPT_ENDPOINTS") or frozenset()
        if request.path.startswith("/api/") or request.endpoint in exempt:
            ct = (request.content_type or "").split(";")[0].strip()
            # If a Content-Type header is present at all it must be JSON.
            # Browser form submissions always declare application/x-www-form-urlencoded
            # or multipart/form-data, so this reliably blocks them.
            # Requests with no body and no Content-Type header are allowed through.
            if ct and ct != "application/json":
                return jsonify({"error": "Content-Type must be application/json"}), 415

    @app.route("/sw.js")
    def service_worker():
        import os as _os
        from flask import send_from_directory, make_response
        static_dir = _os.path.join(_os.path.dirname(__file__), "static")
        resp = make_response(send_from_directory(static_dir, "sw.js"))
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/")
    def index():
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        _devinfo_warnings = [
            {**p, "body_html": render_markdown(p.get("body"))}
            for p in get_devinfo_posts()
            if (p.get("type") or "").strip().lower() == "warning"
        ]
        return render_template(
            "index.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
            devinfo_warnings=_devinfo_warnings,
        )


    # NOTE: the section headers that used to live here (Notification settings,
    # Upscale Queue, Crunchyroll/Fernsehserien/Plex integrations, MediaScan,
    # TMDB lookup, Auto-Sync, Download History, Favourites, Stats, External
    # REST API v1, Captcha, Streaming/Transcoder, Watch Progress) marked
    # inline route definitions before the 12k-line app.py was split up; the
    # routes themselves now live in web/routes/*.py and are wired in via the
    # register_xxx_routes(app) calls below.

    from flask import Response as _FlaskResponse  # noqa: F401 (kept for compat; unused since the routes split)

    # SyncPlay — native in-app synchronised playback (own room service). All
    # clients are browsers on THIS instance (phone / tablet / PC); the server
    # is authoritative and guests may join via an invite without a login.
    # Restore saved rooms on startup so people can rejoin them after a restart.
    try:
        import json as _json_boot
        from . import syncplay_rooms as _sp_boot
        for _rn in _json_boot.loads(get_setting("syncplay_rooms", "[]") or "[]"):
            _sp_boot.ensure_room(_rn)
    except Exception:
        pass

    # Stream endpoints SyncPlay guests need for library playback. Exempted from
    # login_required (see _exempt) and gated here: logged-in OR valid sp guest.


    # ---- Register all feature route groups (plain functions, no blueprints) ----
    from .routes.search import register_search_routes
    from .routes.queue import register_queue_routes
    from .routes.push_notifications import register_push_notifications_routes
    from .routes.library import register_library_routes
    from .routes.settings import register_settings_routes
    from .routes.integrations import register_integrations_routes
    from .routes.extensions import register_extensions_routes
    from .routes.syncplay import register_syncplay_routes
    from .routes.uptime import register_uptime_routes
    from .routes.devinfos import register_devinfos_routes
    from .routes.calendar_routes import register_calendar_routes
    from .thirdparties import discover_and_register as _discover_and_register_thirdparties
    from .routes.encoding import register_encoding_routes
    from .routes.upscale import register_upscale_routes
    from .routes.browse import register_browse_routes
    from .routes.update import register_update_routes
    from .routes.seerr import register_seerr_routes
    from .routes.autosync import register_autosync_routes
    from .routes.stats import register_stats_routes
    from .routes.history import register_history_routes
    from .routes.favourites import register_favourites_routes
    from .routes.image_proxy import register_image_proxy_routes
    from .routes.v1_api import register_v1_api_routes
    from .routes.captcha import register_captcha_routes
    from .routes.stream import register_stream_routes
    from .routes.progress import register_progress_routes
    from .routes.direct_link import register_direct_link_routes

    register_search_routes(app)
    register_queue_routes(app)
    register_direct_link_routes(app)
    register_push_notifications_routes(app)
    register_library_routes(app)
    register_settings_routes(app)
    register_integrations_routes(app)
    register_syncplay_routes(app)
    register_uptime_routes(app)
    register_devinfos_routes(app)
    register_calendar_routes(app)
    # Third-party integrations (web/thirdparties/<name>/) are auto-discovered
    # and registered here — see web/thirdparties/__init__.py. Adding a new
    # one means adding a new subfolder, not editing this file.
    _discover_and_register_thirdparties(app)

    # A module uninstalled live (Modulmanager, no restart) has its files deleted
    # and its registry entries dropped, but Flask has no way to *un*register the
    # blueprint it added — those URL rules stay in the map until the process
    # restarts, now pointing at a package that no longer exists. Answer them with
    # a plain 404 rather than letting them blow up in a template loader.
    # See web/thirdparties/__init__.py's uninstall_module_live().
    @app.before_request
    def _block_uninstalled_module_routes():
        from flask import abort, request as _req
        from .thirdparties import uninstalled_blueprints

        if _req.blueprint and _req.blueprint in uninstalled_blueprints():
            abort(404)

    # Reads whatever the discovery pass above just populated in
    # web/thirdparties/registry.py's _MODULES/_ITEMS at *request* time, not
    # at registration time, so placement relative to the discovery call
    # above doesn't actually matter — kept next to it for readability.
    register_extensions_routes(app)
    register_encoding_routes(app)
    register_upscale_routes(app)
    register_browse_routes(app)
    register_update_routes(app)
    register_seerr_routes(app)
    register_autosync_routes(app)
    register_stats_routes(app)
    register_history_routes(app)
    register_favourites_routes(app)
    register_image_proxy_routes(app)
    register_v1_api_routes(app)
    register_captcha_routes(app)
    register_stream_routes(app)
    register_progress_routes(app)

    # ---- Background workers relocated into their feature modules ----
    from .routes.image_proxy import ensure_image_cache_cleanup
    from .routes.browse import ensure_prefetch_worker
    from .routes.update import ensure_update_check_worker, ensure_auto_update_worker
    from .routes.calendar_routes import ensure_calendar_watcher_started
    ensure_image_cache_cleanup()
    ensure_prefetch_worker()
    ensure_update_check_worker()
    ensure_auto_update_worker()
    ensure_calendar_watcher_started()

    if auth_enabled:
        from .auth import admin_required
        from .thirdparties.registry import (
            admin_required_blueprints, admin_required_endpoints, is_admin_view,
        )

        # Blueprint names any thirdparty registered with auth_required="admin"
        # (see register_thirdparty) -- every route under one of these
        # blueprints is wrapped with admin_required below, exactly like the
        # hand-maintained _admin_only set, without needing an entry added
        # here by hand for each one.
        # ...plus the per-route version of the same thing, for a module whose
        # blueprint is NOT uniformly admin-only (any logged-in user may read,
        # only an admin may write). Two ways in, one enforcement point: the
        # endpoints a module named in register_thirdparty(admin_endpoints=...),
        # and the views it decorated with @module_admin_required. Both are
        # resolved inside secure_endpoints() below, because a module registered
        # live adds to both sets after this point.

        # Endpoints that require admin instead of just login
        _admin_only = {
            "settings_page",
            "api_settings",
            "api_settings_update",
            "api_settings_sso_get",
            "api_settings_sso_put",
            "api_settings_env_file_get",
            "api_settings_env_file_delete",
            "api_settings_api_key_get",
            "api_settings_api_key_regenerate",
            "encoding_page",
            "api_encoding_settings_get",
            "api_encoding_settings_post",
            "api_encoding_detect_hw",
            "api_library_delete",
            "api_library_rename",
            "api_library_move",
            "api_library_refresh",
            "api_custom_paths_add",
            "api_custom_paths_delete",
            "api_autosync_create",
            "api_autosync_update",
            "api_autosync_delete",
            "api_autosync_trigger",
            # The Module Manager. Its sidebar link was always admin-only
            # (base.html), but the route itself wasn't -- harmless while the
            # page merely *listed* modules, no longer true now that it hosts
            # the store configuration (which remote MediaForge trusts) and the
            # uninstall buttons. Gate the page, not just the link.
            "extensions_page",
            # Imports and executes arbitrary code found on disk (any new
            # web/thirdparties/<name>/ folder).
            "api_extensions_rescan",
            # Downloads packages from PyPI and makes them importable in this
            # process (into ~/.mediaforge/module_deps/, see thirdparties/deps.py).
            # As privileged as installing a module -- which is exactly the point:
            # a module can't pull code onto the host by being enabled, an admin
            # has to say yes.
            "api_extensions_install_deps",
            "api_extensions_deps",
            # Module store (web/thirdparties/store.py): these decide which
            # remote MediaForge trusts, download code from it, and stage it to
            # be imported into this very process on the next start. Strictly
            # admin, all of them -- including the read-only ones, since the
            # catalog also reveals the configured store URL.
            "api_store_config",
            "api_store_catalog",
            "api_store_install",
            "api_store_uninstall",
            "api_store_pending",
            # Restarting the server is about as privileged as an action gets. Note
            # api_health is deliberately NOT here: it must answer before anyone is
            # logged in, or the restart button could never tell that the new process
            # is up, and a Docker HEALTHCHECK could never see it either.
            "api_store_restart",
            # Telemetry: device-wide consent/data-collection decision, same
            # admin-only tier as SSO/DNS/API-key -- not a per-user preference.
            "api_settings_telemetry_get",
            "api_settings_telemetry_put",
            "api_settings_telemetry_consent",
            "api_settings_telemetry_regenerate_id",
            "api_settings_telemetry_request",
            "api_settings_telemetry_request_status",
        }

        # Wrap all non-auth, non-static view functions with login_required
        # (admin_required for settings endpoints)
        _exempt = {
            "static",
            "auth.login",
            "auth.logout",
            "auth.setup",
            "auth.oidc_login",
            "auth.oidc_callback",
            # Liveness probe. Must answer without a session: it is what the Modulmanager's
            # restart button polls to find out whether the *new* process is up — and after
            # a restart the browser's session cookie is for a server that no longer exists,
            # so requiring a login here would make the button unable to see its own result.
            # It exposes nothing: an "ok" and two booleans.
            "api_health",
            # SyncPlay guest endpoints — gated by room token + enabled flag,
            # so invited guests can watch together without an account.
            "api_syncplay_config",
            "api_syncplay_join",
            "api_syncplay_stream",
            "api_syncplay_control",
            "api_syncplay_report",
            "api_syncplay_ready",
            "api_syncplay_chat",
            "api_syncplay_episode",
            "api_syncplay_leave",
            "api_syncplay_rooms",
            "api_syncplay_snapshot",
            "api_user_language",
            "api_syncplay_kick",
            "api_syncplay_ban",
            "api_syncplay_transfer_host",
            "api_syncplay_close",
            "api_syncplay_host_lock",
            "api_syncplay_max",
            "api_syncplay_password",
            "api_syncplay_away",
            "api_syncplay_typing",
            "api_syncplay_reaction",
            "api_syncplay_track",
            "syncplay_page",
            # Stream endpoints reachable by SyncPlay guests (gated in before_request)
            "api_stream_check",
            "api_stream_start",
            "api_stream_playlist",
            "api_stream_segment",
            "api_stream_status",
            "api_stream_stop",
            "api_stream_active",
            # External REST API — authenticated via API key, not session
            "api_v1_status",
            "api_v1_queue",
            "api_v1_queue_item",
            "api_v1_library",
            "api_v1_library_series",
            "api_v1_library_movies",
            "api_v1_stats",
        }
        # Endpoints that have already been through the pass below. A module
        # installed live (store install, dependency install, Modulmanager
        # "Refresh") adds its blueprint to a *running* app, i.e. after this pass
        # has already run once -- and an endpoint that never went through it is
        # an endpoint with no login check at all. So the pass is a function, it
        # remembers what it has done, and web/thirdparties/ calls it again after
        # every live registration (see _secure_new_endpoints there).
        _secured = set()

        def secure_endpoints():
            """Wrap every not-yet-wrapped view with login_required /
            admin_required, and apply the CSRF exemption to it. Idempotent:
            re-running it only touches endpoints added since the last run, so a
            view is never double-wrapped."""
            admin_module_endpoints = set(admin_required_endpoints())
            admin_blueprints = admin_required_blueprints()
            for endpoint, view in list(app.view_functions.items()):
                if is_admin_view(view):
                    admin_module_endpoints.add(endpoint)

            for endpoint, view_func in list(app.view_functions.items()):
                if endpoint in _secured or endpoint in _exempt:
                    _secured.add(endpoint)
                    continue
                endpoint_blueprint = endpoint.rsplit(".", 1)[0] if "." in endpoint else None
                if (endpoint in _admin_only
                        or endpoint in admin_module_endpoints
                        or (endpoint_blueprint and endpoint_blueprint in admin_blueprints)):
                    app.view_functions[endpoint] = admin_required(view_func)
                else:
                    app.view_functions[endpoint] = login_required(view_func)
                _secured.add(endpoint)

            _apply_csrf_exemptions()

        # Called by web/thirdparties/ after it registers a module on the running
        # app. Stored on the app rather than imported, because the thirdparties
        # package has no business importing create_app's internals -- and an app
        # created without auth simply doesn't have it, which is exactly right:
        # there is nothing to secure.
        app.extensions["mediaforge_secure_endpoints"] = secure_endpoints

        # Exempt JSON API routes from CSRF. What replaces the CSRF token for
        # these is _enforce_json_content_type() below: a route that only ever
        # accepts Content-Type: application/json cannot be driven by a
        # cross-origin HTML form (forms can only send urlencoded/multipart/
        # text-plain), and a cross-origin fetch() with a JSON content type is
        # a preflighted request the browser won't send without CORS approval
        # this app never gives. So the exemption is only sound for endpoints
        # that guard actually applies to -- which is why it is keyed on the
        # *path* being under /api/, not merely on the view function being
        # named api_*.
        #
        # The naming convention stays: an endpoint is exempt when it is named
        # api_* AND every URL rule it owns lives under /api/. Endpoint names
        # are "viewfunc" for routes added directly on the app object, but
        # "blueprintname.viewfunc" for anything registered via a Blueprint
        # (every thirdparties/<name>/routes.py, e.g. mediacalendar's
        # "mediacalendar.api_calendars_create") -- so the api_ prefix check
        # looks at the part after the last dot, or every Blueprint-based
        # integration's write routes would silently 400 with a CSRF error on
        # every POST/PUT/DELETE (its own fetch() calls, like mediacalendar.js's
        # mcApi(), send no CSRF token at all).
        #
        # A module route named api_* but mounted somewhere else (e.g.
        # /mymodule/save) used to be exempted *and* left uncovered by the JSON
        # guard -- i.e. accepting a cross-site form POST with no token at all.
        # It now keeps CSRF protection and says so in the log, so the author
        # sees why their fetch() suddenly needs a token: mount it under /api/,
        # or send the X-CSRFToken header.
        _csrf_exempt_endpoints = set()

        def _apply_csrf_exemptions():
            rules_by_endpoint = {}
            for rule in app.url_map.iter_rules():
                rules_by_endpoint.setdefault(rule.endpoint, []).append(str(rule.rule))

            for endpoint in list(app.view_functions):
                if endpoint in _csrf_exempt_endpoints:
                    continue
                view_name = endpoint.rsplit(".", 1)[-1] if "." in endpoint else endpoint
                rules = rules_by_endpoint.get(endpoint, [])
                under_api = bool(rules) and all(r.startswith("/api/") for r in rules)
                # auth.admin_* are the user-management endpoints under
                # /auth/admin/api/... -- JSON-only in practice, and exempt since
                # before this convention existed. They are added to the exempt
                # set explicitly (not by path), and _enforce_json_content_type()
                # now covers them too, which it previously did not.
                if endpoint.startswith("auth.admin_"):
                    _csrf_exempt_endpoints.add(endpoint)
                elif view_name.startswith("api_"):
                    if under_api:
                        _csrf_exempt_endpoints.add(endpoint)
                    else:
                        logger.warning(
                            "[CSRF] '%s' is named api_* but is mounted outside /api/ (%s) — "
                            "keeping CSRF protection. Mount it under /api/ or send an "
                            "X-CSRFToken header.",
                            endpoint, ", ".join(rules) or "no rule")

            for endpoint in _csrf_exempt_endpoints:
                csrf.exempt(app.view_functions[endpoint])

            # Read back by _enforce_json_content_type() -- the guard has to know
            # which endpoints lost their CSRF token check, since it is the only
            # thing protecting them.
            app.config["CSRF_EXEMPT_ENDPOINTS"] = frozenset(_csrf_exempt_endpoints)

        secure_endpoints()

    # Resolve any update state left behind by the self-update helper.
    try:
        selfupdate.finalize_after_restart()
    except Exception:
        logger.exception("[SelfUpdate] finalize_after_restart failed")


    @app.context_processor
    def override_url_for():
        """Override the `url_for` available in Jinja templates so static asset
        URLs get a `?v=<mtime>` cache-busting query param, forcing browsers to
        fetch new JS/CSS after a deploy without needing manual version bumps."""
        def dated_url_for(endpoint, **values):
            if endpoint == 'static':
                filename = values.get('filename', None)
                if filename:
                    file_path = os.path.join(app.static_folder, filename)
                    if os.path.exists(file_path):
                        values['v'] = int(os.stat(file_path).st_mtime)
            return url_for(endpoint, **values)
        return dict(url_for=dated_url_for)


    return app


def start_web_ui(
    host="127.0.0.1",
    port=8080,
    open_browser=True,
    auth_enabled=True,
    sso_enabled=False,
    force_sso=False,
):
    """Start the Flask web UI server."""
    import os
    import threading
    import webbrowser

    # Allow env var overrides (Docker-friendly)
    force_sso = force_sso or os.getenv("MEDIAFORGE_WEB_FORCE_SSO", "0") == "1"
    sso_enabled = sso_enabled or force_sso or os.getenv("MEDIAFORGE_WEB_SSO", "0") == "1"
    auth_enabled = (
        auth_enabled or force_sso or os.getenv("MEDIAFORGE_WEB_AUTH", "0") == "1"
    )

    if not auth_enabled:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Authentication is DISABLED — all endpoints are accessible without login. "
            "Do not expose this instance to untrusted networks."
        )

    if host not in ("127.0.0.1", "::1", "localhost"):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Web UI bound to %s:%s — accessible from the network. "
            "Ensure authentication is enabled and the /setup endpoint is protected. "
            "For local use only, bind to 127.0.0.1 instead.", host, port
        )

    app = create_app(
        auth_enabled=auth_enabled, sso_enabled=sso_enabled, force_sso=force_sso
    )
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{display_host}:{port}"
    print(f"Starting MediaForge Web UI on {url}")

    debug = os.getenv("MEDIAFORGE_DEBUG_MODE", "0") == "1"

    # In debug mode, Flask's reloader spawns a child process that re-executes
    # this function. Only open the browser in the parent (reloader) process
    # to avoid opening it twice.
    is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if open_browser and not is_reloader_child:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    if debug:
        app.run(host=host, port=port, debug=True)
    else:
        import logging
        import signal
        import time as _time
        # Waitress logs a WARNING every time the task queue depth exceeds its
        # threshold — useful for debugging but noisy in normal operation.
        logging.getLogger("waitress.queue").setLevel(logging.ERROR)

        from waitress.server import create_server

        # Build the server explicitly (instead of waitress.serve) so we keep a
        # handle we can close from a signal handler.  Plain serve() leaves the
        # main thread parked in waitress' socket loop with no SIGINT handler,
        # so on Windows Ctrl+C is effectively ignored — the process keeps
        # running even after a download was aborted in the web UI.
        server = create_server(app, host=host, port=port, threads=16)

        _shutting_down = threading.Event()

        def _graceful_shutdown(signum=None, frame=None):
            # Guard against re-entry (e.g. a second Ctrl+C).
            if _shutting_down.is_set():
                os._exit(0)
            _shutting_down.set()
            print("\nShutting down MediaForge Web UI…")

            # Abort any in-flight downloads / upscales so their ffmpeg (and
            # captcha Chromium) subprocesses are killed instead of orphaned.
            try:
                with _active_cancel_events_lock:
                    for ev in list(_active_cancel_events.values()):
                        ev.set()
            except Exception:
                pass
            try:
                with _upscale_cancel_lock:
                    for ev in list(_upscale_active_cancel_events.values()):
                        ev.set()
            except Exception:
                pass

            # Stop accepting new connections.
            try:
                server.close()
            except Exception:
                pass

            # Give the worker threads a brief moment to kill their subprocesses.
            _time.sleep(1.5)

            # Hard-exit: daemon worker threads and the waitress loop must not
            # keep the process alive after the user pressed Ctrl+C.
            os._exit(0)

        # ---- restart-in-place -------------------------------------------------
        # The Modulmanager's "Restart now" button (see web/restart.py). A module
        # upgrade can only be applied by a process that has not imported the old
        # version yet, so the honest way to finish an upgrade is to stop being this
        # process. Everything before the re-exec is the same shutdown Ctrl+C does —
        # in-flight downloads and upscales are cancelled so their ffmpeg/Chromium
        # children die with us instead of being orphaned onto the new process.
        def _restart_in_place():
            if _shutting_down.is_set():
                return
            _shutting_down.set()
            print("\nRestarting MediaForge Web UI…")

            try:
                with _active_cancel_events_lock:
                    for ev in list(_active_cancel_events.values()):
                        ev.set()
            except Exception:
                pass
            try:
                with _upscale_cancel_lock:
                    for ev in list(_upscale_active_cancel_events.values()):
                        ev.set()
            except Exception:
                pass

            # Free the port before the replacement tries to bind it.
            try:
                server.close()
            except Exception:
                pass
            _time.sleep(1.5)

            web_restart.replace_process()   # does not return

        web_restart.register_restart_handler(_restart_in_place)

        # Signal handlers can only be installed from the main thread; degrade
        # gracefully (rely on the except below) if we are not on it.
        for _sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if _sig is None:
                continue
            try:
                signal.signal(_sig, _graceful_shutdown)
            except (ValueError, AttributeError, OSError):
                   pass

        try:
            server.run()
        except (KeyboardInterrupt, SystemExit):
            _graceful_shutdown()
