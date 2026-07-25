"""Settings migration and DB<->env synchronisation helpers.

Bridges three settings sources: legacy ~/.mediaforge/.env files, the
persistent app_settings DB (the source of truth), and os.environ (read by
other modules via os.environ.get("MEDIAFORGE_*")).

The intended lifecycle of a .env file is **import once, then retire**: its
values are copied into app_settings and the file is renamed to
.env.imported so it can never silently override the WebUI again. From then
on os.environ is populated *from* the DB on every start
(_sync_db_settings_to_env), which is what all the os.environ.get(
"MEDIAFORGE_*") call sites throughout the codebase read.

Real environment variables (Docker -e, shell exports) are unaffected and
remain the way to configure the handful of settings that have no WebUI
equivalent -- MEDIAFORGE_REDIS_URL, MEDIAFORGE_HTTPS, MEDIAFORGE_USER_AGENT
and friends. See mediaforge/env.py.

Used by: web/app.py, which calls these in order during startup
(_migrate_dotenv_to_db -> _sync_db_settings_to_env -> _apply_captcha_env),
and web/routes/settings.py, which re-applies _apply_captcha_env after the
relevant settings are saved.
"""

from ..logger import get_logger
from .db import get_setting, set_setting

logger = get_logger(__name__)


# Renamed-to suffix for a .env that has been imported. Its presence is also
# what stops mediaforge/env.py from loading the file again: prepare_env() only
# reads ".env", so once it is gone the file can no longer override the DB.
_RETIRED_SUFFIX = ".imported"


def _parse_dotenv(env_path):
    """Parse a .env file into a dict. Returns None when it cannot be read.

    Deliberately minimal: skips comments and blank lines, handles KEY=VALUE
    and KEY="VALUE". Used by both the import and the retirement report, so
    the two always see exactly the same keys.
    """
    parsed = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                parsed[key] = value
    except OSError as exc:
        logger.warning("env migration: could not read %s: %s", env_path, exc)
        return None
    return parsed


# Map: env var name -> DB setting key. Everything listed here is imported
# once from a legacy .env and is thereafter owned by the DB (and written back
# to os.environ on every start by _sync_db_settings_to_env). Variables that
# are absent on purpose have no WebUI equivalent -- see _retire_dotenv().
_ENV_TO_DB = {
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
    "MEDIAFORGE_DL_QUALITY_UPGRADE":         "dl_quality_upgrade",
    "MEDIAFORGE_DL_AUDIO_TRACK_MERGE":       "dl_audio_track_merge",
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


def _retire_dotenv(env_path, parsed, mapping):
    """Rename an imported .env out of the way and report what was not imported.

    Renaming rather than deleting keeps the user's original values readable,
    and is what makes the retirement stick: mediaforge/env.py only loads a
    file literally named ".env", so after this the DB is the only settings
    source (real environment variables aside).

    Keys the mapping does not cover are, by design, the ones with no WebUI
    equivalent (SYNCPLAY_*, WEB_ADMIN_USER/PASS, INSTALL_FOLDER, VIDEO_CODEC,
    ANISKIP, ...). They were never DB-backed, so they cannot be imported --
    but they *were* being picked up from the file until now, so silently
    dropping them would change behaviour without a trace. They are logged and
    recorded in app_settings under "env_unimported_keys" instead; the fix for
    a user who needs them is to set them as real environment variables.
    """
    leftover = sorted(
        k for k, v in parsed.items()
        if v and k.startswith("MEDIAFORGE_") and k not in mapping
    )
    if leftover:
        set_setting("env_unimported_keys", ",".join(leftover))
        logger.warning(
            "env migration: %d value(s) in %s have no DB equivalent and are no "
            "longer read: %s. Set them as real environment variables if you "
            "still need them.",
            len(leftover), env_path, ", ".join(leftover),
        )

    target = env_path.with_name(env_path.name + _RETIRED_SUFFIX)
    try:
        if target.exists():
            # A second .env appearing after a previous migration (e.g. restored
            # from a backup): keep the older copy, don't clobber it.
            target = env_path.with_name(f"{env_path.name}{_RETIRED_SUFFIX}.{int(env_path.stat().st_mtime)}")
        env_path.rename(target)
        logger.info("env migration: %s retired to %s (settings now live in the database)",
                    env_path, target.name)
    except OSError as exc:
        # A read-only mount or a locked file must not break startup. The values
        # are already in the DB; the file simply keeps being loaded until the
        # user removes it, which is the pre-migration behaviour.
        logger.warning("env migration: could not rename %s (%s) -- it will keep "
                       "being loaded on startup until it is removed", env_path, exc)


def _migrate_dotenv_to_db():
    """One-time migration: read ~/.mediaforge/.env (if it exists), import all
    known variables into the DB, then retire the file.

    Guarded by the 'env_migrated' key in app_settings. The guard alone is not
    enough, though: installs migrated by an earlier version still have their
    .env sitting there being re-loaded on every start, so a leftover file is
    retired even when the import itself is already marked done.

    Used by: web/app.py (startup, before _sync_db_settings_to_env).
    """
    from pathlib import Path
    env_path = Path.home() / ".mediaforge" / ".env"

    if get_setting("env_migrated") == "1":
        # Already imported by a previous run. Older versions left the file in
        # place and kept loading it, which let a stale .env override the WebUI
        # indefinitely -- retire it now.
        if env_path.exists():
            # `or {}` — an unreadable file still gets retired; we just cannot
            # report which of its keys had no DB equivalent.
            _retire_dotenv(env_path, _parse_dotenv(env_path) or {}, _ENV_TO_DB)
        return

    if not env_path.exists():
        # Nothing to import — mark done so we never check again
        set_setting("env_migrated", "1")
        return

    parsed = _parse_dotenv(env_path)
    if parsed is None:
        return  # unreadable — leave everything alone and retry next start

    mapping = _ENV_TO_DB

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
    logger.info("env migration: imported %d setting(s) from %s", imported, env_path)
    _retire_dotenv(env_path, parsed, mapping)


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
        "dl_quality_upgrade":             "MEDIAFORGE_DL_QUALITY_UPGRADE",
        "dl_audio_track_merge":           "MEDIAFORGE_DL_AUDIO_TRACK_MERGE",
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
