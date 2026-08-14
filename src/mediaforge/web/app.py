"""Flask application factory for the MediaForge web UI.

Owns app-wide concerns only: Flask/Babel/CSRF/rate-limit setup, auth
wiring, DB initialization, background-worker bootstrap, security
headers, and the final login_required/admin_required wrapping pass
over every registered view. The actual page/API routes live under
web/routes/ (one module per feature) and are wired in via
register_xxx_routes(app) calls near the end of create_app().
"""

import re
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
    init_language_groups_db,
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
    evict_browse_cache,
    init_calendar_db,
    init_browse_cache_db,
    init_notification_db,
    init_upscale_queue_db,
    init_encoding_queue_db,
    init_catalogue_cache_db,
    init_mediascan_db,
    init_watch_progress_db,
    init_reading_progress_db,
    init_reading_bookmarks_db,
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
from .version_info import is_release_already_installed
from .pwa_icons import _generate_pwa_icons
from .settings_migration import (
    _migrate_dotenv_to_db,
    _sync_db_settings_to_env,
    _apply_captcha_env,
)
from .tmdb_keywords_sync import _ensure_tmdb_keywords_sync_worker
from .markdown_utils import render_markdown
from ..telemetry.hooks import init_telemetry


# The eight "Advanced Settings" design toggles (profile.html -> Appearance,
# stored per account in user_ui_prefs as ui_<name>; base.html paints them as
# body classes). Module level rather than inside create_app() because
# routes/settings.py validates the instance-default list against it -- a
# whitelist that lives inside a factory function is a whitelist nobody else can
# use, and the alternative is a second hand-maintained copy.
#
# This tuple must stay in step with base.html's window._MF_UI_TOGGLES: a name in
# one and not the other is a toggle that silently ignores the instance default.
UI_TOGGLE_KEYS = (
    "ui_glow_effect", "ui_header_color", "ui_header_color_help",
    "ui_skeleton_loader", "ui_choose_border", "ui_active_download_glow",
    "ui_click_effect", "ui_icon_move",
)

