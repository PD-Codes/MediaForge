"""SQLite persistence layer for the MediaForge web app.

This package owns the single on-disk SQLite database (``mediaforge.db``) and
every table used by the web UI and its background workers.

Why it is a package
-------------------
It used to be one 6939-line ``db.py``. That file was the slowest thing in the
repository to search, the most likely to produce a merge conflict, and the
one place where a mistake takes the whole app down -- and none of that was
caused by the code being complicated, only by all of it living together.

The split is by domain, and it is a pure move: every function is byte-for-byte
what it was, in a file named after the table family it touches. The
dependency graph between those files is a DAG (checked by
``tests/test_db_package.py``), so the import order below is stable and there
are no lazy imports working around a cycle.

The public API did not change. Everything that was importable as
``from .db import x`` still is, because this module re-exports all of it --
roughly 250 names across 40-odd call sites. Import from ``mediaforge.web.db``,
not from the submodules: which file a function lives in is an internal
detail, and moving one between domains must not be a breaking change.

Each public function opens its own connection via ``get_db()``, does its work,
and closes it again -- see ``_core.get_db()`` for how connection reuse and WAL
mode are handled. Tables are created and migrated lazily by the ``init_*_db()``
functions, called once at app startup (see ``mediaforge/web/app.py``) and safe
to call repeatedly. Schema *changes* are versioned separately, in
``mediaforge/web/dbmigrate.py``.
"""

