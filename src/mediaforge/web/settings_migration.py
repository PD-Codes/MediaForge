"""Settings migration and DB<->env synchronisation helpers.

Bridges three settings sources: legacy ~/.mediaforge/.env files, the
persistent app_settings DB (the source of truth going forward), and
os.environ (read by other modules via os.environ.get("MEDIAFORGE_*")).

Used by: web/app.py, which calls these in order during startup
(_migrate_dotenv_to_db -> _sync_db_settings_to_env -> _apply_captcha_env),
and web/routes/settings.py, which re-applies _apply_captcha_env after the
relevant settings are saved.
"""

from ..logger import get_logger
from .db import get_setting, set_setting

logger = get_logger(__name__)


def _migrate_dotenv_to_db():
    """One-time migration: read ~/.mediaforge/.env (if it exists) and import
    all known variables into the DB.  Runs only once — guarded by the
    'env_migrated' key in app_settings so subsequent starts skip it.

    Used by: web/app.py (startup, before _sync_db_settings_to_env).
    """
    if get_setting("env_migrated") == "1":
        return

    from pathlib import Path
    env_path = Path.home() / ".mediaforge" / ".env"
    if not env_path.exists():
        # Nothing to import — mark done so we never check again
        set_setting("env_migrated", "1")
        return

    # Parse the .env file: skip comments, handle KEY=VALUE and KEY="VALUE"
    parsed = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                parsed[key] = value
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("env migration: could not read %s: %s", env_path, exc)
        return

    # Map: env var name → DB setting key
    mapping = {
        "MEDIAFORGE_DOWNLOAD_PATH":     "download_path",
        "MEDIAFORGE_LANG_SEPARATION":   "lang_separation",
        "MEDIAFORGE_DISABLE_ENGLISH_SUB": "disable_english_sub",
        "MEDIAFORGE_MOVIE_SUBFOLDER":   "movie_subfolder",
        "FILMPALAST_MOVIE_SUBFOLDER":   "filmpalast_movie_subfolder",
        "MEDIAFORGE_LANGUAGE":          "download_language",
        "MEDIAFORGE_PROVIDER":          "download_provider",
        "MEDIAFORGE_NAMING_TEMPLATE":   "naming_template",
        "MEDIAFORGE_SYNC_SCHEDULE":              "sync_schedule",
        "MEDIAFORGE_SYNC_MODE":                  "sync_mode",
        "MEDIAFORGE_SYNC_DAYS":                  "sync_days",
        "MEDIAFORGE_SYNC_TIMES":                 "sync_times",
        "MEDIAFORGE_SYNC_LANGUAGE":              "sync_language",
        "MEDIAFORGE_SYNC_PROVIDER":              "sync_provider",
        "MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION": "sync_path_unavailable_action",
        "MEDIAFORGE_HISTORY_RETENTION_DAYS":     "history_retention_days",
        "MEDIAFORGE_WEB_BASE_URL":      "web_base_url",
        "MEDIAFORGE_DEBUG_MODE":        "debug_mode",
        "MEDIAFORGE_MEDIA_STATS_ENABLED": "media_stats_enabled",
        "MEDIAFORGE_WEB_CONSOLE":       "web_console",
        "MEDIAFORGE_WEB_SSO":           "web_sso",
        "MEDIAFORGE_WEB_FORCE_SSO":     "web_force_sso",
        "MEDIAFORGE_OIDC_ISSUER_URL":   "oidc_issuer_url",
        "MEDIAFORGE_OIDC_CLIENT_ID":    "oidc_client_id",
        "MEDIAFORGE_OIDC_CLIENT_SECRET":"oidc_client_secret",
        "MEDIAFORGE_OIDC_DISPLAY_NAME": "oidc_display_name",
        "MEDIAFORGE_OIDC_ADMIN_USER":   "oidc_admin_user",
        "MEDIAFORGE_OIDC_ADMIN_SUBJECT":"oidc_admin_subject",
    }

    imported = 0
    for env_key, db_key in mapping.items():
        value = parsed.get(env_key, "")
        if not value:
            continue  # not in .env or empty — leave DB default
        # Only import if DB has no value yet (don't overwrite user changes)
        if get_setting(db_key) not in (None, ""):
            continue
        set_setting(db_key, value)
        imported += 1

    set_setting("env_migrated", "1")
    import logging
    logging.getLogger(__name__).info(
        "env migration: imported %d setting(s) from %s", imported, env_path
    )