# S.TO's language ids, as the series page numbers them. Four routes carried a
# private copy of this literal; it is one dict now because shared_modals.html
# gets it from a context processor (see _inject_shared_modal_context).
STO_LANG_LABELS = {
    "1": "German Dub",
    "2": "English Dub",
    "3": "English Dub (German Sub)",
}


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

    # ── Schema migrations ───────────────────────────────────────────────────
    # This has to run before ANY init_*_db(), and the ordering is not cosmetic.
    # run_pending() decides between "fresh database, run every migration" and
    # "existing database, baseline it" by looking for tables the pre-migration
    # code used to create (app_settings, download_queue). Call it after those
    # functions have run and a brand-new database looks exactly like an old
    # one: every migration gets marked applied without executing, and the
    # tables they create never appear. That is not a startup warning, it is a
    # missing table at the first request that needs it.
    from . import audit as _audit
    from . import dbmigrate as _dbmigrate

    # The audit writer comes first so the migration result itself is auditable.
    _audit.init_audit_db()

    # Subscribe the audit hooks before anything else runs: the settings
    # listener has to be in place before the first set_setting(), or the
    # writes that happen during startup are the ones missing from the log.
    from . import audit_hooks as _audit_hooks
    _audit_hooks.install()
    _audit_hooks.record_lifecycle("app_started", pid=os.getpid())

    _migration_result = _dbmigrate.run_pending()
    if not _migration_result.get("ok"):
        # Deliberately not fatal. A database that failed to migrate is still
        # readable, and refusing to start would leave the user without the UI
        # they need to roll back from -- the pre-migration snapshot named in
        # this message is the way out, and the Operations tab surfaces it.
        logger.error(
            "[Migrate] Schema migration %s failed (%s). Snapshot for rollback: %s",
            _migration_result.get("failed"), _migration_result.get("error"),
            _migration_result.get("snapshot"),
        )
    elif _migration_result.get("applied"):
        _audit.audit("system", "schema_migrated",
                     target="v%s" % _migration_result.get("current"),
                     detail={"applied": _migration_result["applied"],
                             "snapshot": _migration_result.get("snapshot")},
                     severity="notice")

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

    # ---- Theme packs (web/themes.py) ------------------------------------
    # Shared by both context processors below (auth and no-auth). Cheap per
    # request: installed_themes() is cached with a short TTL and invalidated
    # by install/uninstall, so this is a dict lookup, not a disk scan.
    def _resolve_active_theme_pack():
        from . import themes as _themes
        active = _themes.active_theme()
        if not active:
            return None
        return {"id": active["id"], "folder": active["folder"],
                "name": active["name"], "version": active["version"]}

    def _resolve_theme_pack_list():
        from . import themes as _themes
        return [
            {"id": t["id"], "folder": t["folder"], "name": t["name"],
             "version": t["version"], "supports": t["supports"],
             "preview": t["preview"]}
            for t in _themes.installed_themes() if t["valid"]
        ]

    # The current account's saved appearance (theme pack, dark/light, accent).
    # Rendered into base.html's <head> so the very first paint already uses
    # what the user picked -- on any browser or device, not just the one whose
    # localStorage happens to hold it. Values are whitelisted and validated in
    # db.get_user_ui_prefs(), so a template can use them without escaping
    # worries. In no-auth mode session user_id is 0 (the pseudo-user), which
    # makes the same storage act as an instance-wide preference.
    def _resolve_user_ui_prefs():
        from flask import session as _sess
        from .db import get_user_ui_prefs as _get_prefs
        try:
            return _get_prefs(_sess.get("user_id"))
        except Exception:
            return {}

    # The instance defaults for dark/light and the accent colour: what an
    # account that has never picked one is shown. There was no such thing
    # before -- the Design tab's controls were purely per-account, so an admin
    # setting them up "for the instance" changed nothing for anybody else.
    _ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

    def _resolve_appearance_defaults():
        # Imported here rather than closed over. `_get_setting` is bound further
        # down inside a conditional block of create_app(), so whether this
        # function could read a setting at all depended on that block having
        # run -- and when it had not, every read raised NameError straight into
        # the except below and this quietly reported the built-in defaults.
        # That is why an admin could set a default theme or accent and see no
        # effect: the value was stored and then never read back.
        from .db import get_setting as _setting
        try:
            mode = (_setting("default_theme_mode", "dark") or "dark").strip()
            accent = (_setting("default_accent", "") or "").strip()
        except Exception:
            mode, accent = "dark", ""
        # Instance default for the eight design toggles. Until now these had no
        # middle level at all: theme/accent cascaded account -> instance ->
        # built-in, but the toggles went straight from the account to a
        # localStorage mirror. So a brand-new account started with all eight off
        # no matter how the instance was set up, and looked different from every
        # other account on the same install -- which is what "the extra design
        # options are missing on new accounts" actually was.
        #
        # Stored as ONE comma-separated key rather than eight, so "the admin has
        # never configured this" is a value the template can see (key absent ->
        # None) and not something it has to infer from eight zeroes. That
        # distinction is what lets base.html keep the old localStorage fallback
        # for existing installs instead of blanking their look on upgrade.
        try:
            raw = _setting("default_ui_toggles", None)
        except Exception:
            raw = None
        ui_default = None
        if raw is not None:
            picked = {p.strip() for p in str(raw).split(",") if p.strip()}
            ui_default = {key: ("1" if key in picked else "0")
                          for key in UI_TOGGLE_KEYS}
        return {
            "theme_mode": mode if mode in ("dark", "light") else "dark",
            # "" means "the built-in accent" -- base.html already has one and
            # duplicating the literal here is one more place to keep in step.
            "accent": accent if _ACCENT_RE.match(accent) else "",
            # None = never configured, keep the pre-existing behaviour.
            "ui": ui_default,
        }

    if auth_enabled:
        from .auth import (
            auth_bp,
            get_current_user,
            get_or_create_secret_key,
            init_oidc,
            login_required,
            refresh_session_role,
        )
        # NOT init_app_settings_db: it is already imported at module level, and
        # re-importing it *here* makes the name local to create_app() for the
        # whole function -- so the unconditional call further down
        # ("init_app_settings_db()", next to init_queue_db()) raised
        # UnboundLocalError whenever this branch did not run, i.e. every start
        # with auth_enabled=False.
        from .db import has_any_admin, init_db

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

        # Behind a reverse proxy every request arrives from the proxy, so
        # remote_addr is the proxy and the scheme is http even when the client
        # spoke https. That breaks two things: the login rate limit buckets all
        # clients together (one attacker can lock everyone out, and per-attacker
        # limiting does not work), and HTTPS goes undetected.
        #
        # Opt-in, because trusting X-Forwarded-* headers when NOT behind a proxy
        # would let any client forge its own address. Set the variable to the
        # number of proxies in front of MediaForge (usually 1).
        try:
            _trusted_proxies = int(os.environ.get("MEDIAFORGE_TRUSTED_PROXIES", "0"))
        except ValueError:
            _trusted_proxies = 0
        if _trusted_proxies > 0:
            from werkzeug.middleware.proxy_fix import ProxyFix
            app.wsgi_app = ProxyFix(
                app.wsgi_app,
                x_for=_trusted_proxies,
                x_proto=_trusted_proxies,
                x_host=_trusted_proxies,
                x_port=_trusted_proxies,
            )
            get_logger(__name__).info(
                "Trusting X-Forwarded-* from %d proxy hop(s)", _trusted_proxies)

        # Check HTTPS AFTER init_db() so the DB-stored web_base_url is available as fallback
        from .db import get_setting as _get_setting
        _db_base_url = (_get_setting("web_base_url") or "").strip().rstrip("/")
        _effective_base_url = base_url or _db_base_url
        _https_forced = os.environ.get("MEDIAFORGE_HTTPS", "").lower() in ("1", "true", "yes")
        # With MEDIAFORGE_TRUSTED_PROXIES set, a TLS-terminating proxy is
        # detected on its own -- no need to also set MEDIAFORGE_HTTPS.
        _https_forced = _https_forced or _trusted_proxies > 0
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
            # Same reasoning as static: a browse page fires ~40 of these, and
            # redirecting an <img> to the setup page helps nobody.
            if request.endpoint == "api_image_proxy":
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
                resolve_module_settings,
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
                # Whether Seerr is configured (URL + API key both set).
                # Injected here -- not just on the Integrations page -- so
                # base.html can tell queue.js's global Seerr-badge poller
                # whether to run at all; that poller used to fire on every
                # page load for everyone regardless of setup.
                "seerr_configured": bool(
                    _get_setting("seerr_url", "").strip()
                    and _get_setting("seerr_api_key", "").strip()
                ),
                # Theme packs (web/themes.py): the instance default rendered
                # server-side into base.html's <head> (first paint is already
                # themed), plus a light id/name/version list the pre-paint
                # override script and the Design-tab picker are built from.
                "active_theme_pack": _resolve_active_theme_pack(),
                "installed_theme_packs": _resolve_theme_pack_list(),
                # Per-account appearance (theme pack / dark-light / accent),
                # so the look follows the user instead of the browser.
                "user_ui_prefs": _resolve_user_ui_prefs(),
                # What an account that never picked one gets.
                "appearance_defaults": _resolve_appearance_defaults(),
                # Sidebar entries per category (see web/thirdparties/registry.py's
                # section= param and base.html's per-category loops).
                "discover_menu_items": resolve_menu_items("discover"),
                "management_menu_items": resolve_menu_items("management"),
                "syncplay_menu_items": resolve_menu_items("syncplay"),
                "system_menu_items": resolve_menu_items("system"),
                # Menu rework: module sidebar links no longer sit as their own
                # top-level entries. base.html groups them into a collapsible
                # "Module" sub-menu per main category. Discover absorbs the
                # former SyncPlay category's modules (section="syncplay" stays
                # a valid value -- back-compat -- and is merged in here).
                "discover_module_items": resolve_menu_items("discover") + resolve_menu_items("syncplay"),
                "management_module_items": resolve_menu_items("management"),
                "system_module_items": resolve_menu_items("system"),
                # Shelves contributed by modules, rendered inside the Library
                # sub-menu (see base.html). A module that indexes a media kind
                # MediaForge does not ship belongs next to the built-in
                # shelves, not in a "Modules" group three entries away.
                "library_module_items": resolve_menu_items("library"),
                # Module Settings page (under the Module Manager) -- every
                # enabled module's settings card, grouped by the page its card
                # also lives on. Handed over as the *function*, not its result:
                # this context processor runs on every template render, and
                # resolve_module_settings() walks every registered item and
                # reads a setting per item. module_settings.html is the only
                # caller, and it calls it once.
                "get_module_settings": resolve_module_settings,
                # (Module cards used to be pushed onto the Integrations page's
                # "Third Party" tab from here as `thirdparty_cards`. They are
                # configured in Module Manager -> Module Settings now, which is
                # also where a module is installed and removed; see
                # resolve_module_settings() and templates/integrations.html.
                # Dropping it also takes a walk over every registered item plus
                # one DB read each OFF every single template render.)
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
                resolve_module_settings,
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
                # Whether Seerr is configured (URL + API key both set) -- see
                # the matching key in _inject_auth() above.
                "seerr_configured": bool(
                    _get_setting("seerr_url", "").strip()
                    and _get_setting("seerr_api_key", "").strip()
                ),
                # Theme packs (web/themes.py): the instance default rendered
                # server-side into base.html's <head> (first paint is already
                # themed), plus a light id/name/version list the pre-paint
                # override script and the Design-tab picker are built from.
                "active_theme_pack": _resolve_active_theme_pack(),
                "installed_theme_packs": _resolve_theme_pack_list(),
                # Per-account appearance (theme pack / dark-light / accent),
                # so the look follows the user instead of the browser.
                "user_ui_prefs": _resolve_user_ui_prefs(),
                # What an account that never picked one gets.
                "appearance_defaults": _resolve_appearance_defaults(),
                "discover_menu_items": resolve_menu_items("discover"),
                "management_menu_items": resolve_menu_items("management"),
                "syncplay_menu_items": resolve_menu_items("syncplay"),
                "system_menu_items": resolve_menu_items("system"),
                # Menu rework: module sidebar links no longer sit as their own
                # top-level entries. base.html groups them into a collapsible
                # "Module" sub-menu per main category. Discover absorbs the
                # former SyncPlay category's modules (section="syncplay" stays
                # a valid value -- back-compat -- and is merged in here).
                "discover_module_items": resolve_menu_items("discover") + resolve_menu_items("syncplay"),
                "management_module_items": resolve_menu_items("management"),
                "system_module_items": resolve_menu_items("system"),
                # Shelves contributed by modules, rendered inside the Library
                # sub-menu (see base.html). A module that indexes a media kind
                # MediaForge does not ship belongs next to the built-in
                # shelves, not in a "Modules" group three entries away.
                "library_module_items": resolve_menu_items("library"),
                # Module Settings page (under the Module Manager) -- every
                # enabled module's settings card, grouped by the page its card
                # also lives on. Handed over as the *function*, not its result:
                # this context processor runs on every template render, and
                # resolve_module_settings() walks every registered item and
                # reads a setting per item. module_settings.html is the only
                # caller, and it calls it once.
                "get_module_settings": resolve_module_settings,
                # (No `thirdparty_cards` -- module cards live on the Module
                # Settings page now, not on Integrations' Third Party tab.)
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

    # ── Everything templates/shared_modals.html needs, on every page ────────
    # The series/download modal is an include, and it built its language and
    # hoster <option>s straight out of three template variables. Four routes
    # passed them (index, advanced search, seerr, catalogue) and everybody else
    # did not -- so on the calendar page, and on ANY page a module renders, the
    # include produced two empty dropdowns and no error to explain it. That is
    # the "modules show no language or provider" report.
    #
    # A context processor rather than a fifth copy of the kwargs: an include
    # that only works if the view remembered to feed it is an include a module
    # author cannot use, and .examples/thirdparties/README.md tells them to use
    # exactly this one. The routes that already pass these keep working -- view
    # kwargs shadow the context processor, and the values are identical.
    #
    # WORKING_PROVIDERS is read per request on purpose: extractors.register_
    # hoster() mutates it at runtime, so a module that brings its own hoster
    # appears in the dropdown without a restart.
    @app.context_processor
    def _inject_shared_modal_context():
        return {
            "lang_labels": LANG_LABELS,
            "sto_lang_labels": STO_LANG_LABELS,
            "supported_providers": WORKING_PROVIDERS,
        }

    # Initialize download queue, custom paths and autosync (works with or without auth)
    init_queue_db()
    init_custom_paths_db()
    init_language_groups_db()
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

    # Groups/permissions. The migration engine already created these tables on
    # a fresh database (migration 2); this covers the database that was
    # baselined instead, where migration 2 was marked applied without running.
    # Both paths end at the same schema, and the DDL is IF NOT EXISTS.
    from .groups import init_groups_db
    init_groups_db()

    # Periodically evict expired TMDB / provider cache entries so the tables
    # don't grow unboundedly.
    def _tmdb_cache_eviction_loop():
        import time as _t
        import datetime as _dt
        from . import worker_registry as _wr

        _interval = 3600  # run every hour

        def _next_run_iso():
            return (_dt.datetime.now()
                    + _dt.timedelta(seconds=_interval)).isoformat(timespec="seconds")

        def _audit_rows():
            """Row count of the audit log, or None if it cannot be read."""
            try:
                from . import audit as _audit_mod
                return int(_audit_mod.stats().get("total") or 0)
            except Exception:
                return None

        # Report both workers before the first sleep. The loop used to sleep an
        # hour first, so after every restart these two were the only workers
        # with no heartbeat at all for up to an hour -- which read as "broken"
        # when the truth was "waiting". idle(), not done(): nothing has run yet,
        # so last_run must stay empty.
        try:
            _wr.idle("cache_evict", detail="waiting", next_run=_next_run_iso())
            _rows = _audit_rows()
            _wr.idle("audit_prune", detail="waiting", next_run=_next_run_iso(),
                     extra=None if _rows is None else {"entries": _rows})
        except Exception:
            get_logger(__name__).debug("[Workers] Initial eviction heartbeat failed",
                                       exc_info=True)

        while True:
            _t.sleep(_interval)
            _wr.working("cache_evict")
            # Audit retention rides along with the hourly eviction pass rather
            # than getting a thread of its own: both are "delete rows nobody
            # needs any more", both are cheap, and one fewer thread in the web
            # process is one fewer thing to move when the workers get their own.
            try:
                from . import audit as _audit_mod
                _keep = int(get_setting("audit_retention_days", "0") or 0)
                if _keep > 0:
                    _audit_mod.prune(_keep)
                _rows = _audit_rows()
                _wr.done("audit_prune", detail="retention %d day(s)" % _keep,
                         next_run=_next_run_iso(),
                         extra=None if _rows is None else {"entries": _rows})
            except Exception as exc:
                get_logger(__name__).debug("[Audit] Retention prune failed: %s", exc)
                _wr.fail("audit_prune", str(exc))
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
            try:
                removed = evict_browse_cache()
                if removed:
                    get_logger(__name__).debug("[DB] Evicted %d expired browse cache entries", removed)
            except Exception as exc:
                get_logger(__name__).warning("[DB] Browse cache eviction failed: %s", exc)
            try:
                # A module removed while the app was down never got to call
                # unregister_catalogue(), and an orphaned catalogue is ~10k
                # rows nothing will ever read again.
                from ..catalogue import all_catalogues as _all_cat
                from .db import evict_catalogue_cache as _evict_cat
                dropped = _evict_cat(list(_all_cat()))
                if dropped:
                    get_logger(__name__).debug(
                        "[DB] Evicted %d orphaned catalogue(s)", dropped)
            except Exception as exc:
                get_logger(__name__).warning("[DB] Catalogue eviction failed: %s", exc)
            _wr.done("cache_evict", next_run=_next_run_iso())

    threading.Thread(target=_tmdb_cache_eviction_loop, daemon=True,
                     name="tmdb-cache-evict").start()

    init_browse_cache_db()
    init_notification_db()
    init_upscale_queue_db()
    init_encoding_queue_db()
    init_mediascan_db()
    # Catalogue lists live in the DB now, so the Catalogue page answers from
    # disk instead of waiting on two multi-megabyte downloads (see
    # web/catalogue_store.py). init() also schedules the first staleness
    # check, deferred so startup never waits on the network.
    init_catalogue_cache_db()
    init_watch_progress_db()
    init_reading_progress_db()
    init_reading_bookmarks_db()
    init_uptime_db()
    _start_uptime_monitor()
    init_devinfos_db()
    _start_devinfos_poller()
    # Resolves the alternative names each library folder answers to, so
    # "already downloaded" stops depending on which provider you arrived from.
    # Background and incremental (one TMDB lookup per folder, spread over
    # passes, result stored permanently); no-op without a TMDB key. See
    # web/library_aliases.py.
    try:
        from .library_aliases import start_alias_resolver
        start_alias_resolver()
    except Exception:
        logger.exception("[Aliases] Resolver could not be started")
    # Telemetry: sys.excepthook + Flask error handler + background sender
    # thread. Consent-gated (see mediaforge/telemetry/settings.py) — safe to
    # always initialize since nothing is ever sent before the user has
    # actively granted consent via the first-run dialog or Settings.
    init_telemetry(app)
    _load_queue_paused_from_db()
    # Start MediaScan 24-h background scheduler
    _start_mediascan_scheduler()

    # Catalogue store: registers the module-unregister hook and schedules the
    # first staleness check (deferred inside init(), so startup never waits on
    # a source site). Refreshing here rather than on page open is what turns
    # the first Catalogue open of the day into an instant one.
    try:
        from . import catalogue_store as _catalogue_store
        _catalogue_store.init()
    except Exception as _cat_exc:
        get_logger(__name__).warning("[Catalogue] store init failed: %s", _cat_exc)

    # Title -> TMDB/IMDb id backfill. Starts unconditionally and idles itself
    # when no TMDB key is configured, so configuring one later does not need a
    # restart (see web/catalogue_ids.py).
    try:
        from . import catalogue_ids as _catalogue_ids
        _catalogue_ids.start(delay=_catalogue_ids.START_DELAY)
    except Exception as _cid_exc:
        get_logger(__name__).warning("[CatalogueIds] worker start failed: %s", _cid_exc)

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

    from .routes.library import (
        _lib_build_scan_targets, _lib_do_scan, _lib_stale_targets, _lib_start_auto_rescan,
        _lib_watcher_scan_callback,
    )
    # Start library file watcher (watchdog-based, event-driven rescans)
    from .library_watcher import get_watcher as _get_lib_watcher
    _lib_watcher = _get_lib_watcher()


    def _start_lib_watcher():
        """Start the file watcher, scan what is due, then keep it fresh.

        MediaForge used to run an unconditional full scan of every location on
        every start. On a large library that is minutes of disk I/O for a
        result that is almost always identical to the cache.

        Instead, a location is scanned when its cache is missing (fresh
        install, newly added custom path) or older than the configured rescan
        interval -- and a background loop keeps applying that same rule while
        the app runs. Between those, the file watcher picks up changes live.
        The refresh button on the Library page still forces a scan on demand.
        """
        targets = _lib_build_scan_targets()
        _lib_watcher.start(targets, _lib_watcher_scan_callback)
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"

        due = _lib_stale_targets(targets)
        if due:
            logger.info("[LibraryScan] Startup scan for %d location(s) due", len(due))
            _lib_do_scan(due, lang_sep)

        _lib_start_auto_rescan(
            _lib_build_scan_targets,
            lambda: os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1",
        )

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
    # MEDIAFORGE_WORKER_MODE=external hands these to a separate process
    # (web/worker_host.py). Default is unchanged: they run here, as threads.
    # The two processes coordinate purely through the database -- the claim
    # statements were already atomic UPDATE ... WHERE status='queued', so this
    # is a deployment choice rather than a different execution model.
    from .worker_host import workers_run_in_web_process as _workers_here
    _run_workers = _workers_here()
    if not _run_workers:
        logger.info("[Workers] MEDIAFORGE_WORKER_MODE=external -- the queue, encoding, "
                    "upscale, Auto-Sync and TMDB-keyword workers are expected in a "
                    "separate worker host process (python -m mediaforge.web.worker_host)")

    if _run_workers and (not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        _ensure_queue_worker()
        _ensure_autosync_worker()
        _ensure_upscale_worker()
        _ensure_encoding_worker()
        _ensure_tmdb_keywords_sync_worker()
        # The stall watchdog runs in the process that owns the workers, and
        # only there: two processes both deciding to restart the same worker
        # would race, and the loser would restart the winner's fresh thread.
        # In external mode it is started by worker_host.py instead.
        from .worker_watchdog import start as _start_watchdog
        _start_watchdog()

    # Auto-download mpv.exe on Windows if missing. Outside the worker block:
    # mpv belongs to the local player, which is a web-process feature, so
    # moving the workers out must not stop it from being fetched.
    if not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
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
            "script-src 'self' 'unsafe-inline'; "
            # blob: is what the eBook reader needs: epub.js unpacks the book's
            # own stylesheets, fonts and images out of the archive and hands
            # them to the rendered document as blob: URLs. Without these three,
            # an EPUB renders as unstyled text with broken images. It does not
            # widen what a *page* may load -- a blob URL can only be created by
            # script already running on this origin.
            "style-src 'self' 'unsafe-inline' blob: https://fonts.googleapis.com; "
            "font-src 'self' blob: https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' blob:; "
            "worker-src 'self' blob:; "
            "media-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none';"
            "frame-src 'self' blob: https://www.youtube.com;"
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
            # Fetch metadata beats the Content-Type heuristic: browsers set
            # Sec-Fetch-Site themselves and a page cannot forge it, so a
            # cross-site POST is recognisable no matter what body or headers it
            # carries. This closes the hole the Content-Type check leaves open
            # -- a bodiless fetch(url, {method:'POST', mode:'no-cors',
            # credentials:'include'}) sends no Content-Type at all and is not
            # preflighted. SameSite=Lax already keeps the cookie off such a
            # request today; this is the layer that survives SameSite being
            # loosened. Browsers too old to send the header fall through to the
            # Content-Type rule below, exactly as before.
            _fetch_site = request.headers.get("Sec-Fetch-Site", "")
            if _fetch_site and _fetch_site not in ("same-origin", "none"):
                return jsonify({"error": "cross-site request rejected"}), 403
            ct = (request.content_type or "").split(";")[0].strip()
            # If a Content-Type header is present at all it must be JSON.
            # Browser form submissions always declare application/x-www-form-urlencoded
            # or multipart/form-data, so this reliably blocks them.
            # Bodiless requests without a Content-Type are allowed: ~44 of the
            # frontend's own calls (cancel, delete, pause, ...) send exactly
            # that, and Sec-Fetch-Site above is what guards them.
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

    @app.route("/offline")
    def offline_page():
        """The page the service worker shows when a navigation cannot reach us.

        Precached at install time (see static/sw.js's SHELL), so it has to be
        reachable without a session -- a service worker fetching it during
        install carries no cookies worth relying on, and a login redirect
        cached under this URL would be shown as "you are offline" forever.

        It contains no data at all, which is the point: a queue or library
        served from a stale cache looks current and is wrong.
        """
        from flask import session as _session
        return render_template("offline.html",
                               ui_language=_session.get("ui_language", "en"))

    @app.route("/")
    def index():
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        # Which Dev Info post types get a banner at the top of the home page:
        #   warning   -- dismissible banner
        #   important -- the same banner, plus a modal (devinfo_important.js)
        #   release   -- the same banner, showing the announced version and a
        #                shortcut into Settings -> Updates; the changelog itself
        #                stays on the Dev Infos page. Skipped entirely once the
        #                announced version is installed (or on a dev/source
        #                build): a banner offering an update somebody already
        #                has is noise they cannot get rid of, because it
        #                returns on every visit. The post itself stays on the
        #                Dev Infos page -- this only suppresses the banner.
        # Rendered in one pass over the cached feed rather than three, since
        # get_devinfo_posts() hits the DB.
        #
        # _format_devinfo_timestamp is imported inside the view (not at module
        # level) because routes/devinfos.py is registered from this module
        # further down -- a top-level import would be a cycle.
        from .routes.devinfos import _format_devinfo_timestamp

        _devinfo_banners = []
        _devinfo_importants = []
        for _p in get_devinfo_posts():
            _type = (_p.get("type") or "").strip().lower()
            if _type not in ("warning", "important", "release"):
                continue
            if _type == "release" and is_release_already_installed(_p.get("release_tag")):
                continue
            _entry = {
                **_p,
                "type": _type,
                "body_html": render_markdown(_p.get("body")),
                "formatted_time": _format_devinfo_timestamp(_p.get("remote_created_at")),
            }
            _devinfo_banners.append(_entry)
            # Only *unread* important posts interrupt with a modal. Marking the
            # post as read (which confirming the modal does) is the one and only
            # way to stop it -- deliberately not a localStorage dismissal like
            # the banner has, because "important" exists precisely for the
            # things a user must not be able to click away by accident.
            if _type == "important" and not _p.get("is_read"):
                _devinfo_importants.append(_entry)
        # Which home page layout this request gets. Read per request so the
        # switch takes effect on the next load instead of at startup.
        #
        # The account wins over the instance: Settings -> Start Page sets the
        # DEFAULT (and is what a fresh account sees), and a user who tried the
        # other layout for themselves keeps their choice. Same relationship the
        # rows and filters of that page have had since Start Page 2.0 -- before
        # this, one admin looking at the new layout switched it for everyone.
        _prefs = _resolve_user_ui_prefs()
        _default_new_home = (get_setting("new_home_enabled")
                             or os.environ.get("MEDIAFORGE_NEW_HOME_ENABLED", "0")) == "1"
        _own = str(_prefs.get("new_home") or "")
        _new_home = (_own == "1") if _own in ("0", "1") else _default_new_home
        # The banner on the classic page that offers the new one. Shown only
        # where it is actionable and only while it is news: never on the new
        # layout itself, and never again once the user dismissed it or tried
        # the new page at least once (both write new_home_promo_done).
        _show_promo = not _new_home and str(_prefs.get("new_home_promo_done") or "") != "1"
        # Per-account: how the two-tab layout's Dashboard and Discover
        # sections are arranged ("Customise this page" -> "Home tabs" on
        # either tab). See the home_dash_enabled comment in ui_prefs.py for
        # the three values -- dash_enabled is "does Dashboard content render
        # at all" (true for "", "1" and "all"), all_in_one is "no tab pill,
        # both sections stacked on one page" (only "all").
        # Same account-wins-over-instance relationship as the layout above:
        # Settings -> Start Page's new "Home tabs" group sets what a fresh
        # account (or one that never touched "Customise this page") starts
        # with; picking a value there of one's own always overrules it.
        _dash_default = str(get_setting("home_dash_enabled_default") or "")
        _dash_mode = str(_prefs.get("home_dash_enabled") or _dash_default)
        _dash_enabled = _dash_mode != "0"
        _all_in_one = _dash_mode == "all"
        # Which tab opens first -- only read client-side (home_2_1.js), which
        # already falls back through window._USER_PREFS.home_tab first, so
        # only the raw instance default needs to travel with the page.
        _tab_default = str(get_setting("home_tab_default") or "")
        return render_template(
            "index.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
            devinfo_banners=_devinfo_banners,
            devinfo_importants=_devinfo_importants,
            new_home=_new_home,
            show_new_home_promo=_show_promo,
            dash_enabled=_dash_enabled,
            all_in_one=_all_in_one,
            home_tab_default=_tab_default,
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
        # Close rooms nobody is in any more. Without this an abandoned room
        # lived until an explicit close or a restart: it stayed listed in the
        # lobby, kept being written back here on every persist, and kept its
        # name reserved. The callback writes the shortened list back -- a reaped
        # room still in app_settings would be recreated by the loop above on the
        # very next start.
        def _sp_reaped(_names):
            from .db import set_json_setting as _set_json
            _set_json("syncplay_rooms", _sp_boot.all_room_names())

        _sp_boot.start_room_reaper(_sp_reaped)
    except Exception:
        logger.exception("[SyncPlay] Room restore/reaper setup failed")

    # Stream endpoints SyncPlay guests need for library playback. Exempted from
    # login_required (see _exempt) and gated here: logged-in OR valid sp guest.


    # ---- Register all feature route groups (plain functions, no blueprints) ----
    from .routes.search import register_search_routes
    from .routes.queue import register_queue_routes
    from .routes.push_notifications import register_push_notifications_routes
    from .routes.library import register_library_routes
    from .routes.comics import register_comic_routes
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
    from .routes.catalogue import register_catalogue_routes
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
    from .routes.reading import register_reading_routes
    from .routes.direct_link import register_direct_link_routes
    from .routes.backup import register_backup_routes
    from .routes.themes import register_themes_routes
    from .routes.ops import register_ops_routes

    register_search_routes(app)
    register_queue_routes(app)
    register_direct_link_routes(app)
    register_push_notifications_routes(app)
    register_library_routes(app)
    register_comic_routes(app)
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
    register_catalogue_routes(app)
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
    register_reading_routes(app)
    register_backup_routes(app)
    register_themes_routes(app)
    register_ops_routes(app)
    # /profile — the account's own settings. Its own page because /settings is
    # admin-only, so a normal account had no way to reach its own theme,
    # accent colour or media-server profile at all.
    from .routes.profile import register_profile_routes
    register_profile_routes(app)

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
        from .auth import admin_required, adult_required
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
            # The generic module settings API (thirdparties/registry.py).
            # Every module's settings card is read and written through this
            # one pair of routes, so leaving them at login_required meant any
            # logged-in account could read a module's configuration -- and
            # PUT could switch a module on or off, and write its extra
            # settings, which is the same class of decision as installing one
            # (already admin-only via api_store_install). Secrets are masked
            # on the way out, but the enabled flag, every non-secret setting
            # and the "is a token configured" answer were all readable.
            "api_thirdparty_settings_get",
            "api_thirdparty_settings_put",
            # The Integrations page itself: its sidebar link was already
            # admin-only (base.html), the route was not -- same mismatch the
            # Module Manager had.
            "integrations_page",
            # Integrations (routes/integrations.py). None of these had an
            # admin check of their own, and every *_get returned the stored
            # Jellyfin/Plex/TMDB credentials in clear text -- a plain "user"
            # account could read them and repoint the server at a host of its
            # choosing. The availability lookups
            # (api_crunchyroll_availability, api_fernsehserien_availability)
            # and api_mediascan_library stay open: they carry no secrets and
            # are used by the normal search/detail pages.
            "api_settings_crunchyroll_get",
            "api_settings_crunchyroll_put",
            "api_settings_crunchyroll_test",
            "api_settings_crunchyroll_profiles",
            "api_settings_opensubtitles_get",
            "api_settings_opensubtitles_put",
            "api_settings_opensubtitles_test",
            "api_settings_comicvine_get",
            "api_settings_comicvine_put",
            "api_settings_comicvine_test",
            "api_settings_fernsehserien_get",
            "api_settings_fernsehserien_put",
            "api_settings_fernsehserien_test",
            "api_settings_mediaplayer_get",
            "api_settings_mediaplayer_put",
            "api_settings_mediaplayer_test",
            "api_mediaplayer_scan",
            "api_mediaplayer_scan_status",
            "api_mediaplayer_plex_pin_create",
            "api_mediaplayer_plex_pin_poll",
            "api_mediaplayer_plex_libraries",
            "api_settings_mediascan_get",
            "api_settings_mediascan_put",
            "api_mediascan_refresh",
            "api_mediascan_status",
            "api_mediascan_plex_libraries",
            "api_mediascan_debug",
            "api_settings_jellyfin_nfo_get",
            "api_settings_jellyfin_nfo_put",
            # Repoints the whole process's DNS resolver (api_settings_dns) or
            # the Seerr/TMDB endpoints it talks to -- same tier as SSO.
            "api_settings_dns",
            "api_settings_seerr",
            "api_settings_cineinfo_get",
            "api_settings_cineinfo_put",
            # Touches the filesystem (~/.aniworld) and reveals whether it exists.
            "api_settings_legacy_import_status",
            "api_settings_legacy_import_run",
            "api_settings_legacy_import_dismiss",
            # Structurally identical to api_custom_paths_* below, which is
            # admin-only -- the two were simply inconsistent.
            "api_language_groups_add",
            "api_language_groups_update",
            "api_language_groups_delete",
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
            # Replaces files in place (upscaling_replace_original defaults to
            # on), so it belongs in the same tier as library delete/rename/move.
            "api_upscale_add_library",
            # The comic caches (routes/comics.py): one endpoint reports
            # server-wide state (cache sizes, which unpacker this host has),
            # the other deletes every file in one of the two caches. Both are
            # instance-wide, not per-account -- same tier as the rest of the
            # settings page they are shown on.
            "api_comic_cache",
            "api_comic_cache_clear",
            "api_library_delete",
            "api_library_rename",
            "api_library_move",
            "api_library_refresh",
            "api_custom_paths_add",
            "api_custom_paths_update",
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
            # Module Settings page under the Module Manager -- hosts every
            # enabled module's settings card, whichever page it also lives on
            # (resolve_module_settings). Admin-only for the same reason
            # settings_page is -- and now also because it is the one place that
            # shows every module's settings at once, including the Integrations
            # ones that were already admin-gated on their own page.
            "module_settings_page",
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
            # Instance-wide default theme pack — changes what every user sees.
            "api_themes_active",
            # Installs pip packages into this process's import path. As privileged as an
            # action gets — and it is why the endpoint takes a module id, never a package name.
            "api_store_requirements",
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

        # Operations API (routes/ops.py): audit log, groups, snapshots and
        # rollback, worker states, maintenance windows, diagnostics, rules,
        # language profiles. Imported from the module that defines the routes
        # instead of being re-typed here, so a new endpoint cannot end up
        # registered but unprotected -- which is the exact failure mode this
        # hand-maintained set has. /healthz and /readyz are deliberately not
        # in it: they must answer to an unauthenticated monitor, which is why
        # they return booleans and nothing else.
        from .routes.ops import ADMIN_ONLY_OPS_ENDPOINTS
        _admin_only |= set(ADMIN_ONLY_OPS_ENDPOINTS)

        # Used by secure_endpoints() below: every /api/v1/ endpoint
        # authenticates with an API key instead of a session, so none of them
        # may be wrapped in login_required. Imported as the accessor, not as
        # the dict -- see where it is called.
        from .routes.v1_api import v1_endpoint_scopes

        # Published so it can be asserted on (tests/test_admin_gating.py):
        # authorisation lives in this hand-maintained set, not on the routes,
        # so a new endpoint is login-protected but NOT admin-protected unless
        # someone remembers to add it here.
        app.config["ADMIN_ONLY_ENDPOINTS"] = frozenset(_admin_only)

        # Open to an ordinary account, closed to a kids account. Everything
        # already in _admin_only is closed to kids too (kids is not admin), so
        # this set holds only what a NORMAL user may do and a child may not.
        #
        # The age gate proper (web/age_gate.py) filters what is *shown*; this
        # is about what can be *changed*. A child who can install a module or
        # rewrite the source list can undo the filtering, so the two have to
        # exist together -- filtering without this would be a suggestion.
        _kids_blocked = {
            # Module store: installing code is not a child's decision, and a
            # module can register its own sources and pages.
            "extensions_page",
            "api_extensions_rescan",
            "api_extensions_install_deps",
            "api_extensions_deps",
            "module_settings_page",
            # Leaving the mode is the PIN's job; a kids ACCOUNT has no way out
            # at all, so the endpoint is simply not for it.
            "api_home_mode",
            # Auto-Sync queues downloads on a schedule -- the same reason
            # /api/download is refused in routes/queue.py. The read-only
            # listing endpoints are left open: seeing that a job exists is
            # harmless, creating or triggering one is not.
            "autosync_page",
            "api_autosync_create",
            "api_autosync_update",
            "api_autosync_delete",
            "api_autosync_trigger",
            "api_autosync_sync_all",
            # UpTime: the dashboard lists every monitored source by label and
            # renders its URL as a clickable link -- including the adult one,
            # whose whole point is that it is opt-in and age-gated everywhere
            # else in the app. The page had no age gate at all, so a kids
            # account could reach hanime.tv straight off the monitoring page.
            # Both the page and its data endpoint are blocked; the operational
            # information is an operator's concern anyway.
            "uptime_page",
            "api_uptime_status",
            "api_uptime_heartbeats",
        }
        # /api/user/preferences is deliberately NOT here. It is how an account
        # picks its theme, density and row layout, and a child is allowed to
        # do that. The one preference that would matter, home_max_fsk, is in
        # db.PROTECTED_UI_PREF_KEYS and that endpoint refuses it outright --
        # which is a better guarantee than blocking the whole endpoint and
        # hoping nothing else important lands in it later.
        app.config["KIDS_BLOCKED_ENDPOINTS"] = frozenset(_kids_blocked)

        # Wrap all non-auth, non-static view functions with login_required
        # (admin_required for settings endpoints)
        _exempt = {
            "static",
            # Theme pack stylesheets/assets — same standing as /static: the
            # login page is themed through base.html too, and a CSS request
            # must never bounce into a login redirect. Themes are validated
            # CSS/fonts/images only (web/themes.py), nothing sensitive.
            "theme_bundle_css",
            "theme_asset",
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
            # Container/orchestrator probes (routes/ops.py). Same reasoning as
            # api_health, different audience: a Docker HEALTHCHECK, a k8s probe
            # or an external uptime monitor has no session and never will. Both
            # return a single status string and nothing else -- no version, no
            # worker names, no error text -- so an unauthenticated caller
            # learns only whether the process is up.
            "healthz",
            "readyz",
            # The OpenAPI document for the external API. A client cannot know
            # which scopes to ask for until it can read the spec, so requiring
            # a key to fetch it is a chicken-and-egg problem. It describes
            # shapes, not data: endpoint names, parameter types and the scope
            # each one needs -- nothing an unauthenticated caller could not
            # read in the public documentation.
            "api_v1_openapi",
            # The PWA's offline page. Precached by the service worker at
            # install time, when the fetch carries no session worth relying
            # on -- and a login redirect cached under this URL would be shown
            # as "you are offline" forever. It contains no data at all.
            "offline_page",
            # SyncPlay guest endpoints — gated by room token + enabled flag,
            # so invited guests can watch together without an account.
            "api_syncplay_config",
            "api_syncplay_join",
            "api_syncplay_stream",
            "api_syncplay_control",
            "api_syncplay_report",
            "api_syncplay_ready",
            "api_syncplay_chat",
            # Reactions ride the same guest path as chat: a guest in a room
            # is a participant, and a room where only account holders may
            # react is not the feature.
            "api_syncplay_react",
            # Following an invite link is the one thing a person without an
            # account MUST be able to do before joining. It answers with a
            # room name and nothing else -- no member list, no media, no
            # indication of whether anything is playing.
            "api_syncplay_invite_resolve",
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
            # NOTE: the external REST API (/api/v1/) is NOT listed here. It is
            # added inside secure_endpoints() from v1_endpoint_scopes(), on
            # every run -- see the comment there for why a set literal built
            # once at startup was wrong.
            #
            # Server-side image proxy. Authenticated inside the view, which
            # accepts EITHER a session or an API key: it is the only way a
            # non-browser client (or a module serving its own listing) can
            # render a poster without every caller inventing its own image
            # endpoint and its own allowlist. See routes/image_proxy.py.
            "api_image_proxy",
            # Calendar ICS subscription feed — authenticated by a per-user
            # token in the query string, not by session. A calendar client
            # (Google/Apple/Thunderbird/DAVx5) sends no cookies, so a login
            # check here would make the feed unsubscribable. The token check
            # in routes/calendar_routes.py's api_calendar_ics() is the gate;
            # only the .ics route is exempt, the two token-management routes
            # next to it (api_calendar_feed / _regenerate) stay session-gated.
            "api_calendar_ics",
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

            # Re-read on every run rather than baked into _exempt once at
            # startup. A module installed live registers its /api/v1/ routes
            # AND its scopes after create_app() has already built that set, so
            # a snapshot made the same module behave two different ways: after
            # a restart its routes were exempt, after a hot install they were
            # wrapped in login_required and answered 401 to a perfectly valid
            # API key. That divergence is what modules were papering over with
            # their own before_request hooks.
            exempt = _exempt | set(v1_endpoint_scopes())

            for endpoint, view_func in list(app.view_functions.items()):
                if endpoint in _secured:
                    continue
                endpoint_blueprint = endpoint.rsplit(".", 1)[0] if "." in endpoint else None
                is_admin = (endpoint in _admin_only
                            or endpoint in admin_module_endpoints
                            or (endpoint_blueprint and endpoint_blueprint in admin_blueprints))
                # Admin wins over the exemption, deliberately and in this
                # order. The other way round -- the way this used to read --
                # meant an entry in the v1 scope map ALSO switched off
                # admin_required for that endpoint, so a route could be
                # un-admined by naming it somewhere else entirely. An
                # authentication exemption must never be able to grant an
                # authorisation it was not asked about.
                if is_admin and endpoint in exempt:
                    logger.warning(
                        "[Auth] '%s' is both admin-only and login-exempt; "
                        "admin_required wins. An /api/v1/ route must not live "
                        "in an admin blueprint -- it authenticates by API key, "
                        "which carries no session and therefore no role.",
                        endpoint)
                if not is_admin and endpoint in exempt:
                    # Not added to _secured: the exemption can go away (a
                    # module and its scope entries are removed on uninstall),
                    # and an endpoint remembered as "done" would then keep an
                    # exemption nothing declares any more. Wrapping is what is
                    # remembered here, and this endpoint was not wrapped.
                    continue
                if is_admin:
                    app.view_functions[endpoint] = admin_required(view_func)
                elif endpoint in _kids_blocked:
                    # Open to an ordinary account, closed to a kids account.
                    # Wrapped in login_required as well, because adult_required
                    # only judges the role -- it is not an authentication check.
                    app.view_functions[endpoint] = login_required(adult_required(view_func))
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


    # Static assets carry ?v=<mtime> (see override_url_for below), so a new
    # build produces new URLs and a long max-age is safe. Without it Flask
    # sends no max-age at all and every page load revalidates all 34 CSS/JS
    # files -- 34 conditional requests, each going through the whole
    # before-request chain, just to be told "not modified".
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 31536000)  # 1 year

    @app.context_processor
    def inject_library_media_kinds():
        """Make the media-kind registry available to every template.

        The sidebar lives in base.html, which every page extends, so its
        library sub-menu cannot be fed from one route's context. Injecting the
        registry here keeps the navigation and the hub reading the same list --
        the alternative, spelling the five kinds out in base.html, is exactly
        how a new media type ends up shipped everywhere except the sidebar.
        """
        from .media_kinds import MEDIA_KINDS
        return dict(library_media_kinds=MEDIA_KINDS)

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
    
    is_docker = os.path.exists("/.dockerenv") or os.environ.get("MEDIAFORGE_DOCKER") == "1"
    
    from .db import get_setting
    if not is_docker:
        tray_mode = get_setting("tray_mode", "0") == "1"
        open_browser_db = get_setting("open_browser_on_startup", "1") == "1"
    else:
        tray_mode = False
        open_browser_db = False
        
    if not open_browser_db:
        open_browser = False

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

            # Silence waitress's own death rattle. Closing the listening socket from this
            # thread makes the worker threads still servicing other requests fail their next
            # trigger pull — on Windows that surfaces as a stack of
            # "OSError [WinError 10038] ... not a socket" logged by waitress itself.
            try:
                logging.getLogger("waitress").setLevel(logging.CRITICAL)
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

            # Silence waitress's own death rattle. Closing the listening socket from this
            # thread makes the worker threads still servicing other requests fail their next
            # trigger pull — on Windows that surfaces as a stack of
            # "OSError [WinError 10038] ... not a socket" logged by waitress itself. During a
            # deliberate restart those are expected and meaningless; muting the logger keeps
            # the console readable instead of alarming.
            try:
                logging.getLogger("waitress").setLevel(logging.CRITICAL)
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

        def _run_server():
            try:
                server.run()
            except (KeyboardInterrupt, SystemExit):
                _graceful_shutdown()

        if tray_mode and not debug:
            try:
                import pystray
                from PIL import Image
                import platform
                
                # Hide console window on Windows when starting in tray mode
                if platform.system() == "Windows":
                    import ctypes
                    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                    if hwnd:
                        ctypes.windll.user32.ShowWindow(hwnd, 0) # SW_HIDE
                
                icon_path = os.path.join(app.root_path, "static", "icon-192.png")
                if not os.path.exists(icon_path):
                    icon_path = os.path.join(app.root_path, "static", "icon.png")
                
                try:
                    image = Image.open(icon_path)
                except Exception:
                    image = Image.new('RGB', (64, 64), color=(73, 109, 137))

                def on_open(icon, item):
                    webbrowser.open(url)

                def on_exit(icon, item):
                    icon.stop()
                    # Show window again on exit before closing
                    if platform.system() == "Windows":
                        import ctypes
                        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                        if hwnd:
                            ctypes.windll.user32.ShowWindow(hwnd, 5) # SW_SHOW
                    _graceful_shutdown()

                menu = pystray.Menu(
                    pystray.MenuItem("MediaForge öffnen", on_open, default=True),
                    pystray.MenuItem("Beenden", on_exit)
                )

                icon = pystray.Icon("MediaForge", image, "MediaForge", menu)

                server_thread = threading.Thread(target=_run_server, daemon=True, name="waitress-server")
                server_thread.start()

                try:
                    icon.run()
                except (KeyboardInterrupt, SystemExit):
                    _graceful_shutdown()
            except ImportError:
                import logging
                logging.getLogger(__name__).warning("pystray or Pillow not installed; tray mode disabled.")
                _run_server()
        else:
            _run_server()