# Import order follows the dependency graph: _core has no siblings above it,
# and every module below is imported after the ones it uses.
from ._core import (  # noqa: F401
    ContextConnection,
    DB_PATH,
    SENSITIVE_KEYS,
    USER_ROLES,
    _CREATE_SSO_INDEX,
    _CREATE_TABLE,
    _ENC_PREFIX,
    _LOCK_PATH,
    _RUNTIME_SENSITIVE_KEYS,
    _SQL_IN_CHUNK,
    _configure_connection,
    _decrypt_value,
    _encrypt_existing_plaintext,
    _encrypt_value,
    _fernet_instance,
    _get_fernet,
    _instance_lock_fh,
    _migrate_db,
    _migrate_role_check,
    _recover_interrupted_user_rebuild,
    _sql_chunks,
    _table_columns,
    acquire_instance_lock,
    get_db,
    is_sensitive_key,
    register_sensitive_keys,
)
from .ui_prefs import (  # noqa: F401
    PROTECTED_UI_PREF_KEYS,
    USER_UI_PREF_KEYS,
    _CREATE_USER_UI_PREFS_TABLE,
    _HEX_COLOR_RE,
    _HOME_FEED_FILTER_RE,
    _HOME_MODES_RE,
    _HOME_MODE_ID_RE,
    _MEDIAPLAYER_USER_RE,
    _THEME_FOLDER_RE,
    _WRAPPED_PERIOD_RE,
    _valid_theme_pack,
    clear_user_ui_prefs,
    get_user_ui_prefs,
    register_ui_pref_key,
    set_user_ui_prefs,
)
from .settings import (  # noqa: F401
    _SETTING_LISTENERS,
    _migrate_plaintext_admin_password,
    _migrate_sensitive_settings,
    _notify_setting_listeners,
    add_media_ignores,
    add_setting_listener,
    delete_setting,
    delete_settings_by_prefix,
    get_encoding_ffmpeg_opts,
    get_json_setting,
    get_media_ignores,
    get_setting,
    get_setting_int,
    init_app_settings_db,
    init_media_ignored_db,
    remove_media_ignore,
    set_json_setting,
    set_setting,
)
from .users import (  # noqa: F401
    _has_admin_cached,
    create_user,
    delete_user,
    find_or_create_sso_user,
    get_user_by_id,
    get_user_language,
    has_any_admin,
    init_db,
    list_users,
    set_user_language,
    set_user_password,
    update_user_role,
    verify_user,
)
from .queue import (  # noqa: F401
    _CREATE_QUEUE_TABLE,
    add_to_queue,
    cancel_queue_item,
    claim_next_queued,
    clear_captcha_url,
    clear_completed,
    delete_completed_queue_item,
    get_next_queued,
    get_queue,
    get_queue_item,
    get_running,
    init_queue_db,
    is_queue_cancelled,
    is_series_queued_or_running,
    move_queue_item,
    remove_from_queue,
    restart_queue_item_inplace,
    retry_single_episode,
    set_captcha_url,
    set_queue_status,
    update_queue_errors,
    update_queue_progress,
    update_queue_stats,
)
from .paths import (  # noqa: F401
    _CREATE_CUSTOM_PATHS_TABLE,
    add_custom_path,
    get_custom_path_by_id,
    get_custom_paths,
    init_custom_paths_db,
    is_custom_path_in_use,
    remove_custom_path,
    update_custom_path,
)
from .language_groups import (  # noqa: F401
    _CREATE_LANGUAGE_GROUPS_TABLE,
    _row_to_language_group,
    add_language_group,
    count_language_group_users,
    get_language_group,
    get_language_groups,
    init_language_groups_db,
    remove_language_group,
    update_language_group,
)
from .autosync import (  # noqa: F401
    _CREATE_AUTOSYNC_TABLE,
    add_autosync_job,
    find_autosync_by_url,
    get_autosync_job,
    get_autosync_jobs,
    init_autosync_db,
    remove_autosync_job,
    update_autosync_job,
)
from .history import (  # noqa: F401
    HISTORY_SORT_COLUMNS,
    _CREATE_DOWNLOAD_HISTORY_TABLE,
    _history_where,
    add_download_history,
    clear_download_history,
    delete_download_history_entries,
    delete_download_history_entry,
    get_download_history,
    get_download_history_entry,
    get_download_history_facets,
    get_download_history_meta_for_path,
    get_download_history_summary,
    get_download_period_recap,
    init_download_history_db,
    prune_download_history,
)
from .stats import (  # noqa: F401
    STATS_TRENDS_DEFAULT_DAYS,
    STATS_TRENDS_MAX_DAYS,
    _stats_clamp_days,
    get_general_stats,
    get_queue_stats,
    get_stats_trends,
    get_sync_stats,
)
from .favourites import (  # noqa: F401
    _CREATE_FAVOURITES_TABLE,
    add_favourite,
    get_favourites,
    init_favourites_db,
    is_favourite,
    remove_favourite,
    remove_favourites_bulk,
)
from .seerr import (  # noqa: F401
    _CREATE_SEERR_HIDDEN_TABLE,
    get_hidden_seerr_request_ids,
    get_hidden_seerr_requests,
    hide_seerr_request,
    init_seerr_hidden_db,
    unhide_seerr_request,
)
from .library import (  # noqa: F401
    get_all_library_cache,
    get_library_aliases,
    get_library_cache_status,
    init_library_db,
    invalidate_library_cache,
    prune_library_aliases,
    prune_library_cache,
    set_library_aliases,
    set_library_cache,
    set_library_scanning,
)
from .push import (  # noqa: F401
    db_add_push_subscription,
    db_get_push_subscriptions,
    db_remove_push_subscription,
)
from .notifications import (  # noqa: F401
    _CREATE_PUSH_SUBSCRIPTIONS_TABLE,
    _CREATE_USER_NOTIF_PREFS_TABLE,
    get_user_id_by_username,
    get_user_notif_pref,
    get_user_notif_prefs_all,
    init_notification_db,
    set_user_notif_pref,
    set_user_notif_prefs_bulk,
)
from .caches import (  # noqa: F401
    clear_provider_cache,
    clear_tmdb_cache,
    evict_provider_cache,
    evict_tmdb_cache,
    get_provider_cache,
    get_tmdb_cache,
    get_tmdb_cache_bulk,
    init_provider_cache_db,
    init_tmdb_cache_db,
    set_provider_cache,
    set_tmdb_cache,
)
from .calendar import (  # noqa: F401
    _calendar_ep_key,
    delete_calendar_episodes_except,
    get_cached_calendar_media,
    get_calendar_episodes_from_db,
    get_calendar_media_titles,
    init_calendar_db,
    save_calendar_episode,
    save_calendar_episodes,
    save_calendar_media,
)
from .encoding import (  # noqa: F401
    _CREATE_ENCODING_QUEUE_TABLE,
    _busy_paths,
    _row_paths,
    add_to_encoding_queue,
    cancel_encoding_item,
    claim_next_encoding_queued,
    clear_encoding_completed,
    get_encoding_badge_count,
    get_encoding_item,
    get_encoding_queue,
    get_encoding_running,
    get_next_encoding_queued,
    init_encoding_queue_db,
    is_encoding_cancelled,
    move_encoding_queue_item,
    remove_from_encoding_queue,
    reset_running_encoding_items,
    set_encoding_error,
    set_encoding_status,
    update_encoding_progress,
)
from .upscale import (  # noqa: F401
    _CREATE_UPSCALE_QUEUE_TABLE,
    _upscale_files_of,
    add_to_upscale_queue,
    append_download_upscale_file,
    cancel_upscale_item,
    claim_next_upscale_queued,
    clear_upscale_completed,
    finalize_upscale_item,
    get_next_upscale_queued,
    get_queue_badge_info,
    get_upscale_badge_count,
    get_upscale_files,
    get_upscale_item,
    get_upscale_queue,
    get_upscale_running,
    init_upscale_queue_db,
    is_upscale_cancelled,
    move_upscale_queue_item,
    remove_from_upscale_queue,
    reset_running_upscale_items,
    set_upscale_error,
    set_upscale_status,
    update_upscale_progress,
)
from .catalogue_cache import (  # noqa: F401
    catalogue_entries_without_ids,
    catalogue_entry_count,
    catalogue_id_progress,
    catalogue_meta,
    catalogue_sources_for_urls,
    drop_catalogue,
    evict_catalogue_cache,
    find_catalogue_entry,
    init_catalogue_cache_db,
    load_catalogue,
    mark_catalogue_failed,
    save_catalogue,
    set_catalogue_ids,
    set_catalogue_ids_bulk,
)
from .browse_cache import (  # noqa: F401
    clear_mediascan_cache,
    evict_browse_cache,
    get_browse_cache_stale,
    get_mediascan_count,
    get_mediascan_ids,
    get_mediascan_ids_by_type,
    get_mediascan_last_updated,
    get_mediascan_series,
    init_browse_cache_db,
    init_mediascan_db,
    replace_mediascan_cache,
    set_browse_cache,
)
from .misc import (  # noqa: F401
    MAX_BOOKMARKS_PER_BOOK,
    _CREATE_DEVINFO_READ_TABLE,
    _CREATE_DEVINFO_TABLE,
    _CREATE_READING_BOOKMARKS_TABLE,
    _CREATE_READING_PROGRESS_TABLE,
    _CREATE_UPTIME_INDEX,
    _CREATE_UPTIME_TABLE,
    _CREATE_WATCH_PROGRESS_TABLE,
    _normalize_user,
    add_reading_bookmark,
    clear_watch_progress,
    delete_reading_bookmark,
    delete_reading_progress,
    get_devinfo_count,
    get_devinfo_posts,
    get_reading_progress,
    get_reading_progress_bulk,
    get_recent_reading_progress,
    get_recent_watch_progress,
    get_uptime_heartbeats_between,
    get_uptime_range,
    get_uptime_summary,
    get_watch_progress,
    get_watch_progress_bulk,
    init_devinfos_db,
    init_reading_bookmarks_db,
    init_reading_progress_db,
    init_uptime_db,
    init_watch_progress_db,
    list_reading_bookmarks,
    mark_devinfo_read,
    prune_uptime_heartbeats,
    record_uptime_heartbeat,
    replace_devinfo_posts,
    save_reading_progress,
    save_watch_progress,
)