def _sync_db_settings_to_env():
    """On startup: read all persistent settings from DB and apply to os.environ.
    This means every os.environ.get("MEDIAFORGE_*") call elsewhere in the app
    will automatically pick up DB values without needing individual changes.

    Used by: web/app.py (startup, after _migrate_dotenv_to_db).
    """
    import os
    mapping = {
        "download_path":         "MEDIAFORGE_DOWNLOAD_PATH",
        "lang_separation":       "MEDIAFORGE_LANG_SEPARATION",
        "disable_english_sub":   "MEDIAFORGE_DISABLE_ENGLISH_SUB",
        "download_language":     "MEDIAFORGE_LANGUAGE",
        "download_provider":     "MEDIAFORGE_PROVIDER",
        "naming_template":       "MEDIAFORGE_NAMING_TEMPLATE",
        "download_rate_limit":   "MEDIAFORGE_DOWNLOAD_RATE_LIMIT",
        "download_window_enabled": "MEDIAFORGE_DOWNLOAD_WINDOW_ENABLED",
        "download_window_start":   "MEDIAFORGE_DOWNLOAD_WINDOW_START",
        "download_window_end":     "MEDIAFORGE_DOWNLOAD_WINDOW_END",
        "sync_schedule":                  "MEDIAFORGE_SYNC_SCHEDULE",
        "sync_mode":                      "MEDIAFORGE_SYNC_MODE",
        "sync_days":                      "MEDIAFORGE_SYNC_DAYS",
        "sync_times":                     "MEDIAFORGE_SYNC_TIMES",
        "sync_language":                  "MEDIAFORGE_SYNC_LANGUAGE",
        "sync_provider":                  "MEDIAFORGE_SYNC_PROVIDER",
        "sync_path_unavailable_action":   "MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION",
        "sync_error_retries":             "MEDIAFORGE_SYNC_ERROR_RETRIES",
        "sync_error_retry_time":          "MEDIAFORGE_SYNC_ERROR_RETRY_TIME",
        "sync_adaptive_enabled":          "MEDIAFORGE_SYNC_ADAPTIVE_ENABLED",
        "sync_adaptive_pause_after":      "MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER",
        "sync_adaptive_retry_value":      "MEDIAFORGE_SYNC_ADAPTIVE_RETRY_VALUE",
        "sync_adaptive_retry_unit":       "MEDIAFORGE_SYNC_ADAPTIVE_RETRY_UNIT",
        "history_retention_days":         "MEDIAFORGE_HISTORY_RETENTION_DAYS",
        "web_base_url":          "MEDIAFORGE_WEB_BASE_URL",
        "debug_mode":            "MEDIAFORGE_DEBUG_MODE",
        "media_stats_enabled":   "MEDIAFORGE_MEDIA_STATS_ENABLED",
        "web_console":           "MEDIAFORGE_WEB_CONSOLE",
        "auto_update_enabled":   "MEDIAFORGE_AUTO_UPDATE_ENABLED",
        "auto_update_days":      "MEDIAFORGE_AUTO_UPDATE_DAYS",
        "auto_update_time":      "MEDIAFORGE_AUTO_UPDATE_TIME",
        "oidc_issuer_url":       "MEDIAFORGE_OIDC_ISSUER_URL",
        "oidc_client_id":        "MEDIAFORGE_OIDC_CLIENT_ID",
        "oidc_client_secret":    "MEDIAFORGE_OIDC_CLIENT_SECRET",
        "oidc_display_name":     "MEDIAFORGE_OIDC_DISPLAY_NAME",
        "oidc_admin_user":       "MEDIAFORGE_OIDC_ADMIN_USER",
        "oidc_admin_subject":    "MEDIAFORGE_OIDC_ADMIN_SUBJECT",
        "web_sso":               "MEDIAFORGE_WEB_SSO",
        "web_force_sso":         "MEDIAFORGE_WEB_FORCE_SSO",
    }
    from .language_groups import is_group_ref

    for db_key, env_key in mapping.items():
        val = get_setting(db_key)
        if val is not None and val != "":
            # MEDIAFORGE_LANGUAGE is the language an episode model falls back to
            # when nothing was passed in (CLI runs, mostly). A language fallback
            # group is a web-only concept resolved per episode by the workers, so
            # its "group:<id>" reference must never leak into that default — the
            # UI default stays in the DB and the env keeps the last real label.
            if db_key == "download_language" and is_group_ref(val):
                continue
            os.environ[env_key] = val

    # Ensure all movie subfolder environment variables stay in sync
    subfolder_val = get_setting("movie_subfolder") or get_setting("filmpalast_movie_subfolder")
    if subfolder_val is not None and subfolder_val != "":
        os.environ["MEDIAFORGE_MOVIE_SUBFOLDER"] = subfolder_val
        os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = subfolder_val
        os.environ["MEGAKINO_MOVIE_SUBFOLDER"] = subfolder_val


def _apply_captcha_env():
    """Map the captcha/browser DB settings onto the environment variables read
    by mediaforge.playwright.captcha.  Protective features default to ON, so a
    stored "0" translates into the corresponding MEDIAFORGE_..._NO_/kill-switch.
    Note: DNS routing is intentionally NOT toggleable and has no setting here.

    Used by: web/app.py (startup) and web/routes/settings.py (re-applied
    immediately after captcha-related settings are saved, so changes take
    effect without a restart).
    """
    import os

    def _on(key, default):
        return get_setting(key, default) == "1"

    # Protective features (default ON) — turning them off sets the NO_ kill-switch
    for db_key, no_env in (
        ("captcha_adblock",         "MEDIAFORGE_NO_ADBLOCK"),
        ("captcha_adtab_guard",     "MEDIAFORGE_CAPTCHA_NO_ADTAB_GUARD"),
        ("captcha_overlay_removal", "MEDIAFORGE_CAPTCHA_NO_OVERLAY_REMOVAL"),
        ("captcha_ua_sync",         "MEDIAFORGE_CAPTCHA_NO_UA_SYNC"),
    ):
        if _on(db_key, "1"):
            os.environ.pop(no_env, None)
        else:
            os.environ[no_env] = "1"

    # Opt-in features (default OFF)
    for db_key, env in (
        ("captcha_webgl_spoof", "MEDIAFORGE_SPOOF_WEBGL"),
        ("captcha_manual",      "MEDIAFORGE_CAPTCHA_MANUAL"),
        ("captcha_visible",     "MEDIAFORGE_CAPTCHA_VISIBLE"),
    ):
        if _on(db_key, "0"):
            os.environ[env] = "1"
        else:
            os.environ.pop(env, None)

    # Solve timeout in seconds (empty = code default)
    to = (get_setting("captcha_timeout", "") or "").strip()
    if to.isdigit() and int(to) > 0:
        os.environ["MEDIAFORGE_CAPTCHA_TIMEOUT"] = str(int(to))
    else:
        os.environ.pop("MEDIAFORGE_CAPTCHA_TIMEOUT", None)
