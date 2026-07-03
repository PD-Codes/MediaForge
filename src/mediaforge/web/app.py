import json
import re
import secrets
import struct
import threading

# Registry of active cancel events keyed by queue item ID.
# Allows api_queue_cancel to kill the active subprocess immediately.
_active_cancel_events: dict = {}
_active_cancel_events_lock = threading.Lock()

_calendar_watcher_active = False
_calendar_watcher_scanning = False
_calendar_watcher_last_sync = 0.0
_calendar_watcher_started = False
# Crunchyroll calendar targets are resolved by the background watcher and
# cached here so the /api/calendar request path never calls TMDB/Crunchyroll.
_cr_calendar_ids: list = []
_cr_calendar_meta: dict = {}
_cr_calendar_titles: dict = {}  # normalized CR title -> {in_wl,in_list,lists}
_cr_targets_built_at: float = 0.0
_CR_TARGETS_TTL = 900  # rebuild the CR target list at most every 15 min
_CR_CAL_PAST_DAYS = 60  # CR calendar: only keep episodes from this many days back (trims the huge past tail of a large watchlist; future is always kept)
_CAL_A_BATCH = 25  # calendar watcher: items synced per cycle (throttled by _tmdb_rl)
import time
import zlib
import os
from html import unescape as _html_unescape
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from flask_babel import Babel, gettext as _, lazy_gettext as _l

from ..config import MEDIAFORGE_CONFIG_DIR, INVERSE_LANG_LABELS, LANG_KEY_MAP, LANG_LABELS, SUPPORTED_PROVIDERS, check_redirect_available
from ..extractors import provider_functions
from ..logger import get_logger
from . import selfupdate
from ..providers import resolve_provider
from ..search import (
    fetch_new_animes,
    fetch_new_series,
    fetch_popular_animes,
    fetch_popular_series,
    query_s_to,
    random_anime,
)
from ..search import query as aniworld_query
from .db import (
    add_autosync_job,
    add_custom_path,
    add_to_queue,
    cancel_queue_item,
    clear_captcha_url,
    clear_completed,
    delete_completed_queue_item,
    find_autosync_by_url,
    get_autosync_job,
    get_autosync_jobs,
    get_custom_path_by_id,
    get_custom_paths,
    get_general_stats,
    get_next_queued,
    claim_next_queued,
    get_queue,
    get_queue_item,
    get_queue_stats,
    get_running,
    get_sync_stats,
    init_autosync_db,
    add_favourite,
    remove_favourite,
    get_favourites,
    is_favourite,
    init_favourites_db,
    init_seerr_hidden_db,
    hide_seerr_request,
    unhide_seerr_request,
    get_hidden_seerr_request_ids,
    get_hidden_seerr_requests,
    init_custom_paths_db,
    init_queue_db,
    init_library_db,
    get_all_library_cache,
    set_library_cache,
    set_library_scanning,
    invalidate_library_cache,
    init_media_ignored_db,
    add_media_ignores,
    remove_media_ignore,
    get_media_ignores,
    is_queue_cancelled,
    is_series_queued_or_running,
    claim_next_upscale_queued,
    move_queue_item,
    remove_autosync_job,
    remove_custom_path,
    is_custom_path_in_use,
    remove_from_queue,
    restart_queue_item_inplace,
    retry_single_episode,
    set_captcha_url,
    set_queue_status,
    update_autosync_job,
    update_queue_errors,
    update_queue_progress,
    update_queue_stats,
    init_download_history_db,
    add_download_history,
    get_download_history,
    get_download_history_entry,
    delete_download_history_entry,
    delete_download_history_entries,
    clear_download_history,
    prune_download_history,
    init_app_settings_db,
    get_setting,
    set_setting,
    delete_setting,
    init_tmdb_cache_db,
    get_tmdb_cache,
    get_tmdb_cache_bulk,
    set_tmdb_cache,
    clear_tmdb_cache,
    evict_tmdb_cache,
    init_calendar_db,
    save_calendar_media,
    save_calendar_episode,
    delete_calendar_episodes_except,
    get_cached_calendar_media,
    get_calendar_episodes_from_db,
    get_mediascan_series,
    init_browse_cache_db,
    get_browse_cache_stale,
    set_browse_cache,
    init_notification_db,
    get_user_id_by_username,
    get_user_notif_prefs_all,
    set_user_notif_prefs_bulk,
    db_add_push_subscription,
    db_remove_push_subscription,
    # upscale queue
    add_to_upscale_queue,
    get_upscale_queue,
    get_upscale_item,
    get_next_upscale_queued,
    get_upscale_running,
    set_upscale_status,
    update_upscale_progress,
    set_upscale_error,
    remove_from_upscale_queue,
    cancel_upscale_item,
    is_upscale_cancelled,
    clear_upscale_completed,
    get_upscale_badge_count,
    reset_running_upscale_items,
    init_upscale_queue_db,
    # mediascan
    init_mediascan_db,
    replace_mediascan_cache,
    get_mediascan_ids,
    get_mediascan_count,
    get_mediascan_last_updated,
    clear_mediascan_cache,
    # watch progress
    init_watch_progress_db,
    save_watch_progress,
    get_watch_progress,
    get_watch_progress_bulk,
)

logger = get_logger(__name__)


def _get_working_providers():
    """Return only providers whose extractors are actually implemented.

    Each extractor is probed with an empty URL string.  If it raises
    NotImplementedError the provider is considered not yet implemented and is
    skipped.  Any other exception means the extractor *is* implemented (it just
    rejected the empty URL as expected).  Logging is silenced during the probe
    so that the intentional empty-URL errors don't spam the terminal on startup.
    """
    import logging as _logging
    working = []
    for p in SUPPORTED_PROVIDERS:
        func_name = f"get_direct_link_from_{p.lower()}"
        if func_name not in provider_functions:
            continue
        _logging.disable(_logging.CRITICAL)  # suppress expected empty-URL errors
        try:
            provider_functions[func_name]("")
        except NotImplementedError:
            continue
        except Exception:
            working.append(p)
        finally:
            _logging.disable(_logging.NOTSET)  # restore normal logging
    return tuple(working)


WORKING_PROVIDERS = _get_working_providers()

# Only match series-level links: /anime/stream/<slug> (no season/episode)
_SERIES_LINK_PATTERN = re.compile(r"^/anime/stream/[a-zA-Z0-9\-]+/?$", re.IGNORECASE)

# Only match s.to series-level links: /serie/<slug> (no season/episode)
_STO_SERIES_LINK_PATTERN = re.compile(
    r"^/serie/(stream/)?[a-zA-Z0-9\-]+/?$", re.IGNORECASE
)


# ---- DNS resolver patch ----

import socket as _socket
import ipaddress as _ipaddress

_DNS_PRESETS = {
    "cloudflare": "1.1.1.1",
    "google":     "8.8.8.8",
    "quad9":      "9.9.9.9",
}

# Map preset names to niquests DoH resolver URLs.
# niquests uses these to resolve DNS over HTTPS directly, bypassing the OS
# resolver entirely — which is why socket.getaddrinfo patching alone doesn't
# affect niquests requests.
_DNS_NIQUESTS_MAP = {
    "cloudflare": ["doh+cloudflare://"],
    "google":     ["doh+google://"],
    "quad9":      ["doh://9.9.9.9/dns-query"],
}

_original_getaddrinfo = _socket.getaddrinfo
_active_dns_server: str | None = None


def _apply_dns_patch(server_ip: str | None, mode: str | None = None) -> None:
    """
    Apply DNS routing for the given mode/server_ip.

    Two layers are updated together:
      1. socket.getaddrinfo patch (covers stdlib HTTP, ffmpeg subprocesses, etc.)
      2. GLOBAL_SESSION niquests rebuild (covers all niquests HTTP requests)

    Args:
        server_ip: IP address for the socket patch, or None to restore system DNS.
        mode:      Preset name ("cloudflare", "google", "quad9") used to pick the
                   matching DoH URL for niquests.  For "custom" mode only the socket
                   patch is applied (no DoH URL available).  Pass None or "system"
                   to reset everything to defaults.
    """
    from ..config import rebuild_global_session, set_active_dns_mode

    global _active_dns_server

    if not server_ip:
        # Restore system DNS
        _socket.getaddrinfo = _original_getaddrinfo
        _active_dns_server = None
        rebuild_global_session("system")  # use system DNS resolution
        set_active_dns_mode("system")
        return

    try:
        import dns.resolver as _dns_resolver  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("dnspython not installed — custom DNS not available")
        return

    _active_dns_server = server_ip

    def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        # Don't try to resolve bare IPs or empty strings
        is_ip = False
        if host:
            try:
                _ipaddress.ip_address(host)
                is_ip = True
            except ValueError:
                pass
        if not is_ip and host:
            try:
                res = _dns_resolver.Resolver(configure=False)
                res.nameservers = [server_ip]
                res.timeout = 3
                res.lifetime = 5
                answers = res.resolve(host, "A")
                resolved = str(answers[0])
                return _original_getaddrinfo(resolved, port, family, type, proto, flags)
            except Exception:
                pass  # fall back to system DNS on any error
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _socket.getaddrinfo = _patched_getaddrinfo

    # Also rebuild the niquests GLOBAL_SESSION with the matching DoH resolver.
    # For presets we have a known DoH URL; for "custom" IPs we can't build a
    # DoH URL, so we tell niquests to use system DNS resolution (which uses
    # our patched socket.getaddrinfo).
    doh_urls = _DNS_NIQUESTS_MAP.get(mode) if mode in _DNS_NIQUESTS_MAP else "system"
    rebuild_global_session(doh_urls)
    set_active_dns_mode(mode)


# Queue worker state
_queue_worker_started = False
_queue_lock = threading.Lock()
_dl_lock = threading.Lock()  # guards duplicate-check + add_to_queue

# Global pause flag — when True the worker waits after finishing the current episode.
# Persisted in app_settings DB so it survives restarts.
_queue_paused = False
_queue_pause_lock = threading.Lock()

# Per-job skip-episode flag — worker checks this after each download attempt.
# When set for a job ID, the current episode is silently skipped (no error recorded).
_skip_episode_ids: set = set()
_skip_episode_lock = threading.Lock()



def is_episode_skip_requested(queue_id: int) -> bool:
    with _skip_episode_lock:
        return queue_id in _skip_episode_ids


def request_episode_skip(queue_id: int):
    with _skip_episode_lock:
        _skip_episode_ids.add(queue_id)


def consume_episode_skip(queue_id: int) -> bool:
    """Return True and clear the flag if a skip was requested, else False."""
    with _skip_episode_lock:
        if queue_id in _skip_episode_ids:
            _skip_episode_ids.discard(queue_id)
            return True
        return False


def _load_queue_paused_from_db() -> None:
    """Read persisted pause state from DB into the in-memory flag."""
    global _queue_paused
    try:
        val = get_setting("queue_paused", "0")
        with _queue_pause_lock:
            _queue_paused = val == "1"
    except Exception as e:
        logger.warning("[Queue] Could not load pause state from DB, defaulting to unpaused: %s", e)


def is_queue_paused():
    with _queue_pause_lock:
        return _queue_paused


def set_queue_paused(paused: bool):
    global _queue_paused
    with _queue_pause_lock:
        _queue_paused = paused
    try:
        set_setting("queue_paused", "1" if paused else "0")
    except Exception as e:
        logger.warning("[Queue] Could not persist pause state to DB (in-memory state still applied): %s", e)

# Auto-sync worker state
_autosync_worker_started = False

# Track jobs currently being synced to prevent duplicate runs
_syncing_jobs = set()
_syncing_jobs_lock = threading.Lock()

# Upscale worker state
_upscale_worker_started = False
_upscale_lock = threading.Lock()  # guards worker startup
_upscale_active_cancel_events: dict = {}
_upscale_cancel_lock = threading.Lock()

# Library move job tracking
_move_jobs: dict = {}  # job_id -> {status, copied_bytes, total_bytes, current_file, error}
_move_jobs_lock = threading.Lock()

# Schedule intervals in seconds
SYNC_SCHEDULE_MAP = {
    "1min": 60,
    "30min": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "8h": 28800,
    "12h": 43200,
    "16h": 57600,
    "24h": 86400,
}

# Retry delay map
SYNC_RETRY_MAP = {
    "0min": 0,
    "1min": 60,
    "2min": 120,
    "3min": 180,
    "4min": 240,
    "5min": 300,
}

# Adaptive Auto-Sync: how long without a new episode before a job enters
# "pause mode" (slower re-check cadence). Values in seconds.
SYNC_ADAPTIVE_PAUSE_MAP = {
    "2w": 2 * 7 * 86400,
    "3w": 3 * 7 * 86400,
    "4w": 4 * 7 * 86400,
    "5w": 5 * 7 * 86400,
    "6w": 6 * 7 * 86400,
    "7w": 7 * 7 * 86400,
    "8w": 8 * 7 * 86400,
}

# Adaptive Auto-Sync: seconds per unit for the "retry after" interval while paused.
SYNC_ADAPTIVE_UNIT_MAP = {
    "days": 86400,
    "weeks": 7 * 86400,
    "months": 30 * 86400,
}


def _is_job_adaptive_paused(job) -> bool:
    """Return True if the job is currently in Adaptive Auto-Sync pause mode:
    enabled, not currently retrying after an error, and no new episode found
    for longer than the configured threshold. Mirrors the worker logic in
    `_autosync_worker` so the UI can show a matching status pill."""
    import os
    from datetime import datetime, timedelta

    if not job.get("enabled"):
        return False
    if (get_setting("sync_adaptive_enabled")
            or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_ENABLED", "0")) != "1":
        return False
    # Adaptive pause only applies in interval mode (weekly runs on fixed slots).
    if (os.environ.get("MEDIAFORGE_SYNC_MODE", "interval") or "interval").lower() == "weekly":
        return False
    # Jobs in the error-retry window are handled by that logic, not adaptive pause.
    try:
        max_retries = int(get_setting("sync_error_retries")
                          or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
    except (ValueError, TypeError):
        max_retries = 0
    retry_count = job.get("retry_count", 0) or 0
    if 0 < retry_count <= max_retries:
        return False
    last_new = job.get("last_new_found")
    if not last_new:
        return False
    try:
        ref_dt = datetime.strptime(last_new, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    pause_key = (get_setting("sync_adaptive_pause_after")
                 or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER", "4w"))
    pause_seconds = SYNC_ADAPTIVE_PAUSE_MAP.get(pause_key, 4 * 7 * 86400)
    return datetime.utcnow() >= ref_dt + timedelta(seconds=pause_seconds)



_last_disk_notif_time: "float" = 0.0  # debounce: max once per hour per run
_last_history_prune: "float" = 0.0  # throttle: download-history retention prune (~hourly)


def _check_disk_space_and_notify(username: str | None = None, check_path: str | None = None) -> bool:
    """Check free disk space against the configured minimum.

    Returns True when space is OK, False when below threshold.
    Sends a notification (at most once per hour per process lifetime).
    check_path: path to check (custom download path or default).
    """
    import shutil
    import time as _time

    global _last_disk_notif_time

    raw_gb = None
    try:
        from .db import get_setting as _gs
        raw_gb = _gs("notif_disk_space_min_gb")
    except Exception as e:
        logger.debug("[DiskCheck] Could not read disk space setting: %s", e)
    try:
        min_gb = float(raw_gb) if raw_gb else 5.0
    except (TypeError, ValueError):
        min_gb = 5.0

    if min_gb <= 0:
        return True  # feature disabled

    try:
        dl_path = check_path or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "") or str(Path.home() / "Downloads")
        usage = shutil.disk_usage(dl_path)
        free_gb = usage.free / (1024 ** 3)
    except Exception:
        return True  # can't determine → don't block

    if free_gb >= min_gb:
        return True

    # Only notify once per hour
    now = _time.time()
    if now - _last_disk_notif_time < 3600:
        return False
    _last_disk_notif_time = now

    try:
        from .notifications import notify_all
        notify_all(
            title="⚠️ Speicherplatz niedrig",
            body=f"Nur noch {free_gb:.1f} GB frei (Limit: {min_gb:.0f} GB). Download wird trotzdem gestartet.",
            event="on_disk_space_low",
            username=username,
        )
    except Exception as e:
        logger.warning("[DiskCheck] Notification failed: %s", e)
    return False



def _normalize_media_url(raw: str) -> str:
    """Ensure a media-server URL has an http(s):// scheme."""
    raw = (raw or "").strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _validate_server_url(url: str) -> None:
    """Validate a user-supplied server URL against SSRF risks.
    Raises ValueError with a user-friendly message on violation.
    Private/local IPs are allowed (legitimate for home-server setups);
    cloud metadata endpoints and unroutable addresses are blocked."""
    import ipaddress
    import socket as _socket
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not url:
        return

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL muss mit http:// oder https:// beginnen.")

    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise ValueError("URL muss einen Hostnamen enthalten.")

    # Explicitly blocked hostnames (cloud metadata endpoints)
    _BLOCKED_HOSTS = {
        "169.254.169.254",           # AWS / Azure / GCP instance metadata
        "metadata.google.internal",  # GCP metadata
        "metadata.internal",
    }
    if hostname.lower() in _BLOCKED_HOSTS:
        raise ValueError(f"Host '{hostname}' ist aus Sicherheitsgründen nicht erlaubt.")

    # Blocked IP networks — cloud metadata and unroutable only
    _BLOCKED_NETS = [
        ipaddress.ip_network("169.254.0.0/16"),  # link-local (cloud metadata)
        ipaddress.ip_network("0.0.0.0/8"),        # "this" network
        ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ]

    def _check_addr(addr_str):
        try:
            addr = ipaddress.ip_address(addr_str)
            for net in _BLOCKED_NETS:
                if addr in net:
                    raise ValueError(
                        f"Adresse '{addr_str}' ist aus Sicherheitsgründen nicht erlaubt."
                    )
        except ValueError as exc:
            if "nicht erlaubt" in str(exc):
                raise
            # Not a valid IP literal — ignore

    # Check if hostname is a bare IP
    _check_addr(hostname)

    # Also resolve hostname and check the resolved IP
    try:
        resolved = _socket.gethostbyname(hostname)
        _check_addr(resolved)
    except (_socket.gaierror, OSError):
        pass  # Cannot resolve at config time — allow; validated at connection time

# ─────────────────────────────────────────────────────────────────────────────
#  MediaScan — persistent scan-status (survives page navigation)
# ─────────────────────────────────────────────────────────────────────────────

_mediascan_status: dict = {
    "running":      False,
    "started_at":   None,   # float epoch
    "finished_at":  None,   # float epoch
    "count":        0,
    "total":        0,
    "error":        None,
    "source":       "",     # "plex" | "jellyfin"
}
_mediascan_status_lock = threading.Lock()

# Timer handle for the post-download delayed refresh
_mediascan_delay_timer: threading.Timer | None = None
_mediascan_delay_lock  = threading.Lock()

# 24-h scheduler
_mediascan_scheduler_thread: threading.Thread | None = None
_mediascan_scheduler_stop   = threading.Event()


def _mediascan_get_credentials() -> dict:
    """
    Return credentials for MediaScan from its own settings keys.
    The Plex token (mediaplayer_apikey) is the only shared field — one OAuth
    login works for both MediaPlayer and MediaScan.
    """
    source   = get_setting("mediascan_source", "") or ""
    jf_url   = _normalize_media_url(get_setting("mediascan_jf_url",   "") or "")
    jf_key   = get_setting("mediascan_jf_apikey", "") or ""
    plex_url = _normalize_media_url(get_setting("mediascan_plex_url", "") or "")
    plex_key = get_setting("mediaplayer_apikey", "") or ""  # shared Plex token
    return {
        "svc":      source,
        "jf_url":   jf_url,
        "jf_key":   jf_key,
        "plex_url": plex_url,
        "plex_key": plex_key,
    }


def _mediascan_fetch_jellyfin(jf_url: str, jf_key: str) -> list:
    """Fetch all TV-shows and movies from Jellyfin and return normalised entries."""
    import urllib.request as _req
    import json as _json

    entries = []
    for item_type in ("Series", "Movie"):
        url = (
            f"{jf_url}/Items"
            f"?Recursive=true"
            f"&IncludeItemTypes={item_type}"
            f"&fields=ProviderIds,Name"
            f"&Limit=5000"
            f"&api_key={jf_key}"
        )
        try:
            with _req.urlopen(url, timeout=30) as r:
                data = _json.loads(r.read())
            for item in data.get("Items") or []:
                pids = item.get("ProviderIds") or {}
                entries.append({
                    "tmdb_id":    str(pids.get("Tmdb") or "").strip() or None,
                    "imdb_id":    str(pids.get("Imdb") or "").strip() or None,
                    "tvdb_id":    str(pids.get("Tvdb") or "").strip() or None,
                    "title":      item.get("Name") or "",
                    "media_type": "show" if item_type == "Series" else "movie",
                })
        except Exception as exc:
            logger.warning("[MediaScan] Jellyfin fetch failed for %s: %s", item_type, exc)
    return entries


def _mediascan_parse_plex_guid(guid_str: str) -> tuple:
    """
    Parse a Plex GUID string into (source, id).

    Handles two formats:
    - New Plex agents (Guid array items):  "tmdb://12345", "imdb://tt123", "tvdb://456"
    - Old Plex agents (item.guid field):   "com.plexapp.agents.thetvdb://12345/1/1?lang=en"
                                           "com.plexapp.agents.imdb://tt1234567?lang=en"
                                           "com.plexapp.agents.themoviedb://12345?lang=en"
    Returns ("tmdb"|"imdb"|"tvdb"|"", extracted_id_or_"")
    """
    import re as _re
    g = (guid_str or "").strip()
    # New format
    for prefix, key in (("tmdb://", "tmdb"), ("imdb://", "imdb"), ("tvdb://", "tvdb")):
        if g.startswith(prefix):
            return key, g[len(prefix):]
    # Old agent format  com.plexapp.agents.XYZ://ID/...?...
    m = _re.match(r"com\.plexapp\.agents\.([\w]+)://([^/?]+)", g)
    if m:
        agent = m.group(1).lower()
        raw_id = m.group(2)
        if "themoviedb" in agent or "tmdb" in agent:
            return "tmdb", raw_id
        if "imdb" in agent:
            return "imdb", raw_id
        if "thetvdb" in agent or "tvdb" in agent:
            return "tvdb", raw_id
    return "", ""


def _mediascan_fetch_plex(plex_url: str, plex_key: str) -> list:
    """Fetch all libraries from Plex and return normalised entries."""
    import urllib.request as _req
    import json as _json

    entries = []

    # 1. Get list of sections
    try:
        url = f"{plex_url}/library/sections?X-Plex-Token={plex_key}&Accept=application/json"
        req = _req.Request(url, headers={"Accept": "application/json"})
        with _req.urlopen(req, timeout=15) as r:
            sections_data = _json.loads(r.read())
    except Exception as exc:
        logger.warning("[MediaScan] Plex sections fetch failed: %s", exc)
        return entries

    directories = (
        sections_data.get("MediaContainer", {}).get("Directory") or []
    )

    for section in directories:
        sec_id   = section.get("key", "")
        sec_type = section.get("type", "")   # "show" | "movie"
        if sec_type not in ("show", "movie"):
            continue
        plex_type = 2 if sec_type == "show" else 1
        url = (
            f"{plex_url}/library/sections/{sec_id}/all"
            f"?type={plex_type}"
            f"&includeGuids=1"
            f"&Accept=application/json"
            f"&X-Plex-Token={plex_key}"
        )
        req = _req.Request(url, headers={"Accept": "application/json"})
        try:
            with _req.urlopen(req, timeout=30) as r:
                data = _json.loads(r.read())
        except Exception as exc:
            logger.warning("[MediaScan] Plex section %s fetch failed: %s", sec_id, exc)
            continue

        items = data.get("MediaContainer", {}).get("Metadata") or []
        for item in items:
            tmdb_id = imdb_id = tvdb_id = None

            # New agents: Guid array (list of {id: "tmdb://..."})
            for guid_obj in item.get("Guid") or []:
                key, val = _mediascan_parse_plex_guid(guid_obj.get("id", ""))
                if key == "tmdb" and not tmdb_id:
                    tmdb_id = val
                elif key == "imdb" and not imdb_id:
                    imdb_id = val
                elif key == "tvdb" and not tvdb_id:
                    tvdb_id = val

            # Old agents: single guid string on the item itself
            if not tmdb_id and not imdb_id and not tvdb_id:
                key, val = _mediascan_parse_plex_guid(item.get("guid", ""))
                if key == "tmdb":
                    tmdb_id = val
                elif key == "imdb":
                    imdb_id = val
                elif key == "tvdb":
                    tvdb_id = val

            entries.append({
                "tmdb_id":    tmdb_id,
                "imdb_id":    imdb_id,
                "tvdb_id":    tvdb_id,
                "title":      item.get("title") or "",
                "media_type": sec_type,
            })

    return entries


def _mediascan_resolve_ids(entries: list) -> list:
    """
    Post-fetch resolution pass: for entries missing a tmdb_id, try to find one
    by looking up the imdb_id in the local CineInfo tmdb_cache (free, no API call).
    This helps with Plex anime items that only have TVDB/IMDB GUIDs.
    """
    import json as _json
    country = get_setting("cineinfo_country", "DE") or "DE"
    resolved = improved = 0

    for entry in entries:
        if entry.get("tmdb_id"):
            continue  # already have it
        imdb_id = entry.get("imdb_id")
        if not imdb_id:
            continue
        # Look up  imdb_id|||country  in the CineInfo cache
        cached = get_tmdb_cache(f"{imdb_id}|||{country}")
        if cached and cached.get("found") and cached.get("tmdb_id"):
            entry["tmdb_id"] = str(cached["tmdb_id"])
            improved += 1
        resolved += 1

    if improved:
        logger.info("[MediaScan] Resolved %d/%d IMDB→TMDB IDs from CineInfo cache", improved, resolved)
    return entries


def _run_mediascan(source: str | None = None) -> None:
    """
    Core refresh: fetch library from Plex / Jellyfin and populate mediascan_cache.
    Runs in a background thread. Updates _mediascan_status throughout.
    """
    global _mediascan_status
    import time as _t

    creds = _mediascan_get_credentials()
    # Determine effective source
    if not source:
        source = get_setting("mediascan_source", "") or ""
    if not source:
        source = creds["svc"]  # fall back to whatever mediaplayer has

    with _mediascan_status_lock:
        if _mediascan_status["running"]:
            logger.info("[MediaScan] Scan already running, skipping duplicate request")
            return
        _mediascan_status.update({
            "running":     True,
            "started_at":  _t.time(),
            "finished_at": None,
            "count":       0,
            "total":       0,
            "error":       None,
            "source":      source,
        })

    try:
        logger.info("[MediaScan] Starting library fetch from %s", source)

        if source == "jellyfin":
            if not creds["jf_url"] or not creds["jf_key"]:
                raise ValueError("Jellyfin URL oder API-Key nicht konfiguriert (MediaPlayer-Integration prüfen)")
            entries = _mediascan_fetch_jellyfin(creds["jf_url"], creds["jf_key"])
        elif source == "plex":
            if not creds["plex_url"] or not creds["plex_key"]:
                raise ValueError("Plex URL oder Token nicht konfiguriert (MediaPlayer-Integration prüfen)")
            entries = _mediascan_fetch_plex(creds["plex_url"], creds["plex_key"])
        else:
            raise ValueError(f"Unbekannte MediaScan-Quelle: {source!r}")

        with _mediascan_status_lock:
            _mediascan_status["total"] = len(entries)

        # Resolution pass: fill in missing tmdb_id from local CineInfo cache
        # for items that only have imdb_id. This is cheap (local DB lookup).
        entries = _mediascan_resolve_ids(entries)

        replace_mediascan_cache(entries)

        with _mediascan_status_lock:
            _mediascan_status.update({
                "running":     False,
                "finished_at": _t.time(),
                "count":       len(entries),
                "error":       None,
            })
        logger.info("[MediaScan] Completed — %d entries cached", len(entries))

    except Exception as exc:
        logger.error("[MediaScan] Fetch failed: %s", exc)
        with _mediascan_status_lock:
            _mediascan_status.update({
                "running":     False,
                "finished_at": _t.time(),
                "error":       str(exc),
            })


def _schedule_mediascan_delayed(delay: float = 120.0) -> None:
    """
    Schedule a one-shot mediascan refresh after *delay* seconds.
    If one is already pending, reset the timer.
    """
    global _mediascan_delay_timer
    if get_setting("mediascan_enabled", "0") != "1":
        return
    source = get_setting("mediascan_source", "") or ""
    if source == "folders" or not source:
        return

    def _fire():
        logger.info("[MediaScan] Post-download delayed refresh firing")
        t = threading.Thread(target=_run_mediascan, daemon=True)
        t.start()

    with _mediascan_delay_lock:
        if _mediascan_delay_timer and _mediascan_delay_timer.is_alive():
            _mediascan_delay_timer.cancel()
        _mediascan_delay_timer = threading.Timer(delay, _fire)
        _mediascan_delay_timer.daemon = True
        _mediascan_delay_timer.start()
    logger.info("[MediaScan] Post-download refresh scheduled in %.0fs", delay)


def _start_mediascan_scheduler() -> None:
    """Start a daemon thread that triggers a mediascan every 24 h."""
    global _mediascan_scheduler_thread
    import time as _t

    def _loop():
        # Wait 24 h, then refresh, repeat
        while not _mediascan_scheduler_stop.wait(timeout=86400):
            if get_setting("mediascan_enabled", "0") == "1":
                source = get_setting("mediascan_source", "") or ""
                if source and source != "folders":
                    logger.info("[MediaScan] 24-h scheduled refresh")
                    _run_mediascan()
            _mediascan_scheduler_stop.wait(timeout=1)  # re-check flag quickly

    _mediascan_scheduler_stop.clear()
    _mediascan_scheduler_thread = threading.Thread(target=_loop, name="mediascan-scheduler", daemon=True)
    _mediascan_scheduler_thread.start()
    logger.info("[MediaScan] 24-h scheduler started")


def _trigger_mediaplayer_refresh(title: str | None = None, selected_path: str | None = None) -> None:
    """Trigger a library refresh on Jellyfin or Plex after a successful download."""
    try:
        svc  = get_setting("mediaplayer_type", "")       # "jellyfin" | "plex"
        url  = _normalize_media_url(get_setting("mediaplayer_url",  "") or "")
        key  = get_setting("mediaplayer_apikey", "") or ""
        if not svc or not key:
            return
        # For Plex the URL lives in mediaplayer_plex_url, not mediaplayer_url
        if svc == "plex":
            plex_url_check = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "")
            if not plex_url_check:
                logger.warning("Plex library refresh skipped: keine Plex-URL konfiguriert")
                return
        elif not url:
            logger.warning("Mediaplayer refresh skipped: keine Server-URL konfiguriert")
            return

        import urllib.request as _urllib_req

        if svc == "jellyfin":
            req = _urllib_req.Request(
                f"{url}/Library/Refresh",
                data=b"",
                method="POST",
                headers={
                    "X-Emby-Token": key,
                    "Content-Type": "application/json",
                },
            )
            _urllib_req.urlopen(req, timeout=10)
            logger.info("Jellyfin library refresh triggered")

        elif svc == "plex":
            # Plex: refresh the specific section or the whole library
            plex_url = _normalize_media_url(get_setting("mediaplayer_plex_url", "") or "") or url
            section = (get_setting("mediaplayer_plex_section", "") or "").strip()
            if section:
                req_url = f"{plex_url}/library/sections/{section}/refresh?X-Plex-Token={key}"
            else:
                req_url = f"{plex_url}/library/sections/all/refresh?X-Plex-Token={key}"
            req = _urllib_req.Request(req_url, method="GET")
            _urllib_req.urlopen(req, timeout=10)
            logger.info("Plex library refresh triggered (section=%s)", section or "all")

    except Exception as exc:
        logger.warning("Media-player refresh failed: %s", exc)


def _is_filmpalast_url(url: str) -> bool:
    return "filmpalast.to/stream/" in url


def _parse_season_episode(url):
    """Extract (season, episode) ints from an aniworld/s.to episode URL, else (None, None)."""
    if not url:
        return None, None
    s = re.search(r"staffel-(\d+)", url)
    e = re.search(r"episode-(\d+)", url)
    return (int(s.group(1)) if s else None, int(e.group(1)) if e else None)


def _record_download_history(item, ep_url, start_time, ep_path, size_bytes, status, error=None):
    """Persist a single episode download to the history table. Best-effort."""
    try:
        from datetime import datetime, timezone
        end_time = time.time()
        duration = max(0.0, end_time - start_time)
        size_mb = (size_bytes / (1024 * 1024)) if size_bytes else 0.0
        avg_speed = (size_mb / duration) if (duration > 0 and size_mb > 0) else 0.0
        season, episode = _parse_season_episode(ep_url)

        def _iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        err_text = None
        if error is not None:
            err_text = str(error).strip() or None
            if err_text and len(err_text) > 2000:
                err_text = err_text[:2000] + "…"

        add_download_history(
            item.get("title") or "",
            queue_id=item.get("id"),
            series_url=item.get("series_url"),
            episode_url=ep_url,
            season=season,
            episode=episode,
            language=item.get("language"),
            provider=item.get("provider"),
            source=item.get("source") or "manual",
            username=item.get("username"),
            target_path=ep_path,
            size_mb=round(size_mb, 2) if size_mb else None,
            avg_speed_mbps=round(avg_speed, 2) if avg_speed else None,
            duration_sec=round(duration, 1),
            status=status,
            error=err_text,
            started_at=_iso(start_time),
            finished_at=_iso(end_time),
        )
    except Exception as exc:
        logger.debug("[History] Failed to record download history: %s", exc)


def _is_within_download_window() -> bool:
    """Return True if downloads are allowed to start right now.

    When the download time window is enabled, new downloads are only started
    between the configured start and end time (local wall-clock). Supports
    overnight windows (e.g. 22:00 → 06:00). Already-running downloads are not
    affected — this only gates *starting* new ones.
    """
    import os
    enabled = (get_setting("download_window_enabled")
               or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_ENABLED", "0")) == "1"
    if not enabled:
        return True
    start = (get_setting("download_window_start")
             or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_START", "22:00"))
    end = (get_setting("download_window_end")
           or os.environ.get("MEDIAFORGE_DOWNLOAD_WINDOW_END", "06:00"))
    try:
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
    except (ValueError, AttributeError):
        return True
    from datetime import datetime
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s == e:
        return True  # zero-length window → treat as no restriction
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e  # overnight window wrapping past midnight


def _queue_worker():
    """Single global worker that processes one download at a time."""
    while True:
        item = None
        try:
            # Hold new downloads outside the configured time window. Checked
            # before claiming so items aren't marked running while held.
            if not _is_within_download_window():
                time.sleep(30)
                continue

            _final_status_set = False
            with _queue_lock:
                item = claim_next_queued()

            if not item:
                time.sleep(3)
                continue

            # Don't start a new item while paused
            while is_queue_paused():
                time.sleep(2)

            # Warn if disk space is below configured threshold (non-blocking)
            # Resolve the actual download path for this item (custom path or default)
            _disk_check_path = None
            try:
                _cp_id = item.get("custom_path_id")
                if _cp_id:
                    _cp = get_custom_path_by_id(_cp_id)
                    if _cp:
                        _disk_check_path = str(Path(_cp["path"]).expanduser())
            except Exception as e:
                logger.debug("[Queue] Could not resolve custom path for disk check: %s", e)
            _check_disk_space_and_notify(username=item.get("username"), check_path=_disk_check_path)

            episodes = json.loads(item["episodes"])
            # Carry over any pre-existing errors (e.g. other failed episodes that
            # were kept when the user retried just one specific episode).
            # For brand-new jobs the DB default is '[]', so this is always safe.
            errors = json.loads(item.get("errors") or "[]")

            # Language separation: compute subfolder path if enabled
            import os

            lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
            if item.get("source") == "sync:all_langs":
                lang_sep = True
            selected_path = None

            from pathlib import Path

            # Determine base path: custom path or default
            custom_path_id = item.get("custom_path_id")
            if custom_path_id:
                cp = get_custom_path_by_id(custom_path_id)
                if cp:
                    base = Path(cp["path"]).expanduser()
                    if not base.is_absolute():
                        base = Path.home() / base
                else:
                    base = None
            else:
                base = None

            if base is None:
                raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
                if raw:
                    base = Path(raw).expanduser()
                    if not base.is_absolute():
                        base = Path.home() / base
                else:
                    base = Path.home() / "Downloads"

            if lang_sep:
                lang_folder_map = {
                    "German Dub": "german-dub",
                    "English Sub": "english-sub",
                    "German Sub": "german-sub",
                    "English Dub": "english-dub",
                    "English Dub (German Sub)": "english-dub-german-sub",
                }
                lang_folder = lang_folder_map.get(
                    item["language"], item["language"].lower().replace(" ", "-")
                )
                selected_path = str(base / lang_folder)
            elif custom_path_id:
                selected_path = str(base)

            MAX_EP_RETRIES = 3

            # Create a cancel event for this item so api_queue_cancel can
            # kill the active subprocess immediately.
            _item_cancel = threading.Event()
            with _active_cancel_events_lock:
                _active_cancel_events[item["id"]] = _item_cancel

            # Friendly labels for language-not-available errors
            _LANG_LABELS = {
                ("GERMAN",   "NONE"):    "Deutsch",
                ("ENGLISH",  "NONE"):    "Englisch",
                ("JAPANESE", "GERMAN"):  "Japanisch mit deutschen Untertiteln",
                ("JAPANESE", "ENGLISH"): "Japanisch mit englischen Untertiteln",
                ("GERMAN",   "ENGLISH"): "Deutsch mit englischen Untertiteln",
                ("ENGLISH",  "GERMAN"):  "Englisch mit deutschen Untertiteln",
            }

            def _lang_unavailable_msg(exc):
                """Return a human-readable message if exc is a language-not-found error, else None."""
                msg = str(exc)
                if "No provider data found for language" not in msg:
                    return None
                audio_m = re.search(r"Audio\.(\w+)", msg)
                subs_m  = re.search(r"Subtitles\.(\w+)", msg)
                audio = audio_m.group(1).upper() if audio_m else ""
                subs  = subs_m.group(1).upper()  if subs_m  else "NONE"
                label = _LANG_LABELS.get((audio, subs), f"{audio}" + (f" / UT: {subs}" if subs != "NONE" else ""))
                return f"Nicht verfügbar in: {label}"

            total_bytes_before = 0
            _upscale_after_paths = []  # collect for batch after_download upscaling
            download_start_time = time.time()

            downloaded_count = 0
            for i, ep_url in enumerate(episodes):
                update_queue_progress(item["id"], i, ep_url)

                last_error = None
                _episode_skipped = False
                _episode_cancelled = False
                # Per-episode tracking for the download history
                _ep_start_time = time.time()
                _ep_path = None
                _ep_size_bytes = 0
                for attempt in range(1, MAX_EP_RETRIES + 1):
                    try:
                        prov = resolve_provider(ep_url)
                        ep_kwargs = {
                            "url": ep_url,
                            "selected_language": item["language"],
                            "selected_provider": item["provider"],
                        }
                        if selected_path:
                            ep_kwargs["selected_path"] = selected_path
                        episode = prov.episode_cls(**ep_kwargs)
                        from ..playwright import captcha as _captcha_mod
                        from ..models.common.common import get_ffmpeg_progress
                        _queue_id = item["id"]
                        _upscale = bool(item.get("upscale", 0))

                        # ── Watchdog: run download in a thread so a hung yt-dlp
                        # cannot freeze the entire queue worker forever. ──────
                        _HANG_TIMEOUT   = int(get_setting("watchdog_hang_timeout") or os.environ.get("MEDIAFORGE_HANG_TIMEOUT", "1800"))   # 30 min default
                        _STALL_TIMEOUT  = int(get_setting("watchdog_stall_timeout") or os.environ.get("MEDIAFORGE_STALL_TIMEOUT", "3600"))  # 60 min no progress (must exceed reconnect_delay_max=300)
                        _dl_exc = [None]
                        _dl_res = [None]
                        _dl_done = threading.Event()
                        _attempt_cancel = threading.Event()

                        def _dl_thread():
                            # threading.local() is per-thread — set queue_id here, not in the parent thread
                            _captcha_mod._local.queue_id = _queue_id
                            _captcha_mod._local.upscale = _upscale
                            try:
                                _dl_res[0] = episode.download(cancel_event=_attempt_cancel)
                            except Exception as _e:
                                _dl_exc[0] = _e
                            finally:
                                _captcha_mod._local.queue_id = None
                                _captcha_mod._local.upscale = False
                                _dl_done.set()

                        _t = threading.Thread(target=_dl_thread, daemon=True)
                        _t.start()

                        # Poll for stall / hard timeout
                        _last_pct    = -1.0
                        _last_change = time.monotonic()
                        _start_watch = time.monotonic()
                        _timed_out   = False
                        while not _dl_done.wait(timeout=5):
                            if _item_cancel.is_set():
                                _attempt_cancel.set()
                                break
                            _now = time.monotonic()
                            if _now - _start_watch > _HANG_TIMEOUT:
                                logger.error(
                                    f"[watchdog] Download hard-timeout ({_HANG_TIMEOUT}s) for {ep_url} — aborting"
                                )
                                _attempt_cancel.set()
                                _timed_out = True
                                break
                            _prog = get_ffmpeg_progress()
                            _pct  = _prog.get("percent", 0.0)
                            if _pct != _last_pct:
                                _last_pct    = _pct
                                _last_change = _now
                            elif _now - _last_change > _STALL_TIMEOUT:
                                logger.error(
                                    f"[watchdog] No progress for {_STALL_TIMEOUT}s on {ep_url} — aborting"
                                )
                                _attempt_cancel.set()
                                _timed_out = True
                                break

                        if _timed_out:
                            _dl_done.wait(timeout=10)  # give thread a moment to notice cancel
                            raise RuntimeError(
                                f"Download aborted by watchdog (stalled/hung after {_STALL_TIMEOUT}s without progress)"
                            )

                        if _dl_exc[0] is not None:
                            raise _dl_exc[0]
                        # ── end watchdog ─────────────────────────────────────
                        last_error = None

                        # Track size
                        try:
                            if hasattr(episode, "_episode_path") and episode._episode_path.exists():
                                _ep_size_bytes = os.path.getsize(episode._episode_path)
                                total_bytes_before += _ep_size_bytes
                                _ep_path = str(episode._episode_path)
                        except Exception:
                            pass

                        # Collect path for batch after_download upscaling
                        try:
                            if hasattr(episode, "_episode_path") and episode._episode_path.exists():
                                _upscale_after_paths.append(str(episode._episode_path))
                        except Exception:
                            pass

                        break  # success — stop retrying
                    except Exception as e:
                        from ..playwright import captcha as _captcha_mod
                        _captcha_mod._local.queue_id = None
                        if is_queue_cancelled(item["id"]) or "Download cancelled" in str(e):
                            _episode_cancelled = True
                            last_error = None
                            break
                        friendly = _lang_unavailable_msg(e)
                        if friendly is not None:
                            # Language simply doesn't exist — no point retrying
                            last_error = Exception(friendly)
                            logger.warning(f"Language unavailable for {ep_url}: {e}")
                            break
                        last_error = e
                        if attempt < MAX_EP_RETRIES:
                            delay = 2
                            logger.warning(
                                f"Episode {ep_url} failed (attempt {attempt}/{MAX_EP_RETRIES}), "
                                f"retrying in {delay}s: {e}"
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                f"Episode {ep_url} failed after {MAX_EP_RETRIES} attempts: {e}"
                            )
                    # Check skip flag after each attempt (success or fail)
                    if consume_episode_skip(item["id"]):
                        logger.info(f"Episode {ep_url} skipped by user request")
                        last_error = None  # treat as skipped, not failed
                        _episode_skipped = True
                        break

                from ..models.common.common import print_episode_summary
                if last_error is not None:
                    errors.append({"url": ep_url, "error": str(last_error)})
                    update_queue_errors(item["id"], json.dumps(errors))
                    print_episode_summary(item["title"], ep_url, success=False)
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "failed", error=last_error)
                elif _episode_cancelled:
                    print_episode_summary(item["title"], ep_url, success="Abgebrochen")
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "cancelled")
                elif not _episode_skipped:
                    if _dl_res[0] is not False:
                        downloaded_count += 1
                        print_episode_summary(item["title"], ep_url, success=True)
                        _record_download_history(item, ep_url, _ep_start_time, _ep_path, _ep_size_bytes, "completed")
                    else:
                        print_episode_summary(item["title"], ep_url, success="Bereits vorhanden")
                        _record_download_history(item, ep_url, _ep_start_time, _ep_path, 0, "skipped", error="Bereits vorhanden")
                else:
                    print_episode_summary(item["title"], ep_url, success=True)
                    _record_download_history(item, ep_url, _ep_start_time, None, 0, "skipped", error="Übersprungen")

                # Mark episode as done: increment counter and clear current URL
                update_queue_progress(item["id"], i + 1, "")

                # Check for cancellation after each episode
                if is_queue_cancelled(item["id"]):
                    logger.info(f"Download cancelled for queue item {item['id']}")
                    # Record the remaining, not-yet-started episodes as cancelled so
                    # the whole aborted job is reflected in the download history.
                    for _rem_url in episodes[i + 1:]:
                        _record_download_history(item, _rem_url, time.time(), None, 0, "cancelled")
                    break

                # Pause: hold here until resumed (checks every 2s)
                while is_queue_paused():
                    time.sleep(2)

            # Batch-trigger after_download upscaling for all collected episode paths
            if _upscale_after_paths and not is_queue_cancelled(item["id"]):
                try:
                    _trigger_batch_after_download_upscale(_upscale_after_paths, item.get("title", ""), upscale=bool(item.get("upscale", 0)))
                except Exception as _ue:
                    logger.warning(f"[Upscale] Batch-Trigger Fehler: {_ue}")

            # Only set final status if not already cancelled
            if not is_queue_cancelled(item["id"]):
                update_queue_progress(item["id"], len(episodes), "")

                # Calculate speed and update stats
                download_end_time = time.time()
                duration = download_end_time - download_start_time
                if duration > 0 and total_bytes_before > 0:
                    total_size_mb = total_bytes_before / (1024 * 1024)
                    avg_speed = total_size_mb / duration
                    update_queue_stats(item["id"], round(avg_speed, 2), round(total_size_mb, 1))

                successful = len(episodes) - len(errors)
                if not errors:
                    status = "completed"
                elif successful > 0:
                    status = "partial"
                else:
                    status = "failed"
                set_queue_status(item["id"], status)
                _final_status_set = True

                # Send notifications (all services)
                from .notifications import notify_all
                _is_movie = _is_filmpalast_url(item.get("url", ""))
                if status == "completed":
                    _body = "✅ Film heruntergeladen" if _is_movie else f"✅ {len(episodes)} Episode(n) heruntergeladen"
                    _event = "on_completed"
                elif status == "partial":
                    _body = f"❌ Film-Download fehlgeschlagen ({len(errors)} Fehler)" if _is_movie else f"⚠️ {successful} von {len(episodes)} Episode(n) heruntergeladen, {len(errors)} Fehler"
                    _event = "on_partial"
                else:
                    _body = f"❌ Download fehlgeschlagen ({len(errors)} Fehler)"
                    _event = "on_errors"
                notify_all(
                    title=item.get("title", "Unbekannt"),
                    body=_body,
                    event=_event,
                    username=item.get("username"),
                    status=status,
                    episode_count=len(episodes),
                    errors=errors,
                    is_movie=_is_movie,
                )
                # Trigger Jellyfin/Plex library refresh on completed or partial downloads
                if status in ("completed", "partial"):
                    _trigger_mediaplayer_refresh(title=item.get("title"), selected_path=selected_path)
                    # MediaScan: schedule a delayed library re-fetch (2 min) so
                    # Plex/Jellyfin has time to ingest the new file first
                    _schedule_mediascan_delayed(delay=120.0)
            else:
                _final_status_set = True
                from .notifications import notify_all
                notify_all(
                    title=item.get("title", "Unbekannt"),
                    body="⏹️ Download abgebrochen",
                    event="on_cancelled",
                    username=item.get("username"),
                    status="cancelled",
                    episode_count=len(episodes),
                    errors=[],
                )

        except Exception as e:
            logger.error(f"Queue worker error: {e}", exc_info=True)
            if item is not None and not _final_status_set:
                try:
                    if not is_queue_cancelled(item["id"]):
                        try:
                            errors = json.loads(item.get("errors") or "[]")
                        except Exception:
                            errors = []
                        errors.append({"url": item.get("series_url", ""), "error": f"Internal worker error: {str(e)}"})
                        update_queue_errors(item["id"], json.dumps(errors))
                        set_queue_status(item["id"], "failed")
                        
                        try:
                            from .notifications import notify_all
                            _is_movie = _is_filmpalast_url(item.get("url", ""))
                            notify_all(
                                title=item.get("title", "Unbekannt"),
                                body=f"❌ Download durch internen Fehler abgebrochen: {e}",
                                event="on_errors",
                                username=item.get("username"),
                                status="failed",
                                episode_count=0,
                                errors=[{"url": item.get("series_url", ""), "error": str(e)}],
                                is_movie=_is_movie,
                            )
                        except Exception as ne:
                            logger.error(f"Failed to send crash notification for item {item['id']}: {ne}", exc_info=True)
                except Exception as db_err:
                    logger.error(f"Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(3)
        finally:
            # Always deregister the cancel event for this item.
            if item is not None:
                with _active_cancel_events_lock:
                    _active_cancel_events.pop(item["id"], None)


def _ensure_queue_worker():
    """Start the queue worker thread once."""
    global _queue_worker_started
    with _queue_lock:
        if _queue_worker_started:
            return
        _queue_worker_started = True

    # Crash recovery: reset any 'running' items back to 'queued'
    from .db import get_db

    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET status = 'queued' WHERE status = 'running'"
        )
        conn.execute("UPDATE download_queue SET captcha_url = NULL")
        conn.commit()
    finally:
        conn.close()

    thread = threading.Thread(target=_queue_worker, daemon=True)
    thread.start()


def _normalize_episode_filter(value):
    """Normalise an episode_filter payload to a JSON string or None.

    Accepts a dict (from the API), a JSON string, or None. An empty/invalid
    value or an "all"-mode filter with no exclusions and no movies collapses to
    None, which means "no filter" (legacy behaviour).
    """
    if value is None:
        return None
    from .autosync_filter import parse_filter
    flt = parse_filter(value)
    if flt is None:
        return None
    # Collapse a no-op filter to NULL so legacy behaviour is preserved exactly.
    if (flt.get("mode") == "all" and not flt.get("seasons")
            and not flt.get("include_movies")):
        return None
    return json.dumps(flt, ensure_ascii=False)


def _parse_sync_days(raw, default="0,1,2,3,4,5,6"):
    """Parse a CSV of weekday indices (0=Mon..6=Sun) into a sorted set of ints."""
    if raw is None or str(raw).strip() == "":
        raw = default
    out = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part == "":
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if 0 <= v <= 6:
            out.add(v)
    return out


def _parse_sync_times(raw, default="06:00"):
    """Parse a CSV of HH:MM into a sorted list of (hour, minute) tuples."""
    if raw is None or str(raw).strip() == "":
        raw = default
    seen = set()
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part == "":
            continue
        if ":" not in part:
            continue
        hh, _, mm = part.partition(":")
        try:
            h, m = int(hh), int(mm)
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59 and (h, m) not in seen:
            seen.add((h, m))
            out.append((h, m))
    out.sort()
    return out


def _normalize_sync_times(raw):
    """Return a normalized CSV "HH:MM,HH:MM" string, or "" if none valid."""
    return ",".join(f"{h:02d}:{m:02d}" for (h, m) in _parse_sync_times(raw, default=""))


def _norm_title(s):
    """Lowercase, strip punctuation/diacritics-ish to bare alnum tokens."""
    import re, unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_is_confident(source, candidates, threshold=0.86):
    """Whether *source* clearly refers to the same title as one of *candidates*.

    Used to decide if a TMDB-localized title may safely replace the original
    site title. Errs toward False (keep the original) when unsure, so a wrong
    TMDB match (e.g. a spin-off) never overrides the real title.
    """
    import difflib
    src = _norm_title(source)
    if not src:
        return False
    best = 0.0
    for c in candidates:
        cn = _norm_title(c)
        if not cn:
            continue
        if cn == src:
            return True
        r = difflib.SequenceMatcher(None, src, cn).ratio()
        if r > best:
            best = r
    return best >= threshold


def _run_autosync_for_job(job, force_notify=False):
    """Check a single autosync job for new/missing episodes and queue them."""
    import os
    from datetime import datetime
    from pathlib import Path
    from .autosync_filter import parse_filter, episode_included, movie_included

    job_id = job["id"]
    with _syncing_jobs_lock:
        if job_id in _syncing_jobs:
            logger.info("Auto-sync skipped job %d — already running", job_id)
            return
        _syncing_jobs.add(job_id)

    try:
        # ------------------------------------------------------------------ #
        # Custom Path availability check                                       #
        # If the job has a custom_path_id, verify the directory is accessible #
        # before doing any online fetching. Depending on the job's             #
        # path_unavailable_action setting we either skip or hold.              #
        # ------------------------------------------------------------------ #
        from pathlib import Path as _Path
        _cp_id = job.get("custom_path_id")
        if _cp_id:
            _cp_record = get_custom_path_by_id(_cp_id)
            _cp_available = False
            if _cp_record:
                try:
                    _cp_available = _Path(_cp_record["path"]).expanduser().is_dir()
                except Exception:
                    _cp_available = False

            if not _cp_available:
                _global_action = os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip").lower()
                _action = (job.get("path_unavailable_action") or _global_action or "skip").lower()
                _was_on_hold = bool(job.get("on_hold"))

                if _action == "hold":
                    if not _was_on_hold:
                        # First time going on hold — persist state + notify
                        update_autosync_job(
                            job["id"],
                            on_hold=1,
                            last_error="Custom Path nicht erreichbar — Sync pausiert (Hold)",
                        )
                        logger.warning(
                            "Auto-sync HOLD for '%s' — custom path '%s' not accessible",
                            job.get("title", "?"),
                            _cp_record["path"] if _cp_record else _cp_id,
                        )
                        try:
                            from .notifications import notify_all
                            notify_all(
                                title=job.get("title", "Auto-Sync"),
                                body="⏸ Sync pausiert: Custom Path nicht erreichbar — "
                                     + str(_cp_record['path'] if _cp_record else 'Unbekannt'),
                                event="on_sync_hold",
                                username=job.get("added_by"),
                            )
                        except Exception as e:
                            logger.warning("[AutoSync] Hold notification failed: %s", e)
                    else:
                        logger.info(
                            "Auto-sync still on HOLD for '%s' — custom path still unavailable",
                            job.get("title", "?"),
                        )
                    return  # wait for next cycle
                else:
                    # action == "skip" (default)
                    logger.info(
                        "Auto-sync SKIP for '%s' — custom path not accessible (action=skip)",
                        job.get("title", "?"),
                    )
                    update_autosync_job(
                        job["id"],
                        last_check=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        last_error="Custom Path nicht erreichbar — Sync übersprungen",
                    )
                    return
            else:
                # Path is accessible — if we were on hold, clear it and notify resume
                if job.get("on_hold"):
                    update_autosync_job(job["id"], on_hold=0, last_error=None)
                    logger.info(
                        "Auto-sync RESUME for '%s' — custom path is accessible again",
                        job.get("title", "?"),
                    )
                    try:
                        from .notifications import notify_all
                        notify_all(
                            title=job.get("title", "Auto-Sync"),
                            body="▶️ Sync wird fortgesetzt: Custom Path ist wieder erreichbar — "
                                 + str(_cp_record['path'] if _cp_record else ''),
                            event="on_sync_resume",
                            username=job.get("added_by"),
                        )
                    except Exception as e:
                        logger.warning("[AutoSync] Resume notification failed: %s", e)

        prov = resolve_provider(job["series_url"])
        series = prov.series_cls(url=job["series_url"])

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        # Only use lang_sep for "All Languages" when the global setting is enabled;
        # otherwise scan root directory to avoid phantom missing-episode detection.
        if job.get("language") == "All Languages" and not lang_sep:
            logger.warning(
                "Auto-sync job '%s' uses 'All Languages' but lang_separation is off — scanning root.",
                job.get("title", "?"),
            )

        lang_folder_map = {
            "German Dub": "german-dub",
            "English Sub": "english-sub",
            "German Sub": "german-sub",
            "English Dub": "english-dub",
            "English Dub (German Sub)": "english-dub-german-sub",
        }

        target_languages = []
        if job.get("language") == "All Languages":
            disable_eng_sub = os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
            for lang in lang_folder_map.keys():
                if disable_eng_sub and lang == "English Sub":
                    continue
                target_languages.append(lang)
        else:
            target_languages.append(job["language"])

        # Phase 1: Count all episodes available online (language-independent).
        # This is done before any disk scan so we can decide early whether
        # new episodes have appeared since the last check.
        # We keep the episode object so Phase 2 can lazily check language
        # availability via provider_data before actually queuing.
        previous_episodes_found = job.get("episodes_found", 0)
        _flt = parse_filter(job.get("episode_filter"))
        _movies_on = movie_included(_flt)
        _filter_dirty = bool(job.get("filter_dirty"))
        # Resolve the path for movies/specials: dedicated path falls back to the
        # series path when unset.
        _movie_path_id = job.get("movie_custom_path_id") or job.get("custom_path_id")
        # list of (season_num, ep_num, url, ep_obj, is_movie)
        online_episodes = []
        for season in series.seasons:
            season_obj = prov.season_cls(url=season.url, series=series)
            if getattr(season_obj, "are_movies", False):
                # Movies / specials collection (aniworld "/filme"). Controlled
                # solely by the filter's include_movies flag — no per-episode
                # filtering. Legacy (no filter) keeps skipping movies.
                if not _movies_on:
                    logger.debug(
                        "Auto-sync: skipping movie season for '%s'",
                        job.get("title", "?"),
                    )
                    continue
                for ep in season_obj.episodes:
                    online_episodes.append(
                        (ep.season.season_number, ep.episode_number, ep.url, ep, True)
                    )
                continue
            for ep in season_obj.episodes:
                _sn = ep.season.season_number
                _en = ep.episode_number
                if not episode_included(_flt, _sn, _en):
                    continue
                online_episodes.append((_sn, _en, ep.url, ep, False))

        total_online_count = len(online_episodes)
        # (season, episode) pairs that are in scope of the filter — used to keep
        # the local/downloaded count consistent with the configured episodes.
        scope_pairs = {(s, e) for (s, e, _u, _o, _m) in online_episodes}
        is_first_run = previous_episodes_found == 0
        has_new_episodes_online = total_online_count > previous_episodes_found

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        if not is_first_run and not has_new_episodes_online:
            logger.info(
                "Auto-sync: no new episodes online for '%s' (%d found, unchanged) — will still check local files",
                job["title"],
                total_online_count,
            )

        if has_new_episodes_online:
            new_count = total_online_count - previous_episodes_found
            logger.info(
                "Auto-sync: %d new episode(s) detected for '%s' (was %d, now %d)",
                new_count,
                job["title"],
                previous_episodes_found,
                total_online_count,
            )
            pass  # notification fires in Phase 2 when episodes are actually queued

        # Phase 2: Per language — scan disk and queue missing episodes.
        total_new_queued = 0
        max_local_found = 0

        # Pre-fetch provider_data for all online episodes once, shared across all
        # languages — avoids one HTTP request per episode per language (N+1).
        _pd_cache: dict = {}  # url -> pd_data dict (or None on error)

        def _fetch_pd(ep_url, ep_obj):
            if ep_url in _pd_cache:
                return
            try:
                pd = ep_obj.provider_data
                _pd_cache[ep_url] = pd._data if hasattr(pd, "_data") else pd
            except Exception as exc:
                logger.debug("Auto-sync: provider_data prefetch failed for %s: %s", ep_url, exc)
                _pd_cache[ep_url] = None

        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=5) as _pool:
            for (_s, _e, _url, _ep, _is_movie) in online_episodes:
                _pool.submit(_fetch_pd, _url, _ep)

        # Compute scan_roots once — same for all languages
        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            dl_base = Path(raw).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            dl_base = Path.home() / "Downloads"

        scan_roots = [dl_base]
        for cp in get_custom_paths():
            cp_path = Path(cp["path"]).expanduser()
            if not cp_path.is_absolute():
                cp_path = Path.home() / cp_path
            scan_roots.append(cp_path)

        title_clean = (
            getattr(series, "title_cleaned", None) or getattr(series, "title", "")
        ).lower()
        ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)

        # mtime-based scan cache: {base_path_str -> (mtime, downloaded_eps_set)}
        # Avoids re-scanning the same folder multiple times within one sync run.
        _scan_cache: dict = {}

        def _scan_base(base: Path) -> set:
            """Return (season, episode) pairs found under base, using mtime cache."""
            key = str(base)
            try:
                mtime = base.stat().st_mtime if base.is_dir() else 0
            except OSError:
                mtime = 0
            cached = _scan_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
            eps: set = set()
            if base.is_dir() and title_clean:
                for folder in base.iterdir():
                    if not folder.is_dir() or not folder.name.lower().startswith(title_clean):
                        continue
                    for f in folder.rglob("*"):
                        if f.is_file():
                            m = ep_re.search(f.name)
                            if m:
                                eps.add((int(m.group(1)), int(m.group(2))))
            _scan_cache[key] = (mtime, eps)
            return eps

        for target_lang in target_languages:
            job_lang_folder = lang_folder_map.get(
                target_lang, target_lang.lower().replace(" ", "-")
            )

            # Build set of downloaded (season, episode) on disk using cached scans
            downloaded_eps: set = set()
            for root in scan_roots:
                base = (root / job_lang_folder) if lang_sep else root
                downloaded_eps |= _scan_base(base)

            in_scope_local = downloaded_eps & scope_pairs
            if len(in_scope_local) > max_local_found:
                max_local_found = len(in_scope_local)

            # Build the lang-enum target for language-availability checks.
            # We compare by string value so it works for both aniworld and s.to
            # enums (which are separate classes with identical values).
            _lang_key = INVERSE_LANG_LABELS.get(target_lang)
            _target_lang_str = None
            if _lang_key:
                _target_enum = LANG_KEY_MAP.get(_lang_key)
                if _target_enum:
                    _target_lang_str = (_target_enum[0].value, _target_enum[1].value)

            # Collect episode URLs that are not yet present on disk AND whose
            # target language is actually available online. Movies/specials are
            # collected separately so they can be queued to their own path.
            missing_series = []
            missing_movies = []
            for (s_num, e_num, url, ep_obj, is_movie) in online_episodes:
                if (s_num, e_num) in downloaded_eps:
                    continue
                # Check language availability before queuing to avoid
                # "No provider data found for language" errors in the queue worker.
                if _target_lang_str is not None:
                    # Use pre-fetched provider_data from cache (avoids repeated HTTP requests)
                    pd_data = _pd_cache.get(url)
                    if pd_data is None:
                        # Prefetch failed — skip conservatively to avoid queueing
                        # episodes that will fail with "No provider data for language"
                        logger.debug(
                            "Auto-sync: provider data unavailable for S%02dE%02d of '%s' — skipping",
                            s_num, e_num, job["title"],
                        )
                        lang_available = False
                    else:
                        try:
                            lang_available = any(
                                (k[0].value, k[1].value) == _target_lang_str
                                for k in pd_data
                            )
                        except Exception as exc:
                            logger.warning(
                                "Auto-sync: could not check language availability for S%02dE%02d of '%s': %s — skipping conservatively",
                                s_num, e_num, job["title"], exc,
                            )
                            lang_available = False  # can't verify → skip to avoid failed queue entries
                    if not lang_available:
                        logger.debug(
                            "Auto-sync: S%02dE%02d not yet available in '%s' for '%s' — skipping",
                            s_num, e_num, target_lang, job["title"],
                        )
                        continue
                if is_movie:
                    missing_movies.append(url)
                else:
                    missing_series.append(url)

            _src = ("sync:all_langs"
                    if job.get("language") == "All Languages" else "sync")
            # Queue series episodes and movie episodes as separate entries so they
            # can land in different download paths.
            for (_group, _path_id, _kind) in (
                (missing_series, job.get("custom_path_id"), "series"),
                (missing_movies, _movie_path_id, "movies"),
            ):
                if not _group:
                    continue
                with _dl_lock:
                    # Skip only if THESE episodes already overlap a queued/running
                    # item for this language (lets series + movies queue together).
                    if is_series_queued_or_running(
                        job["series_url"], language=target_lang,
                        requested_episodes=_group,
                    ):
                        logger.info(
                            "Auto-sync skipped '%s' (%s, %s) — already queued/running",
                            job["title"], target_lang, _kind,
                        )
                        continue

                    total_new_queued += len(_group)
                    add_to_queue(
                        title=job["title"],
                        series_url=job["series_url"],
                        episodes=_group,
                        language=target_lang,
                        provider=job["provider"],
                        username=job.get("added_by"),
                        custom_path_id=_path_id,
                        source=_src,
                    )
                logger.info(
                    "Auto-sync queued %d %s episode(s) for '%s' (%s)",
                    len(_group), _kind, job["title"], target_lang,
                )

        update_fields = {
            "last_check": now_str,
            "episodes_found": total_online_count,
            "local_episodes_found": max_local_found,
            "retry_count": 0,
        }

        # Only update last_new_found / last_new_count when episodes genuinely appeared online.
        # After a filter change (filter_dirty) the previous baseline was measured
        # against a different scope, so the delta is meaningless — recompute the
        # baseline silently and clear the flag without firing a "new" badge.
        if has_new_episodes_online and not _filter_dirty:
            update_fields["last_new_found"] = now_str
            update_fields["last_new_count"] = total_online_count - previous_episodes_found
        else:
            # Reset badge counter so UI shows "up to date" after a clean check
            update_fields["last_new_count"] = 0
        if _filter_dirty:
            update_fields["filter_dirty"] = 0

        update_fields["last_error"] = None  # clear any previous error on success
        update_autosync_job(job["id"], **update_fields)

        # Notify when episodes were actually queued for download
        if total_new_queued > 0:
            from .notifications import notify_all
            notify_all(
                title=job["title"],
                body=f"⬇️ {total_new_queued} neue Folge(n) werden heruntergeladen",
                event="on_autosync",
                username=job.get("added_by"),
                episode_count=total_new_queued,
            )
    except Exception as e:
        from datetime import datetime

        # Transient network errors (timeout, connection refused, DNS) are
        # expected occasionally and should not count as retryable failures.
        # Log as WARNING without traceback and skip retry-count increment.
        _net_keywords = ("ReadTimeout", "ConnectTimeout", "ConnectionError",
                         "TimeoutError", "timed out", "timeout", "ConnectionRefused",
                         "RemoteDisconnected", "NameResolutionError")
        _is_transient = any(kw.lower() in type(e).__name__.lower() or kw.lower() in str(e).lower()
                            for kw in _net_keywords)
        if _is_transient:
            logger.warning(
                "Auto-sync network error for '%s' (transient, will retry next cycle): %s",
                job.get("title", "?"), e,
            )
            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                last_error=f"[Netzwerkfehler] {e}",
            )
        else:
            logger.error("Auto-sync failed for '%s': %s", job.get("title", "?"), e, exc_info=True)

            current_retry = job.get("retry_count", 0)
            max_retries = int(get_setting("sync_error_retries") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
            new_retry = current_retry + 1

            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                last_error=str(e),
                retry_count=new_retry,
            )
        
        # Only send error notifications for non-transient failures
        if not _is_transient:
            only_notify_on_all_failed = get_setting("notif_sync_error_only_failed_all", "0") == "1"
            should_notify = True
            if only_notify_on_all_failed:
                if new_retry <= max_retries:
                    should_notify = False

            if should_notify:
                try:
                    from .notifications import notify_all
                    notify_all(
                        title=job.get("title", "Auto-Sync"),
                        body=f"❌ Sync-Fehler: {str(e)[:200]}",
                        event="on_sync_error",
                        username=job.get("added_by"),
                    )
                except Exception as e:
                    logger.warning("[AutoSync] Error notification failed: %s", e)
    finally:
        with _syncing_jobs_lock:
            _syncing_jobs.discard(job_id)


def _autosync_worker():
    """Background thread that periodically syncs all enabled autosync jobs.

    Uses short-polling (every 10 s) and checks each job's last_check
    against the configured interval so that schedule changes take effect
    immediately instead of blocking in a long sleep.
    """
    import os
    from datetime import datetime, timedelta, time as dtime

    while True:
        try:
            # Throttled download-history retention prune (~hourly, also on first cycle)
            global _last_history_prune
            _now_mono = time.monotonic()
            if _now_mono - _last_history_prune > 3600:
                _last_history_prune = _now_mono
                try:
                    _hrd = int(get_setting("history_retention_days")
                               or os.environ.get("MEDIAFORGE_HISTORY_RETENTION_DAYS", "30"))
                    _pruned = prune_download_history(_hrd)
                    if _pruned:
                        logger.info("[History] pruned %d entries older than %d days", _pruned, _hrd)
                except Exception as _pe:
                    logger.debug("[History] prune failed: %s", _pe)

            mode = (os.environ.get("MEDIAFORGE_SYNC_MODE", "interval") or "interval").lower()
            schedule_key = os.environ.get("MEDIAFORGE_SYNC_SCHEDULE", "0")
            interval = SYNC_SCHEDULE_MAP.get(schedule_key, 0)

            # Nothing to do when interval mode is disabled and we're not on a weekly plan.
            if mode != "weekly" and not interval:
                time.sleep(10)
                continue

            now = datetime.utcnow()
            jobs = get_autosync_jobs()
            max_retries = int(get_setting("sync_error_retries") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
            retry_time_key = get_setting("sync_error_retry_time") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRY_TIME", "5min")
            job_retry_interval = SYNC_RETRY_MAP.get(retry_time_key, 300)

            # Adaptive Auto-Sync: jobs that have not found a new episode for a long
            # time are slowed down to a wider re-check interval ("pause mode") until
            # something new appears again, after which they return to the normal cycle.
            adaptive_enabled = (get_setting("sync_adaptive_enabled")
                                or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_ENABLED", "0")) == "1"
            adaptive_pause_key = (get_setting("sync_adaptive_pause_after")
                                  or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER", "4w"))
            adaptive_pause_seconds = SYNC_ADAPTIVE_PAUSE_MAP.get(adaptive_pause_key, 4 * 7 * 86400)
            try:
                adaptive_retry_value = int(get_setting("sync_adaptive_retry_value")
                                           or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_VALUE", "2"))
            except (ValueError, TypeError):
                adaptive_retry_value = 2
            adaptive_retry_unit = (get_setting("sync_adaptive_retry_unit")
                                   or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_UNIT", "days"))
            adaptive_retry_interval = adaptive_retry_value * SYNC_ADAPTIVE_UNIT_MAP.get(adaptive_retry_unit, 86400)

            # Weekly plan: determine the most recent scheduled slot that has
            # already passed today (in local time), expressed as naive UTC so it
            # can be compared against the UTC-stored last_check.
            weekly_slot_utc = None
            if mode == "weekly":
                local_now = datetime.now()
                utc_off = datetime.now().astimezone().utcoffset() or timedelta(0)  # local - utc
                days = _parse_sync_days(os.environ.get("MEDIAFORGE_SYNC_DAYS", "0,1,2,3,4,5,6"))
                times = _parse_sync_times(os.environ.get("MEDIAFORGE_SYNC_TIMES", "06:00"))
                if local_now.weekday() in days and times:
                    passed = [datetime.combine(local_now.date(), dtime(h, m))
                              for (h, m) in times
                              if datetime.combine(local_now.date(), dtime(h, m)) <= local_now]
                    if passed:
                        weekly_slot_utc = max(passed) - utc_off  # local -> naive UTC

            for job in jobs:
                if not job.get("enabled"):
                    continue
                last_check = job.get("last_check")
                try:
                    last_dt = (datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                               if last_check else datetime.min)
                except (ValueError, TypeError):
                    last_dt = datetime.min

                retry_count = job.get("retry_count", 0)
                in_retry = 0 < retry_count <= max_retries

                should_run = False
                if in_retry:
                    # A failed job retries on the short retry interval regardless of mode.
                    if now >= last_dt + timedelta(seconds=job_retry_interval):
                        should_run = True
                elif mode == "weekly":
                    # Run once per slot: the slot passed and the job hasn't run since.
                    if weekly_slot_utc is not None and last_dt < weekly_slot_utc <= now:
                        should_run = True
                else:
                    # Effective interval: normally the configured one, but widened
                    # to the adaptive "retry after" interval while a job is in pause
                    # mode (no new episode found for longer than the threshold).
                    eff_interval = interval
                    if interval and adaptive_enabled:
                        last_new = job.get("last_new_found")
                        ref_dt = None
                        if last_new:
                            try:
                                ref_dt = datetime.strptime(last_new, "%Y-%m-%d %H:%M:%S")
                            except (ValueError, TypeError):
                                ref_dt = None
                        if ref_dt is not None and now >= ref_dt + timedelta(seconds=adaptive_pause_seconds):
                            eff_interval = adaptive_retry_interval
                    if eff_interval and now >= last_dt + timedelta(seconds=eff_interval):
                        should_run = True

                if should_run:
                    _run_autosync_for_job(job)

            time.sleep(10)
        except Exception as e:
            logger.error("Auto-sync worker error: %s", e, exc_info=True)
            time.sleep(30)


def _ensure_autosync_worker():
    """Start the auto-sync worker thread once."""
    global _autosync_worker_started
    if _autosync_worker_started:
        return
    _autosync_worker_started = True
    thread = threading.Thread(target=_autosync_worker, daemon=True)
    thread.start()



# ---------------------------------------------------------------------------
# Upscale Queue Worker
# ---------------------------------------------------------------------------

def _upscale_worker():
    """Single global worker that processes one upscale job at a time."""
    while True:
        try:
            item = None
            _final_status_set = False
            with _upscale_lock:
                item = claim_next_upscale_queued()

            if not item:
                time.sleep(4)
                continue

            cancel_ev = threading.Event()
            with _upscale_cancel_lock:
                _upscale_active_cancel_events[item["id"]] = cancel_ev

            try:
                from ..anime4k.anime4k import upscale_file, get_upscale_progress
                from ..anime4k.anime4k import _upscale_progress, _upscale_progress_lock
            except ImportError:
                set_upscale_status(item["id"], "failed")
                set_upscale_error(item["id"], "anime4k Modul nicht gefunden")
                continue

            settings = {
                "preset":     get_setting("upscaling_shader_preset", "B"),
                "quality":    get_setting("upscaling_shader_quality", "high"),
                "resolution": get_setting("upscaling_resolution", "1080p"),
                "engine":     get_setting("upscaling_engine", "auto"),
                "out_vcodec": get_setting("upscaling_out_vcodec", "libx264"),
                "out_crf":    int(get_setting("upscaling_out_crf", "18") or "18"),
                "out_preset": get_setting("upscaling_out_preset", "medium"),
            }

            # Progress-poll thread: mirrors anime4k live progress -> DB every 2s
            import threading as _th
            _poll_stop = _th.Event()
            def _progress_poller():
                while not _poll_stop.wait(2):
                    prog = get_upscale_progress()
                    if prog.get("active") and not is_upscale_cancelled(item["id"]):
                        _cur_idx = item.get("_runtime_file_idx", 0)
                        _tot = max(item.get("total_files", 1), 1)
                        _base = _cur_idx / _tot * 100
                        _file_pct = prog.get("percent", 0.0) / _tot
                        update_upscale_progress(item["id"],
                            min(round(_base + _file_pct, 1), 99.9))
            _pt = _th.Thread(target=_progress_poller, daemon=True)
            _pt.start()

            import json as _wjson
            from pathlib import Path as _WPath

            # Build file list: multi-file entries store JSON in .files column
            _raw_files = item.get("files")
            if _raw_files:
                try:
                    _file_list = _wjson.loads(_raw_files)
                except Exception:
                    _file_list = [{"file_path": item["file_path"],
                                   "output_path": item.get("output_path") or item["file_path"]}]
            else:
                _file_list = [{"file_path": item["file_path"],
                               "output_path": item.get("output_path") or item["file_path"]}]

            _total_files = max(len(_file_list), 1)
            _overall_failed = 0

            for _fi, _fentry in enumerate(_file_list):
                if is_upscale_cancelled(item["id"]):
                    break

                file_path   = _fentry["file_path"]
                output_path = _fentry.get("output_path") or file_path

                _replace_original = (file_path == output_path)
                if _replace_original:
                    actual_output = str(_WPath(file_path).with_suffix(".upscale_tmp.mkv"))
                else:
                    actual_output = output_path

                # Track current file index (poller reads this)
                item["_runtime_file_idx"] = _fi
                update_upscale_progress(item["id"],
                    round(_fi / _total_files * 100, 1),
                    current_file_idx=_fi)

                try:
                    upscale_file(
                        input_path=file_path,
                        output_path=actual_output,
                        settings=settings,
                        cancel_event=cancel_ev,
                        label=item.get("title", ""),
                    )
                    if not is_upscale_cancelled(item["id"]):
                        if _replace_original:
                            _WPath(file_path).unlink(missing_ok=True)
                            _WPath(actual_output).rename(file_path)
                except Exception as _fe:
                    _overall_failed += 1
                    logger.error(f"[Upscale] Fehler bei {file_path}: {_fe}")
                    try:
                        _WPath(actual_output).unlink(missing_ok=True)
                    except Exception:
                        pass
                    # Continue with next file unless cancelled
                    if is_upscale_cancelled(item["id"]):
                        break

            # Final status
            if not is_upscale_cancelled(item["id"]):
                update_upscale_progress(item["id"], 100.0, current_file_idx=_total_files)
                if _overall_failed == 0:
                    set_upscale_status(item["id"], "completed")
                elif _overall_failed < _total_files:
                    set_upscale_status(item["id"], "completed")
                    set_upscale_error(item["id"], f"{_overall_failed}/{_total_files} Datei(en) fehlgeschlagen")
                else:
                    set_upscale_status(item["id"], "failed")
                    set_upscale_error(item["id"], f"Alle {_total_files} Datei(en) fehlgeschlagen")
                _final_status_set = True

            _poll_stop.set()
            _pt.join(timeout=3)

            if is_upscale_cancelled(item["id"]):
                set_upscale_status(item["id"], "cancelled")
                _final_status_set = True

            with _upscale_cancel_lock:
                _upscale_active_cancel_events.pop(item["id"], None)

        except Exception as e:
            logger.error(f"[Upscale] Worker-Fehler: {e}", exc_info=True)
            if item is not None and not _final_status_set:
                try:
                    if not is_upscale_cancelled(item["id"]):
                        set_upscale_status(item["id"], "failed")
                        set_upscale_error(item["id"], f"Upscale worker error: {str(e)}")
                except Exception as db_err:
                    logger.error(f"[Upscale] Failed to set status to failed for item {item['id']}: {db_err}", exc_info=True)
            time.sleep(5)


def _ensure_upscale_worker():
    """Start the upscale worker thread once."""
    global _upscale_worker_started
    with _upscale_lock:
        if _upscale_worker_started:
            return
        _upscale_worker_started = True
    reset_running_upscale_items()
    thread = threading.Thread(target=_upscale_worker, daemon=True, name="upscale-worker")
    thread.start()


def _trigger_batch_after_download_upscale(episode_paths, title, upscale=False):
    """Add ALL downloaded episodes as ONE upscale queue entry."""
    try:
        mode = get_setting("upscaling_mode", "disabled")
        if mode != "after_download":
            return
        if not upscale:
            return
        replace = get_setting("upscaling_replace_original", "1") == "1"
        from pathlib import Path as _Path
        valid = []
        for episode_path in episode_paths:
            ep = _Path(episode_path)
            if not ep.exists():
                continue
            out = str(ep) if replace else str(ep.with_name(ep.stem + " (upscale).mkv"))
            valid.append({"file_path": str(ep), "output_path": out})
        if not valid:
            return
        add_to_upscale_queue(
            title=title,
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="download",
            files=valid if len(valid) > 1 else None,
        )
        logger.info(f"[Upscale] {len(valid)} Datei(en) als ein Eintrag in Queue: {title}")
    except Exception as exc:
        logger.warning(f"[Upscale] Batch-Trigger Fehler: {exc}")

def _get_version():
    """Return the base version string from package metadata (e.g. '2.1.6')."""
    try:
        from importlib.metadata import version

        return version("mediaforge")
    except Exception:
        return ""


def _get_dev_install_info():
    """
    Detect whether mediaforge was installed from a Git branch (dev install).

    pip writes a ``direct_url.json`` file into the dist-info directory whenever
    a package is installed via ``git+https://...``.  We read that file to get
    the exact commit SHA and the requested revision.

    A git install is only considered a *dev* install when the requested revision
    is a branch name (e.g. ``models``) rather than a version tag (e.g. ``v2.1.7``).

    Returns:
        (is_dev: bool, full_commit_sha: str | None)
    """
    try:
        import importlib.metadata as _meta
        import json as _json
        import re as _re

        dist = _meta.distribution("mediaforge")
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            return False, None
        data = _json.loads(direct_url_text)
        vcs_info = data.get("vcs_info", {})
        if vcs_info.get("vcs") == "git":
            commit_id = vcs_info.get("commit_id", "")
            requested_revision = vcs_info.get("requested_revision", "")
            # Version tags like v2.1.7 or 2.1.7 are release installs, not dev
            if _re.match(r"^v?\d+\.\d+", requested_revision):
                return False, None
            return True, commit_id if commit_id else None
        return False, None
    except Exception:
        return False, None


def _get_display_version():
    """
    Return the version string shown in the UI.

    - Release install (``@v2.1.6``):  ``"2.1.6"``
    - Dev install    (``@main``):   ``"2.1.6-dev+abc1234"``
    """
    base = _get_version()
    if not base:
        return ""
    is_dev, commit_hash = _get_dev_install_info()
    if is_dev and commit_hash:
        return f"{base}-dev+{commit_hash[:7]}"
    return base


# ---------------------------------------------------------------------------
# Update checker
# ---------------------------------------------------------------------------
_GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/PD-Codes/MediaForge/releases/latest"
)
_GITHUB_COMMITS_URL = (
    "https://api.github.com/repos/PD-Codes/MediaForge/commits/main"
)
_UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours

_update_cache: dict = {
    "latest_version": None,
    "update_available": False,
    "release_url": None,
    "release_notes": None,
    "checked_at": 0.0,
    "error": None,
    "is_dev_install": False,
}


def _fetch_latest_release():
    """Return (version, release_url, release_notes) from the GitHub Releases API."""
    import json
    import urllib.request as _ureq

    try:
        req = _ureq.Request(
            _GITHUB_RELEASES_URL,
            headers={
                "User-Agent": "mediaforge-update-checker/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with _ureq.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        tag = data.get("tag_name", "")
        version = tag.lstrip("v")
        return version, data.get("html_url"), data.get("body") or ""
    except Exception:
        return None, None, None


def _fetch_latest_commit_sha():
    """Return the full SHA of the latest commit on the main branch."""
    import json
    import urllib.request as _ureq

    try:
        req = _ureq.Request(
            _GITHUB_COMMITS_URL,
            headers={
                "User-Agent": "mediaforge-update-checker/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with _ureq.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return data.get("sha", None)
    except Exception:
        return None


def _do_update_check():
    import time
    from packaging.version import InvalidVersion, Version

    is_dev, full_commit_hash = _get_dev_install_info()
    local_base = _get_version()

    _update_cache["checked_at"] = time.time()
    _update_cache["is_dev_install"] = is_dev

    if is_dev:
        # Dev install: compare our commit SHA against the latest on main branch
        latest_sha = _fetch_latest_commit_sha()
        if latest_sha and full_commit_hash:
            update_available = not latest_sha.startswith(full_commit_hash[:7]) and latest_sha != full_commit_hash
            _update_cache["update_available"] = update_available
            _update_cache["latest_version"] = latest_sha[:7]
            _update_cache["release_url"] = (
                "https://github.com/PD-Codes/MediaForge/commits/main"
            )
            _update_cache["release_notes"] = None
            _update_cache["error"] = None
        else:
            _update_cache["update_available"] = False
            _update_cache["latest_version"] = None
            _update_cache["error"] = "GitHub nicht erreichbar"
    else:
        # Release install: compare version numbers against latest GitHub Release
        latest, release_url, release_notes = _fetch_latest_release()
        _update_cache["latest_version"] = latest
        _update_cache["release_url"] = release_url
        _update_cache["release_notes"] = release_notes

        if latest and local_base:
            try:
                _update_cache["update_available"] = Version(latest) > Version(local_base)
                _update_cache["error"] = None
            except InvalidVersion:
                _update_cache["update_available"] = False
                _update_cache["error"] = "Versionsformat unbekannt"
        else:
            _update_cache["update_available"] = False
            _update_cache["error"] = "GitHub nicht erreichbar" if not latest else None



def _generate_pwa_icons():
    """Generate icon-192.png and icon-512.png in the static directory if missing.

    Uses only Python stdlib (struct + zlib) — no Pillow required.
    The icons are a solid #7c3aed (purple) square.
    """
    import os as _os
    static_dir = _os.path.join(_os.path.dirname(__file__), "static")

    def _png_bytes(size: int) -> bytes:
        """Create a minimal valid PNG: solid #7c3aed square of given size."""
        r, g, b = 0x7C, 0x3A, 0xED

        # Build raw image data: one filter byte (0) + RGB pixels per row
        row = bytes([0]) + bytes([r, g, b] * size)
        raw = row * size

        def _chunk(tag: bytes, data: bytes) -> bytes:
            length = struct.pack(">I", len(data))
            crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            return length + tag + data + crc

        signature = b"\x89PNG\r\n\x1a\n"
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        idat = _chunk(b"IDAT", zlib.compress(raw, 9))
        iend = _chunk(b"IEND", b"")
        return signature + ihdr + idat + iend

    for size in (192, 512):
        path = _os.path.join(static_dir, f"icon-{size}.png")
        if not _os.path.exists(path):
            try:
                with open(path, "wb") as f:
                    f.write(_png_bytes(size))
                logger.debug("[PWA] Generated %s", path)
            except Exception as exc:
                logger.warning("[PWA] Could not generate icon-%s.png: %s", size, exc)



def _migrate_dotenv_to_db():
    """One-time migration: read ~/.mediaforge/.env (if it exists) and import
    all known variables into the DB.  Runs only once — guarded by the
    'env_migrated' key in app_settings so subsequent starts skip it."""
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
    will automatically pick up DB values without needing individual changes."""
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
    for db_key, env_key in mapping.items():
        val = get_setting(db_key)
        if val is not None and val != "":
            os.environ[env_key] = val


_tmdb_keywords_worker_started = False

def _tmdb_keywords_sync_worker():
    """Background task to sync the daily TMDB keyword export."""
    import time
    import gzip
    import urllib.request
    from datetime import datetime
    
    while True:
        try:
            from .db import get_setting
            # Only run if advanced search is enabled in config
            if get_setting("cineinfo_advanced_search", "0") != "1":
                time.sleep(3600)
                continue
                
            yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%m_%d_%Y")
            url = f"https://files.tmdb.org/p/exports/keyword_ids_{yesterday_str}.json.gz"
            dest_file = MEDIAFORGE_CONFIG_DIR / "keyword_ids.json"
            
            download_needed = True
            if dest_file.exists():
                mtime = datetime.utcfromtimestamp(dest_file.stat().st_mtime)
                if mtime.date() == datetime.utcnow().date():
                    download_needed = False
            
            if download_needed:
                logger.info(f"Downloading TMDB keywords from {url}")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                try:
                    with urllib.request.urlopen(req, timeout=30) as response:
                        with gzip.GzipFile(fileobj=response) as gz:
                            data = gz.read()
                            with open(dest_file, "wb") as f:
                                f.write(data)
                    logger.info("Successfully downloaded TMDB keywords.")
                except Exception as e:
                    logger.warning(f"Failed to download TMDB keywords: {e}")
                    
        except Exception as e:
            logger.error(f"Error in TMDB keywords sync worker: {e}")
            
        time.sleep(3600)  # Check every hour

def _ensure_tmdb_keywords_sync_worker():
    global _tmdb_keywords_worker_started
    if _tmdb_keywords_worker_started:
        return
    _tmdb_keywords_worker_started = True
    thread = threading.Thread(target=_tmdb_keywords_sync_worker, daemon=True)
    thread.start()

def create_app(auth_enabled=True, sso_enabled=False, force_sso=False):
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

    # ── i18n / Flask-Babel ──────────────────────────────────────────────────
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
    # ──────────────────────────────────────────────────────────────────────── # DKS, nur für Entwicklung
    app_version = _get_display_version()

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
                "  ERSTEINRICHTUNG — Noch kein Admin-Konto vorhanden.\n"
                f"  Setup-Token: {_setup_token}\n"
                "  Lokale Installation: \n"
                "  Öffne http://localhost:<PORT>/setup?token=<token> im Browser.\n"
                "  Docker Installation: \n"
                "  Öffne http://<DockerHostIP>:<HostPort>/setup?token=<token> im Browser.\n"
                "  Standardport ist 8080\n"
                "  Der Token ist 30 Minuten gültig. Danach App neu starten.\n"
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
    init_calendar_db()

    # Periodically evict expired TMDB cache entries so the table doesn't grow unboundedly
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

    threading.Thread(target=_tmdb_cache_eviction_loop, daemon=True,
                     name="tmdb-cache-evict").start()

    init_browse_cache_db()
    init_notification_db()
    init_upscale_queue_db()
    init_mediascan_db()
    init_watch_progress_db()
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

    # Apply saved filmpalast subfolder setting on startup
    os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = get_setting("filmpalast_movie_subfolder", "0")

    # One-time migration: import .env values into DB (runs only once)
    _migrate_dotenv_to_db()

    # Apply all persistent DB settings to os.environ on startup
    _sync_db_settings_to_env()

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
        _ensure_tmdb_keywords_sync_worker()
        # Auto-download mpv.exe on Windows if missing
        try:
            from ..autodeps import ensure_mpv_windows_async
            ensure_mpv_windows_async()
        except Exception:
            pass

    @app.teardown_appcontext
    def _close_db_connection(exception):
        from flask import g
        conn = g.pop("db_conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    @app.after_request
    def _set_security_headers(response):
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
        """Reject non-JSON POST/PUT/DELETE on API routes to prevent form-based CSRF bypass."""
        if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/"):
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
        return render_template(
            "index.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )

    def _filmpalast_search(keyword):
        """Search filmpalast.to via autocomplete and return list of {title, url} dicts."""
        import urllib.parse as _up
        import requests as _req
        try:
            url = f"https://filmpalast.to/autocomplete.php?term={_up.quote(keyword)}"
            resp = _req.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/javascript, */*",
                    "Accept-Encoding": "gzip, deflate",  # requests decompresses automatically
                },
                timeout=8,
            )
            resp.raise_for_status()
            if not resp.text.strip():
                return []  # empty response = no results
            titles = resp.json()
        except Exception as e:
            logger.warning("FilmPalast autocomplete failed: %s", e)
            return []

        # The API returns either a list ["Title1", "Title2", ...]
        # or a dict {"0": "Title1", "1": "Title2", ...} depending on the query.
        # Normalise to a flat list of title strings.
        if isinstance(titles, dict):
            title_list = list(titles.values())
        elif isinstance(titles, list):
            title_list = titles
        else:
            return []

        candidates = []
        seen_urls = set()
        for title in title_list:
            if not isinstance(title, str) or not title.strip():
                continue
            slugs = _filmpalast_title_to_slugs(title)
            for slug in slugs:
                fp_url = f"https://filmpalast.to/stream/{slug}"
                if fp_url not in seen_urls:
                    seen_urls.add(fp_url)
                    candidates.append({"title": title, "url": fp_url})

        if not candidates:
            return []

        # Validate each candidate URL exists (our slug may differ from the real slug).
        # Run HEAD requests in parallel so the total wait is ~1 request RTT, not N×RTT.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests as _req2

        _val_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        def _head_ok(candidate):
            try:
                r = _req2.head(candidate["url"], headers=_val_headers, timeout=5, allow_redirects=True)
                return candidate if r.status_code == 200 else None
            except Exception:
                return None

        raw_ok = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_head_ok, c): c for c in candidates}
            for fut in as_completed(futures):
                ok = fut.result()
                if ok:
                    raw_ok.append(ok)

        # Restore original autocomplete order & keep only the first working URL per title
        order = {c["url"]: i for i, c in enumerate(candidates)}
        raw_ok.sort(key=lambda c: order.get(c["url"], 9999))

        results = []
        seen_titles = set()
        for item in raw_ok:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                results.append(item)

        # Re-sort results to preserve the original autocomplete order of titles
        title_order = {t: i for i, t in enumerate(title_list)}
        results.sort(key=lambda c: title_order.get(c["title"], 9999))
        return results

    def _filmpalast_title_to_slugs(title):
        """Convert a movie title to a list of potential filmpalast URL slugs.

        Strategy: lowercase, replace umlauts with both simple and phonetic forms,
        replace non-alphanumeric with hyphens, collapse consecutive hyphens,
        strip leading/trailing hyphens.
        """
        import unicodedata
        import itertools
        import re as _r

        replacements = {
            "ä": ["a", "ae"],
            "ö": ["o", "oe"],
            "ü": ["u", "ue"],
            "Ä": ["a", "ae"],
            "Ö": ["o", "oe"],
            "Ü": ["u", "ue"],
            "ß": ["s", "ss"]
        }

        chars = list(title)
        options = []
        for c in chars:
            if c in replacements:
                options.append(replacements[c])
            else:
                options.append([c])

        special_count = sum(1 for c in chars if c in replacements)
        if special_count > 4:
            simple_list = []
            complex_list = []
            for c in chars:
                if c in replacements:
                    simple_list.append(replacements[c][0])
                    complex_list.append(replacements[c][1])
                else:
                    simple_list.append(c)
                    complex_list.append(c)
            combinations = ["".join(simple_list), "".join(complex_list)]
        else:
            combinations = ["".join(p) for p in itertools.product(*options)]

        slugs = set()
        for s in combinations:
            s = unicodedata.normalize("NFKD", s)
            s = s.encode("ascii", "ignore").decode("ascii")
            s = s.lower()
            s = _r.sub(r"[^a-z0-9]+", "-", s)
            s = s.strip("-")
            if s:
                slugs.add(s)

        return list(slugs)

    @app.route("/api/search", methods=["POST"])
    def api_search():
        data = request.get_json(silent=True) or {}
        keyword = (data.get("keyword") or "").strip()
        keyword = re.sub(r"!+$", "", keyword).strip()
        site = (data.get("site") or "aniworld").strip()
        if not keyword:
            return jsonify({"error": "keyword is required"}), 400

        results = []

        def _get_site_results(kw, site_name):
            site_res = []
            if site_name == "sto":
                items = query_s_to(kw) or []
                if isinstance(items, dict): items = [items]
                for item in items:
                    link = item.get("link") or item.get("url", "")
                    if _STO_SERIES_LINK_PATTERN.match(link):
                        title = _html_unescape(item.get("title") or item.get("name", "Unknown")).replace("<em>", "").replace("</em>", "")
                        site_res.append({"title": title, "url": f"https://s.to{link}"})
            else:
                items = aniworld_query(kw) or []
                if isinstance(items, dict): items = [items]
                for item in items:
                    link = item.get("link") or item.get("url", "")
                    if _SERIES_LINK_PATTERN.match(link):
                        title = _html_unescape(item.get("title") or item.get("name", "Unknown")).replace("<em>", "").replace("</em>", "")
                        site_res.append({"title": title, "url": f"https://aniworld.to{link}"})
            return site_res

        if site == "filmpalast":
            results = _filmpalast_search(keyword)
        else:
            results = _get_site_results(keyword, site)
            
            # Fallback for apostrophes (AniWorld's search is broken for titles with apostrophes)
            if not results and ("'" in keyword or "’" in keyword):
                # Try the opposite apostrophe first
                alt_keyword = keyword.replace("'", "’") if "'" in keyword else keyword.replace("’", "'")
                if alt_keyword != keyword:
                    logger.debug("[CineInfo] Fallback Alt Apostrophe: Searching for %r", alt_keyword)
                    results = _get_site_results(alt_keyword, site)

                # Strategy: search for the part before the apostrophe
                if not results:
                    clean = keyword.replace("’", "'").split("'")[0].strip()
                    if clean and clean != keyword and clean != alt_keyword:
                        logger.debug("[CineInfo] Fallback 1: Searching for %r", clean)
                        results = _get_site_results(clean, site)
                
                # Secondary fallback: just remove the apostrophe
                if not results:
                    clean2 = keyword.replace("'", "").replace("’", "")
                    if clean2 and clean2 != keyword and clean2 != alt_keyword:
                        logger.debug("[CineInfo] Fallback 2: Searching for %r", clean2)
                        results = _get_site_results(clean2, site)

            # Fallback for hyphens / dashes
            if not results and ("-" in keyword or "–" in keyword):
                # Strategy: search for the part before the hyphen
                clean_hyphen = re.split(r"[-–]", keyword)[0].strip()
                if clean_hyphen and clean_hyphen != keyword:
                    logger.debug("[CineInfo] Fallback Hyphen: Searching for %r", clean_hyphen)
                    results = _get_site_results(clean_hyphen, site)

        return jsonify({"results": results})


    @app.route("/api/tmdb/genres")
    def api_tmdb_genres():
        """Fetch TV and Movie genres from TMDB and cache them."""
        import requests as _req
        from .db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
        
        try:
            headers = {"accept": "application/json"}
            tv_de   = _req.get(f"https://api.themoviedb.org/3/genre/tv/list?language=de&api_key={api_key}",    headers=headers, timeout=10)
            tv_en   = _req.get(f"https://api.themoviedb.org/3/genre/tv/list?language=en&api_key={api_key}",    headers=headers, timeout=10)
            mov_de  = _req.get(f"https://api.themoviedb.org/3/genre/movie/list?language=de&api_key={api_key}", headers=headers, timeout=10)
            mov_en  = _req.get(f"https://api.themoviedb.org/3/genre/movie/list?language=en&api_key={api_key}", headers=headers, timeout=10)
            for r in (tv_de, tv_en, mov_de, mov_en):
                r.raise_for_status()

            return jsonify({
                "tv":    {"de": tv_de.json().get("genres", []),  "en": tv_en.json().get("genres", [])},
                "movie": {"de": mov_de.json().get("genres", []), "en": mov_en.json().get("genres", [])},
            })
        except Exception as e:
            logger.error(f"Error fetching TMDB genres: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tmdb/keywords")
    def api_tmdb_keywords():
        """Autocomplete search over the downloaded keyword_ids.json file."""
        query = request.args.get("q", "").strip().lower()
        if not query or len(query) < 2:
            return jsonify({"results": []})
        
        dest_file = MEDIAFORGE_CONFIG_DIR / "keyword_ids.json"
        if not dest_file.exists():
            return jsonify({"error": "Keyword data not downloaded yet. Please wait."}), 404
            
        results = []
        try:
            with open(dest_file, "r", encoding="utf-8") as f:
                for line in f:
                    if query in line.lower():
                        data = json.loads(line)
                        results.append(data)
                        if len(results) >= 20:
                            break
            return jsonify({"results": results})
        except Exception as e:
            logger.error(f"Error searching TMDB keywords: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tmdb/watch_regions")
    def api_tmdb_watch_regions():
        """Fetch the list of available watch-provider regions from TMDB."""
        import requests as _req
        from .db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400

        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en-US" if _ui_lang == "en" else "de-DE"
        url = f"https://api.themoviedb.org/3/watch/providers/regions?language={_tmdb_lang}&api_key={api_key}"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            return jsonify({"results": resp.json().get("results", [])})
        except Exception as e:
            logger.error(f"Error fetching TMDB watch regions: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tmdb/watch_providers")
    def api_tmdb_watch_providers():
        """Fetch the list of watch providers for tv/movie from TMDB."""
        import requests as _req
        from .db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400

        media_type = request.args.get("type", "tv")
        if media_type not in ("tv", "movie"):
            media_type = "tv"
        watch_region = request.args.get("watch_region", "").strip()

        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en-US" if _ui_lang == "en" else "de-DE"
        url = f"https://api.themoviedb.org/3/watch/providers/{media_type}?language={_tmdb_lang}&api_key={api_key}"
        if watch_region:
            url += f"&watch_region={watch_region}"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            results.sort(key=lambda p: p.get("display_priority", 9999))
            return jsonify({"results": results})
        except Exception as e:
            logger.error(f"Error fetching TMDB watch providers: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tmdb/discover")
    def api_tmdb_discover():
        """Search TMDB discover API with given params."""
        import requests as _req
        from .db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
            
        params = request.args.to_dict()
        media_type = params.pop("type", "tv")
        if media_type not in ["tv", "movie"]:
            media_type = "tv"
            
        import urllib.parse
        args = dict(request.args)
        args["api_key"] = api_key
        args.pop("type", None)
        
        # Map sorting key for TV shows / movies since TMDB uses different release date keys
        if "sort_by" in args:
            sort_val = args["sort_by"]
            if isinstance(sort_val, list):
                sort_val = sort_val[0] if sort_val else ""
            
            if isinstance(sort_val, str):
                if media_type == "tv" and sort_val.startswith("primary_release_date"):
                    args["sort_by"] = sort_val.replace("primary_release_date", "first_air_date")
                elif media_type == "movie" and sort_val.startswith("first_air_date"):
                    args["sort_by"] = sort_val.replace("first_air_date", "primary_release_date")
                    
        qs = urllib.parse.urlencode(args, doseq=True)
        url = f"https://api.themoviedb.org/3/discover/{media_type}?{qs}"
        logger.info(f"Discovering on TMDB: /discover/{media_type} (params redacted)")
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return jsonify(data)
        except Exception as e:
            logger.error(f"Error discovering on TMDB: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tmdb/details")
    def api_tmdb_details():
        """Fetch details for a specific TMDB item (e.g., to get number of seasons)."""
        import requests as _req
        from .db import get_setting
        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "No TMDB API Key"}), 400
            
        tmdb_id = request.args.get("id")
        media_type = request.args.get("type", "tv")
        
        if not tmdb_id or media_type not in ["tv", "movie"]:
            return jsonify({"error": "Invalid params"}), 400
            
        _ui_lang = session.get("ui_language", "de")
        _tmdb_lang = "en" if _ui_lang == "en" else "de"
        url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?language={_tmdb_lang}&api_key={api_key}&append_to_response=translations"
        try:
            resp = _req.get(url, headers={"accept": "application/json"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return jsonify(data)
        except Exception as e:
            logger.error(f"Error fetching TMDB details: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/series")
    def api_series():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: use episode object directly as metadata source (no series class)
        if _is_filmpalast_url(url):
            try:
                from ..models.filmpalast_to.episode import FilmPalastEpisode
                ep = FilmPalastEpisode(url=url)
                poster = ep.image_url
                if poster and poster.startswith("/"):
                    poster = f"https://filmpalast.to{poster}"
                
                title = ep.title_de or ""
                description = ep.description or ""
                genres = ep.genres or []
                
                from .db import get_setting
                api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
                if api_key:
                    try:
                        country = get_setting("cineinfo_country", "DE")
                        ui_lang = session.get("ui_language", "de")
                        tmdb_data = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                        if tmdb_data.get("found"):
                            if tmdb_data.get("title_confident"):
                                title = tmdb_data.get("title") or title
                            description = tmdb_data.get("overview") or description
                            if tmdb_data.get("genres"):
                                genres = tmdb_data.get("genres")
                    except Exception as _tmdb_exc:
                        logger.debug("[api_series] TMDB localization failed for FilmPalast: %s", _tmdb_exc)

                return jsonify({
                    "title": title,
                    "poster_url": _poster_proxy(poster),
                    "description": description,
                    "genres": genres,
                    "release_year": str(ep.release_year) if ep.release_year else "",
                    "is_movie": True,
                    "available_providers": ep.available_providers,
                })
            except Exception as e:
                logger.error(f"FilmPalast series fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        try:
            prov = resolve_provider(url)
            series = prov.series_cls(url=url)
            poster = getattr(series, "poster_url", None)
            # s.to returns relative poster paths - make them absolute
            if poster and poster.startswith("/"):
                from urllib.parse import urlparse

                parsed = urlparse(url)
                poster = f"{parsed.scheme}://{parsed.netloc}{poster}"
                
            title = _html_unescape(series.title)
            description = getattr(series, "description", "")
            genres = getattr(series, "genres", [])
            imdb_id = getattr(series, "imdb", None) or None
            
            from .db import get_setting
            api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
            if api_key:
                try:
                    country = get_setting("cineinfo_country", "DE")
                    ui_lang = session.get("ui_language", "de")
                    tmdb_data = _tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang)
                    if tmdb_data.get("found"):
                        # Only adopt the TMDB title when it is a confident match,
                        # otherwise keep the original site title (avoids wrong
                        # matches like a spin-off overriding the real name).
                        if tmdb_data.get("title_confident"):
                            title = tmdb_data.get("title") or title
                        description = tmdb_data.get("overview") or description
                        if tmdb_data.get("genres"):
                            genres = tmdb_data.get("genres")
                except Exception as _tmdb_exc:
                    logger.debug("[api_series] TMDB localization failed: %s", _tmdb_exc)

            return jsonify(
                {
                    "title": title,
                    "poster_url": _poster_proxy(poster),
                    "description": description,
                    "genres": genres,
                    "release_year": getattr(series, "release_year", ""),
                    "imdb_id": imdb_id,
                }
            )
        except Exception as e:
            logger.error(f"Series fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/seasons")
    def api_seasons():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: return a single fake "season 1 / episode 1 = the movie itself"
        if _is_filmpalast_url(url):
            return jsonify({"seasons": [{"url": url, "season_number": 1, "episode_count": 1, "are_movies": True, "is_single_movie": True}]})

        try:
            prov = resolve_provider(url)
            series = prov.series_cls(url=url)
            seasons_data = []
            for season in series.seasons:
                seasons_data.append(
                    {
                        "url": season.url,
                        "season_number": season.season_number,
                        "episode_count": season.episode_count,
                        "are_movies": getattr(season, "are_movies", False),
                    }
                )
            return jsonify({"seasons": seasons_data})
        except Exception as e:
            logger.error(f"Seasons fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/episodes")
    def api_episodes():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        # FilmPalast: return the movie itself as a single episode entry
        if _is_filmpalast_url(url):
            try:
                from ..models.filmpalast_to.episode import FilmPalastEpisode
                ep = FilmPalastEpisode(url=url)
                return jsonify({"episodes": [{
                    "url": url,
                    "episode_number": 1,
                    "season_number": 1,
                    "title_de": ep.title_de or "",
                    "title_en": ep.title_de or "",
                    "downloaded": False,
                    "languages": ["German Dub"],
                }]})
            except Exception as e:
                logger.error(f"FilmPalast episodes fetch failed: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        try:
            prov = resolve_provider(url)
            # Pass series to avoid broken series URL reconstruction in s.to
            # season model (its fallback splits on "-" which fails)
            series_url = re.sub(r"/staffel-\d+/?$", "", url)
            series_url = re.sub(r"/filme/?$", "", series_url)
            try:
                series = prov.series_cls(url=series_url)
            except Exception:
                series = None
            season = prov.season_cls(url=url, series=series)

            # Scan download directory for downloaded episodes.
            # Uses S##E### filename matching so it works regardless of
            # which NAMING_TEMPLATE was active when files were downloaded.
            from pathlib import Path

            lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
            lang_folders = ["german-dub", "english-sub", "german-sub", "english-dub"]

            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            if raw:
                dl_base = Path(raw).expanduser()
                if not dl_base.is_absolute():
                    dl_base = Path.home() / dl_base
            else:
                dl_base = Path.home() / "Downloads"

            # Collect all scan roots: default + custom paths
            scan_roots = [dl_base]
            for cp in get_custom_paths():
                cp_path = Path(cp["path"]).expanduser()
                if not cp_path.is_absolute():
                    cp_path = Path.home() / cp_path
                scan_roots.append(cp_path)

            # Build set of (season_num, episode_num) found on disk
            downloaded_eps = set()
            try:
                title_clean = ""
                if series:
                    title_clean = (
                        getattr(series, "title_cleaned", None)
                        or getattr(series, "title", "")
                    ).lower()
                if title_clean:
                    ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)
                    all_bases = []
                    for root in scan_roots:
                        if lang_sep:
                            all_bases.extend([root / lf for lf in lang_folders])
                        else:
                            all_bases.append(root)
                    for base in all_bases:
                        if not base.is_dir():
                            continue
                        for folder in base.iterdir():
                            if (
                                not folder.is_dir()
                                or not folder.name.lower().startswith(title_clean)
                            ):
                                continue
                            for f in folder.rglob("*"):
                                if f.is_file():
                                    m = ep_re.search(f.name)
                                    if m:
                                        downloaded_eps.add(
                                            (int(m.group(1)), int(m.group(2)))
                                        )
            except Exception:
                pass

            # Parse language flags per episode from the already-fetched season HTML
            # (no extra network requests — flags are embedded in the season page)
            ep_languages: dict[str, list[str]] = {}
            try:
                s_html = getattr(season, "_html", None) or ""
                _is_sto = "serienstream" in url or "/serie/" in url

                if _is_sto:
                    # Determine actual base domain from the season URL
                    _sto_base = "https://serienstream.to" if "serienstream" in url else "https://s.to"
                    # s.to: <tr class="episode-row" onclick="window.location='/serie/.../episode-X'">
                    #         <td class="episode-language-cell"> <svg class="svg-flag-german"> ...
                    # Trailing quote prevents svg-flag-german matching svg-flag-english-german
                    _sto_flag_map = {
                        'svg-flag-german':          "German Dub",
                        'svg-flag-english':         "English Dub",
                        'svg-flag-english-german':  "English Dub (German Sub)",
                        'svg-flag-english-english': "English Sub",
                    }
                    for _tr_m in re.finditer(r'<tr[^>]+class="episode-row[^"]*"[^>]*onclick="[^"]*\'(/serie/[^\']+)\'"', s_html):
                        _ep_path = _tr_m.group(1)
                        _ep_url = _sto_base + _ep_path
                        _tr_end = s_html.find("</tr>", _tr_m.start())
                        _tr_chunk = s_html[_tr_m.start():_tr_end]
                        _flag_classes = re.findall(r'svg-flag-[\w-]+', _tr_chunk)
                        _langs = []
                        for _cls in _flag_classes:
                            lbl = _sto_flag_map.get(_cls)
                            if lbl and lbl not in _langs:
                                _langs.append(lbl)
                        ep_languages[_ep_url] = _langs
                else:
                    # AniWorld: <td class="editFunctions"> with <img src=".../german.svg">
                    _flag_map = {
                        "/german.svg":           "German Dub",
                        "/japanese-german.svg":  "German Sub",
                        "/japanese-english.svg": "English Sub",
                        "/english.svg":          "English Dub",
                        "/english-german.svg":   "English Dub (German Sub)",
                    }
                    _marker = 'itemtype="http://schema.org/Episode"'
                    _pos = 0
                    while True:
                        _pos = s_html.find(_marker, _pos)
                        if _pos == -1:
                            break
                        _tr_s = s_html.rfind("<tr", 0, _pos)
                        _tr_e = s_html.find("</tr>", _pos)
                        if _tr_s == -1 or _tr_e == -1:
                            break
                        _tr = s_html[_tr_s:_tr_e]
                        _ep_url = None
                        _up = _tr.find('itemprop="url"')
                        if _up != -1:
                            _hs = _tr.find('href="', _up) + 6
                            _he = _tr.find('"', _hs)
                            _href = _tr[_hs:_he]
                            _ep_url = ("https://aniworld.to" + _href) if _href.startswith("/") else _href
                        if not _ep_url:
                            _hp = _tr.find("film-")
                            if _hp != -1:
                                _hs = _tr.rfind('href="', 0, _hp) + 6
                                _he = _tr.find('"', _hs)
                                _href = _tr[_hs:_he]
                                _ep_url = ("https://aniworld.to" + _href) if _href.startswith("/") else _href
                        if _ep_url:
                            _ed = _tr.find('class="editFunctions"')
                            if _ed != -1:
                                _ee = _tr.find("</td>", _ed)
                                _edit = _tr[_ed:_ee]
                                _langs = [lbl for src, lbl in _flag_map.items() if src in _edit]
                                ep_languages[_ep_url] = _langs
                        _pos = _tr_e
            except Exception as _lang_exc:
                logger.debug("[api_episodes] language flag parsing failed: %s", _lang_exc)

            episodes_data = []
            for ep in season.episodes:
                downloaded = (
                    ep.season.season_number,
                    ep.episode_number,
                ) in downloaded_eps

                episodes_data.append(
                    {
                        "url": ep.url,
                        "episode_number": ep.episode_number,
                        "title_de": getattr(ep, "title_de", ""),
                        "title_en": getattr(ep, "title_en", ""),
                        "downloaded": downloaded,
                        "languages": ep_languages.get(ep.url, []),
                    }
                )
            return jsonify({"episodes": episodes_data})
        except Exception as e:
            logger.error(f"Episodes fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/providers")
    def api_providers():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        try:
            prov = resolve_provider(url)
            episode = prov.episode_cls(url=url)
            pd = episode.provider_data

            disable_eng_sub = os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
            provider_info = {}

            # Normalize any provider_data format into {label: {name: url}}
            # so the availability check runs once, uniformly for all sites.
            raw_by_label = {}

            if isinstance(pd, list):
                # FilmPalast: [{"name": "VOE HD", "url": "..."}, ...]
                def _norm_fp(n):
                    n_clean = n.replace(" HD", "").replace(" HQ", "").strip()
                    for wp in WORKING_PROVIDERS:
                        if wp.lower() == n_clean.lower():
                            return wp
                    return n_clean
                raw_by_label["German Dub"] = {
                    _norm_fp(p["name"]): p["url"]
                    for p in pd
                    if p.get("name") and p.get("url")
                }
            elif hasattr(pd, "_data"):
                # AniWorld: ProviderData object
                lang_tuple_to_label = {
                    (audio.value, subtitles.value): LANG_LABELS.get(key)
                    for key, (audio, subtitles) in LANG_KEY_MAP.items()
                    if LANG_LABELS.get(key)
                }
                for (audio, subtitles), providers in pd._data.items():
                    label = lang_tuple_to_label.get((audio.value, subtitles.value))
                    if not label or (disable_eng_sub and label == "English Sub"):
                        continue
                    raw_by_label[label] = dict(providers)
            else:
                # s.to: plain dict with (Audio, Subtitles) enum tuple keys
                sto_label_map = {
                    ("German", "None"): "German Dub",
                    ("English", "None"): "English Dub",
                }
                for (audio, subtitles), providers in pd.items():
                    label = sto_label_map.get((audio.value, subtitles.value))
                    if label:
                        raw_by_label[label] = dict(providers)

            # Single unified availability check for every label / site
            is_filmpalast = (prov.name == "FilmPalast")
            for label, providers in raw_by_label.items():
                working = [
                    p for p, redirect in providers.items()
                    if p in WORKING_PROVIDERS and (not is_filmpalast or check_redirect_available(redirect))
                ]
                if working:
                    provider_info[label] = working

            return jsonify({"providers": provider_info})
        except Exception as e:
            logger.error(f"Providers fetch failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/veev/check", methods=["POST"])
    def api_veev_check():
        """Check whether a VeeV-hosted episode is actually available for streaming.

        The VeeV CDN sometimes serves a placeholder / offline page instead of
        real video.  We detect this by launching the same headless-browser session
        that the downloader uses and checking whether a 206-response CDN URL is
        captured.  If yes → available, if not → unavailable.
        """
        data = request.get_json(silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        if not episode_url:
            return jsonify({"available": False, "error": "episode_url fehlt"}), 400

        try:
            from ..models.filmpalast_to.episode import FilmPalastEpisode
            from ..extractors.provider.veev import _extract_veev_details
        except ImportError:
            try:
                from mediaforge.models.filmpalast_to.episode import FilmPalastEpisode
                from mediaforge.extractors.provider.veev import _extract_veev_details
            except ImportError as ie:
                return jsonify({"available": False, "error": f"Import-Fehler: {ie}"}), 500

        try:
            ep = FilmPalastEpisode(episode_url, selected_provider="VeeV")
            embed_url = ep.provider_url
        except Exception as e:
            return jsonify({"available": False, "error": f"Episode konnte nicht aufgelöst werden: {e}"})

        try:
            cdn_url, _, _ = _extract_veev_details(embed_url, timeout_ms=30_000)
        except Exception as e:
            return jsonify({"available": False, "error": f"Veev-Prüfung fehlgeschlagen: {e}"})

        if cdn_url:
            return jsonify({"available": True})
        return jsonify({
            "available": False,
            "error": "Dieser Film ist auf Veev momentan nicht verfügbar (kein Stream gefunden).",
        })

    @app.route("/api/download", methods=["POST"])
    def api_download():
        data = request.get_json(silent=True) or {}
        episodes = data.get("episodes", [])
        language = data.get("language", "German Dub")
        provider = data.get("provider", "VOE")
        title = data.get("title", "Unknown")
        series_url = str(data.get("series_url", "")).strip().rstrip("/")
        if not series_url:
            return jsonify({"error": "series_url is required"}), 400

        if not episodes:
            return jsonify({"error": "episodes list is required"}), 400

        if (
            language == "English Sub"
            and os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
        ):
            return jsonify({"error": "English Sub downloads are disabled"}), 403

        username = None
        if auth_enabled:
            user = get_current_user()
            if user:
                username = (
                    user.get("username")
                    if isinstance(user, dict)
                    else getattr(user, "username", None)
                )

        custom_path_id = data.get("custom_path_id")

        # Global lock to prevent race conditions during duplicate check + add
        with _dl_lock:
            # Check for duplicates before adding to queue
            from .db import is_series_queued_or_running
            if is_series_queued_or_running(series_url, language, requested_episodes=episodes):
                return jsonify({"error": "Diese Episoden befinden sich bereits in der Warteschlange (gleiche Sprache)."}), 400

            upscale = bool(data.get("upscale", False))
            queue_id = add_to_queue(
                title,
                series_url,
                episodes,
                language,
                provider,
                username,
                custom_path_id=custom_path_id,
                upscale=upscale,
            )
        return jsonify({"queue_id": queue_id})

    @app.route("/api/queue")
    def api_queue():
        from ..models.common.common import get_ffmpeg_progress
        from .db import get_general_stats

        items = get_queue()
        ffmpeg_pct = get_ffmpeg_progress()
        
        return jsonify({
            "items": items,
            "ffmpeg_progress": ffmpeg_pct,
            "paused": is_queue_paused()
        })

    @app.route("/api/queue/pause", methods=["POST"])
    def api_queue_pause():
        set_queue_paused(True)
        return jsonify({"paused": True})

    @app.route("/api/queue/resume", methods=["POST"])
    def api_queue_resume():
        set_queue_paused(False)
        return jsonify({"paused": False})

    @app.route("/api/push/vapid-public-key")
    def api_vapid_public_key():
        from .notifications import _ensure_vapid_keys
        _, key, _ = _ensure_vapid_keys()
        return jsonify({"vapid_public_key": key})

    @app.route("/api/push/subscribe", methods=["POST"])
    def api_push_subscribe():
        from flask import session as _sess
        from .notifications import add_push_subscription
        sub = request.get_json(silent=True)
        if not sub or "endpoint" not in sub:
            return jsonify({"error": "invalid subscription"}), 400
        add_push_subscription(sub, user_id=_sess.get("user_id"))
        return jsonify({"ok": True})

    @app.route("/api/push/unsubscribe", methods=["POST"])
    def api_push_unsubscribe():
        from .notifications import remove_push_subscription
        data = request.get_json(silent=True)
        if not data or "endpoint" not in data:
            return jsonify({"error": "missing endpoint"}), 400
        remove_push_subscription(data["endpoint"])
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Notification settings API                                            #
    # ------------------------------------------------------------------ #

    @app.route("/api/notif/settings")
    def api_notif_settings_get():
        """Return notification settings for the current user.

        Admins receive global admin settings (tokens masked as '*****').
        All users receive their own per-user settings and event prefs.
        """
        from flask import session as _sess
        uid  = _sess.get("user_id")
        role = _sess.get("user_role", "user")

        # Per-user prefs: uid=0 is the no-auth admin pseudo-user
        user_prefs = get_user_notif_prefs_all(uid) if uid is not None else {}

        result = {"user_prefs": user_prefs, "is_admin": role == "admin"}

        # Global admin settings — admins see masked tokens, users see booleans only
        admin_keys = [
            "notif_telegram_bot_token",
            "notif_telegram_enabled",
            "notif_pushover_app_token",
            "notif_pushover_enabled",
            "notif_discord_webhook_url",
            "notif_discord_enabled",
            "notif_discord_on_completed",
            "notif_discord_on_errors",
            "notif_discord_on_partial",
            "notif_discord_on_cancelled",
            "notif_discord_on_autosync",
            "notif_discord_on_sync_error",
            "notif_discord_on_sync_hold",
            "notif_discord_on_disk_space_low",
            "notif_disk_space_min_gb",
            "notif_sync_error_only_failed_all",
            "notif_whatsapp_sid",
            "notif_whatsapp_auth_token",
            "notif_whatsapp_from",
            "notif_whatsapp_enabled",
            "notif_webpush_enabled",
            "notif_ntfy_server",
            "notif_ntfy_topic",
            "notif_ntfy_auth_token",
            "notif_ntfy_user",
            "notif_ntfy_password",
            "notif_ntfy_enabled",
        ]
        admin_data = {}
        for k in admin_keys:
            raw = get_setting(k) or ""
            if role == "admin":
                # Return the real value to admins — they're already authenticated.
                # Sensitive inputs are shown as type=password in the UI (dots),
                # with an explicit eye-button to reveal. No need to mask here.
                admin_data[k] = raw
            else:
                # Non-admins only need to know if the service is configured
                admin_data[k] = bool(raw)
        result["admin"] = admin_data
        return jsonify(result)

    @app.route("/api/notif/admin-settings", methods=["POST"])
    def api_notif_admin_settings_set():
        """Update global admin notification settings. Admin only."""
        from flask import session as _sess
        if _sess.get("user_role") != "admin":
            return jsonify({"error": "admin access required"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid payload"}), 400

        allowed = {
            "notif_telegram_bot_token",
            "notif_telegram_enabled",
            "notif_pushover_app_token",
            "notif_pushover_enabled",
            "notif_discord_webhook_url",
            "notif_discord_enabled",
            "notif_discord_on_completed",
            "notif_discord_on_errors",
            "notif_discord_on_partial",
            "notif_discord_on_cancelled",
            "notif_discord_on_autosync",
            "notif_discord_on_sync_error",
            "notif_discord_on_sync_hold",
            "notif_discord_on_disk_space_low",
            "notif_disk_space_min_gb",
            "notif_sync_error_only_failed_all",
            "notif_whatsapp_sid",
            "notif_whatsapp_auth_token",
            "notif_whatsapp_from",
            "notif_whatsapp_enabled",
            "notif_webpush_enabled",
            "notif_ntfy_server",
            "notif_ntfy_topic",
            "notif_ntfy_auth_token",
            "notif_ntfy_user",
            "notif_ntfy_password",
            "notif_ntfy_enabled",
        }
        for k, v in data.items():
            if k not in allowed:
                continue
            val = str(v).strip()
            if val == "":
                delete_setting(k)
            else:
                set_setting(k, val)

        return jsonify({"ok": True})

    @app.route("/api/notif/user-settings", methods=["POST"])
    def api_notif_user_settings_set():
        """Update per-user notification settings (chat_id, user_key, phone, event prefs)."""
        from flask import session as _sess
        # uid=0 is the synthetic no-auth admin user
        uid = _sess.get("user_id")
        if uid is None:
            return jsonify({"error": "not authenticated"}), 401

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid payload"}), 400

        allowed_keys = {
            # Web Push prefs
            "webpush_on_completed", "webpush_on_errors",
            "webpush_on_partial", "webpush_on_cancelled", "webpush_on_autosync",
            "webpush_on_sync_error", "webpush_on_sync_hold", "webpush_on_disk_space_low",
            # Telegram
            "telegram_enabled",
            "telegram_chat_id",
            "telegram_on_completed", "telegram_on_errors",
            "telegram_on_partial", "telegram_on_cancelled", "telegram_on_autosync",
            "telegram_on_sync_error", "telegram_on_sync_hold", "telegram_on_disk_space_low",
            # Pushover
            "pushover_enabled",
            "pushover_user_key",
            "pushover_on_completed", "pushover_on_errors",
            "pushover_on_partial", "pushover_on_cancelled", "pushover_on_autosync",
            "pushover_on_sync_error", "pushover_on_sync_hold", "pushover_on_disk_space_low",
            # WhatsApp
            "whatsapp_enabled",
            "whatsapp_phone",
            "whatsapp_on_completed", "whatsapp_on_errors",
            "whatsapp_on_partial", "whatsapp_on_cancelled", "whatsapp_on_autosync",
            "whatsapp_on_sync_error", "whatsapp_on_sync_hold", "whatsapp_on_disk_space_low",
            # NTFY
            "ntfy_enabled",
            "ntfy_on_completed", "ntfy_on_errors",
            "ntfy_on_partial", "ntfy_on_cancelled", "ntfy_on_autosync",
            "ntfy_on_sync_error", "ntfy_on_sync_hold", "ntfy_on_disk_space_low",
        }
        filtered = {k: str(v) for k, v in data.items() if k in allowed_keys}
        set_user_notif_prefs_bulk(uid, filtered)
        return jsonify({"ok": True})

    @app.route("/api/notif/telegram/detect-chat-id")
    def api_telegram_detect_chat_id():
        """Call Telegram getUpdates and return the most recent chat_id."""
        from flask import session as _sess
        # Both admins and users can trigger this (bot token must be set by admin)
        from .notifications import telegram_detect_chat_id
        token = get_setting("notif_telegram_bot_token") or ""
        if not token:
            return jsonify({"error": "Bot-Token wurde noch nicht konfiguriert"}), 400
        chat_id = telegram_detect_chat_id(token)
        if chat_id is None:
            return jsonify({"error": "Keine Nachricht gefunden. Schreib dem Bot zuerst eine Nachricht."}), 404
        return jsonify({"chat_id": chat_id})

    @app.route("/api/notif/test", methods=["POST"])
    def api_notif_test():
        """Send a test notification via the requested service."""
        from flask import session as _sess
        uid      = _sess.get("user_id")
        # In no-auth mode username="admin" is the pseudo-user for per-user pref lookups
        username = _sess.get("user_name")
        data     = request.get_json(silent=True) or {}
        service  = data.get("service", "")

        if service == "webpush":
            from .notifications import notify_webpush
            notify_webpush("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "telegram":
            from .notifications import notify_telegram
            notify_telegram("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "pushover":
            from .notifications import notify_pushover
            notify_pushover("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "whatsapp":
            from .notifications import notify_whatsapp
            notify_whatsapp("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "ntfy":
            from .notifications import notify_ntfy
            notify_ntfy("MediaForge", "🔔 Test-Benachrichtigung erfolgreich!", username=username)
        elif service == "discord":
            if _sess.get("user_role") != "admin":
                return jsonify({"error": "admin access required"}), 403
            from .notifications import send_discord_sync
            from .db import get_setting as _gs
            import os as _os
            _wh_url = (_gs("notif_discord_webhook_url") or _os.environ.get("MEDIAFORGE_DISCORD_WEBHOOK", "")).strip()
            if not _wh_url:
                return jsonify({"error": "Kein Webhook konfiguriert"}), 400
            import json as _json
            _payload = {
                "embeds": [{
                    "title": "MediaForge — Test",
                    "color": 0x57F287,
                    "fields": [
                        {"name": "Status", "value": "Test erfolgreich ✅", "inline": True},
                    ],
                    "footer": {"text": "MediaForge"},
                }]
            }
            _code, _err = send_discord_sync(_wh_url, _payload)
            if _code in (200, 204):
                return jsonify({"ok": True, "http": _code})
            else:
                return jsonify({"error": f"Discord antwortete HTTP {_code}: {_err or ''}"}), 502
        else:
            return jsonify({"error": "unknown service"}), 400

        return jsonify({"ok": True})

    @app.route("/api/queue/<int:queue_id>", methods=["DELETE"])
    def api_queue_remove(queue_id):
        ok, err = remove_from_queue(queue_id)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})

    @app.route("/api/queue/<int:queue_id>/cancel", methods=["POST"])
    def api_queue_cancel(queue_id):
        ok, err = cancel_queue_item(queue_id)
        if not ok:
            return jsonify({"error": err}), 400
        # Signal the worker to kill the active subprocess immediately.
        with _active_cancel_events_lock:
            ev = _active_cancel_events.get(queue_id)
        if ev is not None:
            ev.set()
        return jsonify({"ok": True})

    @app.route("/api/queue/<int:queue_id>/restart", methods=["POST"])
    def api_queue_restart(queue_id):
        import json as _json
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] not in ("failed", "cancelled", "completed"):
            return jsonify({"error": "Only failed, cancelled or completed items can be restarted"}), 400

        # Prefer re-queuing only the failed episode URLs; fall back to full list
        try:
            errors = _json.loads(item.get("errors") or "[]")
            failed_urls = [e["url"] for e in errors if e.get("url")]
        except Exception:
            failed_urls = []

        if failed_urls:
            episodes = failed_urls
        else:
            try:
                episodes = _json.loads(item.get("episodes") or "[]")
            except Exception:
                return jsonify({"error": "Could not parse episode list"}), 500

        if not episodes:
            return jsonify({"error": "No episodes to restart"}), 400

        # Reset the existing row in-place (no new row created)
        ok, err = restart_queue_item_inplace(queue_id, episodes)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True, "queue_id": queue_id, "episodes": len(episodes)})

    @app.route("/api/queue/<int:queue_id>/skip-episode", methods=["POST"])
    def api_queue_skip_episode(queue_id):
        """Signal the worker to skip the current episode after its active attempt finishes."""
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] != "running":
            return jsonify({"error": "Job is not running"}), 400
        request_episode_skip(queue_id)
        return jsonify({"ok": True})

    @app.route("/api/queue/<int:queue_id>/retry-episode", methods=["POST"])
    def api_queue_retry_episode(queue_id):
        """Retry a single failed episode URL, preserving all other episode errors."""
        data = request.get_json(silent=True) or {}
        ep_url = data.get("url", "").strip()
        if not ep_url:
            return jsonify({"error": "Missing episode URL"}), 400
        item = get_queue_item(queue_id)
        if not item:
            return jsonify({"error": "Queue item not found"}), 404
        if item["status"] not in ("failed", "cancelled", "completed"):
            return jsonify({"error": "Only failed, cancelled or completed items support per-episode retry"}), 400
        ok, err = retry_single_episode(queue_id, ep_url)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})

    @app.route("/api/queue/<int:queue_id>/move", methods=["POST"])
    def api_queue_move(queue_id):
        data = request.get_json(silent=True) or {}
        direction = data.get("direction", "").strip()
        if direction not in ("up", "down"):
            return jsonify({"error": "direction must be 'up' or 'down'"}), 400
        ok, err = move_queue_item(queue_id, direction)
        if not ok:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})

    @app.route("/api/queue/completed", methods=["DELETE"])
    def api_queue_clear():
        clear_completed()
        return jsonify({"ok": True})

    @app.route("/library")
    def library_page():
        return render_template("library.html")

    @app.route("/settings")
    def settings_page():
        from pathlib import Path
        import platform

        env_path = Path.home() / ".mediaforge" / ".env"
        if platform.system() != "Windows":
            display = "~/.mediaforge/.env"
        else:
            display = str(env_path)
        return render_template("settings.html", env_path=display)

    @app.route("/integrations")
    def integrations_page():
        return render_template("integrations.html")

    @app.route("/syncplay")
    def syncplay_page():
        """Dedicated SyncPlay page. Guests reach this via an invite link; it is
        the only view they can see (the rest stays behind login)."""
        from .db import get_setting as _gs
        if _gs("syncplay_enabled", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        room = (request.args.get("room") or "").strip()
        return render_template("syncplay.html", invite_room=room)

    @app.route("/advanced-search")
    def advanced_search_page():
        from .db import get_setting
        if get_setting("cineinfo_advanced_search", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        return render_template(
            "advanced_search.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )



    @app.route("/calendar")
    def calendar_page():
        from .db import get_setting
        if get_setting("cineinfo_calendar", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        return render_template("calendar.html")

    @app.route("/encoding")
    def encoding_page():
        return render_template("encoding.html")

    @app.route("/api/encoding/settings", methods=["GET"])
    def api_encoding_settings_get():
        settings = {
            "mode":        get_setting("encoding_mode", "copy"),
            "audio_copy":  get_setting("encoding_audio_copy", "copy"),
            "hw_h264":     get_setting("encoding_hw_h264", "cpu"),
            "preset_h264": get_setting("encoding_preset_h264", "medium"),
            "crf_h264":    int(get_setting("encoding_crf_h264", "23") or "23"),
            "audio_h264":  get_setting("encoding_audio_h264", "copy"),
            "hw_h265":     get_setting("encoding_hw_h265", "cpu"),
            "preset_h265": get_setting("encoding_preset_h265", "medium"),
            "crf_h265":    int(get_setting("encoding_crf_h265", "28") or "28"),
            "audio_h265":  get_setting("encoding_audio_h265", "copy"),
            "expert_video": get_setting("encoding_expert_video", ""),
            "expert_audio": get_setting("encoding_expert_audio", ""),
            "vaapi_device": get_setting("encoding_vaapi_device", ""),
        }
        return jsonify({"ok": True, "settings": settings})

    @app.route("/api/encoding/settings", methods=["POST"])
    def api_encoding_settings_post():
        data = request.get_json(force=True) or {}
        mode = data.get("mode", "copy")
        valid_modes = ("copy", "h264", "h265", "expert")
        if mode not in valid_modes:
            return jsonify({"ok": False, "error": "Invalid mode"}), 400
        set_setting("encoding_mode", mode)
        if mode == "copy":
            audio = data.get("audio", "copy")
            if audio not in ("copy", "aac", "ac3"):
                audio = "copy"
            set_setting("encoding_audio_copy", audio)
        elif mode in ("h264", "h265"):
            hw = data.get("hw", "cpu")
            preset = data.get("preset", "medium")
            crf = str(int(data.get("crf", 23 if mode == "h264" else 28)))
            audio = data.get("audio", "copy")
            valid_presets = ("ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow")
            valid_hw = ("cpu","nvenc","vaapi","videotoolbox")
            valid_audio = ("copy","aac","ac3")
            if preset not in valid_presets: preset = "medium"
            if hw not in valid_hw: hw = "cpu"
            if audio not in valid_audio: audio = "copy"
            vaapi_device = data.get("vaapi_device", "")
            set_setting(f"encoding_hw_{mode}", hw)
            set_setting(f"encoding_preset_{mode}", preset)
            set_setting(f"encoding_crf_{mode}", crf)
            set_setting(f"encoding_audio_{mode}", audio)
            set_setting("encoding_vaapi_device", vaapi_device)
        elif mode == "expert":
            set_setting("encoding_expert_video", data.get("expert_video", ""))
            set_setting("encoding_expert_audio", data.get("expert_audio", ""))
        return jsonify({"ok": True})

    _detect_hw_cache: "dict | None" = None
    _detect_hw_cache_at: float = 0.0
    _detect_hw_cache_ttl: float = 3600.0   # re-probe at most once per hour
    _detect_hw_lock = threading.Lock()

    @app.route("/api/encoding/detect-hw", methods=["POST"])
    def api_encoding_detect_hw():
        import subprocess
        import sys
        import time as _t

        nonlocal _detect_hw_cache, _detect_hw_cache_at

        # Return cached result if still fresh — avoids repeated 12s probe runs
        with _detect_hw_lock:
            if _detect_hw_cache is not None and (_t.time() - _detect_hw_cache_at) < _detect_hw_cache_ttl:
                return jsonify({"ok": True, "encoders": _detect_hw_cache, "cached": True})

        vaapi_device = get_setting("encoding_vaapi_device", "") or "/dev/dri/renderD128"

        def _reason_from_output(output: str, encoder: str) -> str:
            """Parse FFmpeg stderr and return a human-readable reason why an encoder failed."""
            o = output.lower()
            if encoder in ("h264_nvenc", "hevc_nvenc"):
                if "cannot load libcuda" in o or "libcuda.so" in o:
                    return "NVIDIA-Treiber nicht gefunden. Bei Docker: Container mit --gpus all starten."
                if "no nvenc capable devices" in o:
                    return "Keine NVIDIA-GPU gefunden oder NVENC nicht unterstützt."
                if "driver does not support" in o:
                    return "NVIDIA-Treiber zu alt — bitte aktualisieren."
                if "device or resource busy" in o:
                    return "GPU ist vollständig ausgelastet."
            if encoder in ("h264_vaapi", "hevc_vaapi"):
                if "no such file or directory" in o or "cannot open" in o:
                    return f"VAAPI-Gerät nicht gefunden ({vaapi_device}). Bei Docker: /dev/dri mounten."
                if "permission denied" in o:
                    return f"Kein Zugriff auf {vaapi_device}. Benutzer zur video-Gruppe hinzufügen."
                if "no va display" in o or "failed to initialise" in o:
                    return "VAAPI konnte nicht initialisiert werden — Treiber prüfen."
            if encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
                if sys.platform != "darwin":
                    return "Nur auf macOS verfügbar."
            if "unknown encoder" in o or "encoder not found" in o or "no such encoder" in o:
                return "FFmpeg wurde ohne Support für diesen Encoder kompiliert."
            return "Encoder nicht verfügbar."

        def _probe(encoder: str, cmd: list, results: dict):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                output = r.stdout + r.stderr
                if r.returncode == 0:
                    results[encoder] = {"available": True, "reason": None}
                else:
                    results[encoder] = {
                        "available": False,
                        "reason": _reason_from_output(output, encoder),
                    }
            except FileNotFoundError:
                results[encoder] = {"available": False, "reason": "ffmpeg nicht gefunden."}
            except subprocess.TimeoutExpired:
                results[encoder] = {"available": False, "reason": "Timeout — Encoder reagiert nicht."}
            except Exception as exc:
                results[encoder] = {"available": False, "reason": str(exc)}

        _null = ["ffmpeg", "-f", "lavfi", "-i", "nullsrc=size=128x128:rate=1",
                 "-frames:v", "1", "-an"]

        probes = {
            "libx264":            _null + ["-c:v", "libx264",            "-f", "null", "-"],
            "libx265":            _null + ["-c:v", "libx265",            "-f", "null", "-"],
            "h264_nvenc":         _null + ["-c:v", "h264_nvenc",         "-f", "null", "-"],
            "hevc_nvenc":         _null + ["-c:v", "hevc_nvenc",         "-f", "null", "-"],
            "h264_vaapi":         ["ffmpeg", "-vaapi_device", vaapi_device,
                                   "-f", "lavfi", "-i", "nullsrc=size=128x128:rate=1",
                                   "-frames:v", "1", "-an",
                                   "-vf", "format=nv12,hwupload",
                                   "-c:v", "h264_vaapi", "-f", "null", "-"],
            "hevc_vaapi":         ["ffmpeg", "-vaapi_device", vaapi_device,
                                   "-f", "lavfi", "-i", "nullsrc=size=128x128:rate=1",
                                   "-frames:v", "1", "-an",
                                   "-vf", "format=nv12,hwupload",
                                   "-c:v", "hevc_vaapi", "-f", "null", "-"],
            "h264_videotoolbox":  _null + ["-c:v", "h264_videotoolbox",  "-f", "null", "-"],
            "hevc_videotoolbox":  _null + ["-c:v", "hevc_videotoolbox",  "-f", "null", "-"],
        }

        results = {}
        threads = []
        for enc, cmd in probes.items():
            t = threading.Thread(target=_probe, args=(enc, cmd, results))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=12)

        # Cache results so the next request is instant
        import time as _t2
        with _detect_hw_lock:
            _detect_hw_cache = results
            _detect_hw_cache_at = _t2.time()

        return jsonify({"ok": True, "encoders": results, "cached": False})


    # =========================================================
    # Upscale Queue API
    # =========================================================

    @app.route("/api/upscale/queue")
    def api_upscale_queue():
        items = get_upscale_queue()
        badge = get_upscale_badge_count()
        return jsonify({"ok": True, "items": items, "badge": badge})

    @app.route("/api/upscale/progress")
    def api_upscale_progress():
        try:
            from ..anime4k.anime4k import get_upscale_progress
            return jsonify({"ok": True, "progress": get_upscale_progress()})
        except Exception:
            return jsonify({"ok": True, "progress": {"active": False, "percent": 0}})

    @app.route("/api/upscale/badge")
    def api_upscale_badge():
        return jsonify({"ok": True, "count": get_upscale_badge_count()})

    @app.route("/api/upscale/queue/<int:item_id>", methods=["DELETE"])
    def api_upscale_queue_delete(item_id):
        ok, err = remove_from_upscale_queue(item_id)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400

    @app.route("/api/upscale/queue/<int:item_id>/cancel", methods=["POST"])
    def api_upscale_cancel(item_id):
        ok, err = cancel_upscale_item(item_id)
        if ok:
            with _upscale_cancel_lock:
                ev = _upscale_active_cancel_events.get(item_id)
            if ev:
                ev.set()
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400

    @app.route("/api/upscale/queue/clear", methods=["POST"])
    def api_upscale_clear():
        clear_upscale_completed()
        return jsonify({"ok": True})

    @app.route("/api/upscale/queue/<int:item_id>/move", methods=["POST"])
    def api_upscale_move(item_id):
        data = request.get_json(force=True, silent=True) or {}
        direction = data.get("direction", "up")
        ok, err = move_upscale_queue_item(item_id, direction)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400

    @app.route("/api/upscale/add-library", methods=["POST"])
    def api_upscale_add_library():
        """Add library files to the upscale queue as ONE batch entry."""
        data = request.get_json(force=True) or {}
        files = data.get("files", [])  # list of {title, path}
        if not files:
            return jsonify({"ok": False, "error": "Keine Dateien angegeben"}), 400
        replace = get_setting("upscaling_replace_original", "1") == "1"
        from pathlib import Path as _Path
        import json as _json
        valid = []
        title = None
        for f in files:
            fp = _Path(f.get("path", ""))
            if not fp.exists():
                continue
            if replace:
                out = str(fp)
            else:
                out = str(fp.with_name(fp.stem + " (upscale).mkv"))
            valid.append({"file_path": str(fp), "output_path": out})
            if title is None:
                # Use the series title (strip episode suffix)
                t = f.get("title", fp.stem)
                title = t.split(" – ")[0].strip() if " – " in t else t
        if not valid:
            return jsonify({"ok": False, "error": "Keine Dateien gefunden"}), 400
        add_to_upscale_queue(
            title=title or "Unbekannt",
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="library",
            files=valid if len(valid) > 1 else None,
        )
        return jsonify({"ok": True, "added": len(valid)})


    @app.route("/api/upscale/settings", methods=["GET"])
    def api_upscale_settings_get():
        return jsonify({
            "ok": True,
            "settings": {
                "mode":            get_setting("upscaling_mode", "disabled"),
                "engine":          get_setting("upscaling_engine", "auto"),
                "shader_preset":   get_setting("upscaling_shader_preset", "B"),
                "shader_quality":  get_setting("upscaling_shader_quality", "high"),
                "resolution":      get_setting("upscaling_resolution", "1080p"),
                "replace_original":get_setting("upscaling_replace_original", "1"),
                "out_vcodec":      get_setting("upscaling_out_vcodec", "libx264"),
                "out_crf":         get_setting("upscaling_out_crf", "18"),
                "out_preset":      get_setting("upscaling_out_preset", "medium"),
            }
        })

    @app.route("/api/upscale/settings", methods=["POST"])
    def api_upscale_settings_post():
        data = request.get_json(force=True) or {}
        valid_modes    = ("disabled", "during_download", "after_download")
        valid_engines  = ("auto", "mpv", "libplacebo")
        valid_presets  = ("A", "B", "C", "D")
        valid_quality  = ("high", "low")
        valid_res      = ("1080p", "1440p", "4k", "source")
        valid_vcodec   = ("libx264", "libx265", "copy")
        valid_enc_pre  = ("ultrafast","superfast","veryfast","faster","fast",
                          "medium","slow","slower","veryslow")

        def _v(key, valid, default):
            val = data.get(key, default)
            return val if val in valid else default

        set_setting("upscaling_mode",             _v("mode", valid_modes, "disabled"))
        set_setting("upscaling_engine",           _v("engine", valid_engines, "auto"))
        set_setting("upscaling_shader_preset",    _v("shader_preset", valid_presets, "B"))
        set_setting("upscaling_shader_quality",   _v("shader_quality", valid_quality, "high"))
        set_setting("upscaling_resolution",       _v("resolution", valid_res, "1080p"))
        set_setting("upscaling_replace_original", "1" if data.get("replace_original", True) else "0")
        set_setting("upscaling_out_vcodec",       _v("out_vcodec", valid_vcodec, "libx264"))
        crf = str(max(0, min(51, int(data.get("out_crf", 18)))))
        set_setting("upscaling_out_crf",    crf)
        set_setting("upscaling_out_preset", _v("out_preset", valid_enc_pre, "medium"))
        return jsonify({"ok": True})

    @app.route("/api/upscale/mpv-status", methods=["GET"])
    def api_upscale_mpv_status():
        try:
            from ..autodeps import get_mpv_download_status, _bundled_mpv
            import shutil
            present = bool(_bundled_mpv() or shutil.which("mpv"))
            dl = get_mpv_download_status()
            return jsonify({"ok": True, "present": present, "download": dl})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/upscale/check-engines", methods=["POST"])
    def api_upscale_check_engines():
        try:
            from ..anime4k.anime4k import get_available_engines, list_available_shaders
            engines  = get_available_engines()
            shaders  = list_available_shaders("high") + list_available_shaders("low")
            shaders  = sorted(set(shaders))
            return jsonify({"ok": True, "engines": engines, "shaders_available": len(shaders) > 0})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/upscale/download-shaders", methods=["POST"])
    def api_upscale_download_shaders():
        """Download Anime4K GLSL shader pack in background."""
        data     = request.get_json(force=True) or {}
        quality  = data.get("quality", "high")
        if quality not in ("high", "low"):
            return jsonify({"ok": False, "error": "quality must be high or low"}), 400

        def _dl():
            try:
                from ..anime4k.anime4k import download_anime4k, extract_anime4k
                files = download_anime4k(mode=quality)
                extract_anime4k(files)
                logger.info(f"[Anime4K] Shader-Download abgeschlossen ({quality})")
            except Exception as exc:
                logger.error(f"[Anime4K] Shader-Download fehlgeschlagen: {exc}")

        threading.Thread(target=_dl, daemon=True).start()
        return jsonify({"ok": True, "message": "Download gestartet"})

    @app.route("/notifications")
    def notifications_page():
        from flask import session as _sess
        # In no-auth mode user_role is set to "admin" by before_request,
        # so this works correctly for both auth and no-auth.
        return render_template(
            "notifications.html",
            is_admin=(_sess.get("user_role") == "admin"),
        )

    @app.route("/api/random")
    def api_random():
        site = request.args.get("site", "aniworld").strip()
        if site == "sto":
            return jsonify({"error": "Random is not available for S.TO"}), 400
        url = random_anime()
        if url:
            return jsonify({"url": url})
        return jsonify({"error": "Failed to fetch random anime"}), 500

    # TTL cache for browse endpoints — in-memory + SQLite persistence
    import time as _time
    from collections import OrderedDict as _OD

    _BROWSE_CACHE_MAX = 50     # hard cap; evicts LRU entry when exceeded
    _browse_cache: "_OD" = _OD()
    _BROWSE_TTL = 3600  # 1 hour
    _browse_refresh_locks: dict = {}
    _browse_refresh_mutex = threading.Lock()

    def _browse_cache_set(k, v):
        """Insert/update key with LRU eviction when the cap is reached."""
        _browse_cache.pop(k, None)      # move to end on update
        _browse_cache[k] = v
        while len(_browse_cache) > _BROWSE_CACHE_MAX:
            _browse_cache.popitem(last=False)  # evict oldest

    def _cached_browse(key, fetch_fn):
        now = _time.time()
        # 1. In-memory fast path
        entry = _browse_cache.get(key)
        if entry and now - entry[0] < _BROWSE_TTL:
            return entry[1]

        # 2. If nothing in memory, try SQLite (survives restarts)
        if entry is None:
            db_row = get_browse_cache_stale(key)
            if db_row:
                data, cached_at = db_row
                _browse_cache_set(key, (cached_at, data))
                entry = _browse_cache[key]

        # 3. Still fresh after DB load?
        if entry and now - entry[0] < _BROWSE_TTL:
            return entry[1]

        # 4. Stale or missing — avoid duplicate concurrent refreshes
        with _browse_refresh_mutex:
            already_refreshing = key in _browse_refresh_locks
            if not already_refreshing:
                _browse_refresh_locks[key] = True

        if entry is not None:
            # Stale-while-revalidate: serve old data immediately, refresh in background
            if not already_refreshing:
                def _bg_refresh(k=key, fn=fetch_fn):
                    try:
                        results = fn()
                        if results:
                            _browse_cache_set(k, (_time.time(), results))
                            set_browse_cache(k, results)
                    finally:
                        with _browse_refresh_mutex:
                            _browse_refresh_locks.pop(k, None)
                threading.Thread(target=_bg_refresh, daemon=True,
                                 name=f"browse-refresh-{key}").start()
            return entry[1]

        # 5. Cold start — no cached data at all; fetch in a background thread and
        #    wait up to 10 s so the request thread is not blocked indefinitely.
        _cold_done = threading.Event()
        _cold_result = [None]

        def _cold_fetch(k=key, fn=fetch_fn, ev=_cold_done, out=_cold_result):
            try:
                r = fn()
                if r is not None:
                    _browse_cache_set(k, (_time.time(), r))
                    set_browse_cache(k, r)
                    out[0] = r
            finally:
                ev.set()
                with _browse_refresh_mutex:
                    _browse_refresh_locks.pop(k, None)

        threading.Thread(target=_cold_fetch, daemon=True,
                         name=f"browse-cold-{key}").start()
        _cold_done.wait(timeout=10)
        return _cold_result[0]

    @app.route("/api/new-animes")
    def api_new_animes():
        results = _cached_browse("new_animes", fetch_new_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch new animes"}), 500
        return jsonify({"results": _proxy_result_list(results)})

    @app.route("/api/popular-animes")
    def api_popular_animes():
        results = _cached_browse("popular_animes", fetch_popular_animes)
        if results is None:
            return jsonify({"error": "Failed to fetch popular animes"}), 500
        return jsonify({"results": _proxy_result_list(results)})

    @app.route("/api/new-series")
    def api_new_series():
        results = _cached_browse("new_series", fetch_new_series)
        if results is None:
            return jsonify({"error": "Failed to fetch new series"}), 500
        return jsonify({"results": _proxy_result_list(results)})

    @app.route("/api/popular-series")
    def api_popular_series():
        results = _cached_browse("popular_series", fetch_popular_series)
        if results is None:
            return jsonify({"error": "Failed to fetch popular series"}), 500
        return jsonify({"results": _proxy_result_list(results)})

    def _fetch_new_movies():
        """Scrape the FilmPalast homepage for new movies (filters out SxxExx series episodes)."""
        import re as _re2
        import requests as _req
        series_re = _re2.compile(r"\bS\d{2}E\d{2}\b", _re2.IGNORECASE)
        try:
            resp = _req.get(
                "https://filmpalast.to/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "de-DE,de;q=0.9",
                },
                timeout=15,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            logger.warning("FilmPalast new-movies scrape failed: %s", exc)
            return None

        anchors = _re2.findall(
            r'<a\s+href="//filmpalast\.to/stream/([a-zA-Z0-9\-]+)"\s+title="([^"]+)"',
            html,
        )
        imgs = _re2.findall(r'<img\s+src="(/files/movies/[^"]+)"', html)

        results = []
        seen = set()
        for i, (slug, title) in enumerate(anchors):
            if series_re.search(title):
                continue  # skip series episodes like "Show S04E01"
            url = f"https://filmpalast.to/stream/{slug}"
            if url in seen:
                continue
            seen.add(url)
            poster = f"https://filmpalast.to{imgs[i]}" if i < len(imgs) else ""
            results.append({"title": title, "url": url, "poster_url": poster, "genre": ""})
        return results

    @app.route("/api/new-movies")
    def api_new_movies():
        results = _cached_browse("new_movies", _fetch_new_movies)
        if results is None:
            return jsonify({"error": "Failed to fetch new movies"}), 500
        return jsonify({"results": _proxy_result_list(results)})

    @app.route("/api/downloaded-folders")
    def api_downloaded_folders():
        from pathlib import Path
        # If MediaScan is active and using a media-server source,
        # signal the frontend to skip the folder check entirely.
        ms_enabled = get_setting("mediascan_enabled", "0") == "1"
        ms_source  = get_setting("mediascan_source",  "") or ""
        if ms_enabled and ms_source and ms_source != "folders":
            return jsonify({"folders": [], "source": "mediascan", "mediascan_source": ms_source})


        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            dl_path = p
        else:
            dl_path = Path.home() / "Downloads"

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        lang_folders = ["german-dub", "english-sub", "german-sub", "english-dub"]

        # Collect all paths to scan (default + custom)
        scan_roots = [dl_path]
        for cp in get_custom_paths():
            cp_path = Path(cp["path"]).expanduser()
            if not cp_path.is_absolute():
                cp_path = Path.home() / cp_path
            scan_roots.append(cp_path)

        folders = set()
        for root in scan_roots:
            if lang_sep:
                bases = [root / lf for lf in lang_folders]
            else:
                bases = [root]
            for base in bases:
                if not base.is_dir():
                    continue
                for entry in base.iterdir():
                    if entry.is_dir():
                        folders.add(entry.name)
        return jsonify({"folders": sorted(folders)})

    @app.route("/api/update-check", methods=["GET", "POST"])
    def api_update_check():
        import time
        data = request.get_json(silent=True) or {}
        force = data.get("force", False)
        stale = (time.time() - _update_cache["checked_at"]) > _UPDATE_CHECK_INTERVAL
        if force or stale:
            _do_update_check()
        _inst = selfupdate.detect_install()
        return jsonify({
            "local_version": app_version,
            "latest_version": _update_cache["latest_version"],
            "update_available": _update_cache["update_available"],
            "release_url": _update_cache["release_url"],
            "release_notes": _update_cache["release_notes"],
            "checked_at": _update_cache["checked_at"],
            "error": _update_cache["error"],
            "is_dev_install": _update_cache["is_dev_install"],
            "install_type": _inst["type"],
            "channel": _inst["channel"],
            "can_self_update": _inst["can_self_update"],
        })

    @app.route("/api/update/install", methods=["POST"])
    def api_update_install():
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        if channel is not None:
            channel = str(channel).strip().lower()
            if channel not in ("stable", "dev"):
                return jsonify({"error": "invalid channel"}), 400
        try:
            result = selfupdate.start_update(target_channel=channel)
        except selfupdate.UpdateError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("[SelfUpdate] start failed")
            return jsonify({"error": str(exc)}), 500

        # Pause running downloads so they resume after the restart.
        try:
            from .db import get_db
            _c = get_db()
            try:
                _c.execute("UPDATE download_queue SET status = 'queued' WHERE status = 'running'")
                _c.commit()
            finally:
                _c.close()
        except Exception:
            logger.warning("[SelfUpdate] could not pause download queue", exc_info=True)

        # Flush the response, then exit so the helper can replace files & relaunch.
        def _exit_soon():
            import time as _t
            _t.sleep(1.5)
            logger.info("[SelfUpdate] exiting for update helper")
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True, name="selfupdate-exit").start()
        return jsonify(result)

    @app.route("/api/update/status", methods=["GET"])
    def api_update_status():
        return jsonify(selfupdate.read_status())

    @app.route("/api/update/status/ack", methods=["POST"])
    def api_update_status_ack():
        selfupdate.ack_status()
        return jsonify({"ok": True})

    @app.route("/api/user/language", methods=["POST"])
    def api_user_language():
        """Save the current user's UI language preference (EN/DE)."""
        from flask import session as _sess
        from .db import set_user_language as _set_lang
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
        filmpalast_movie_subfolder = get_setting("filmpalast_movie_subfolder", "0")
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
        return jsonify(
            {
                "download_path":             resolved,
                "lang_separation":           lang_separation,
                "disable_english_sub":       disable_english_sub,
                "filmpalast_movie_subfolder": filmpalast_movie_subfolder,
                "sync_schedule":             sync_schedule,
                "sync_mode":                 sync_mode,
                "sync_days":                 sync_days,
                "sync_times":                sync_times,
                "sync_language":              sync_language,
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
                "syncplay_enabled":          get_setting("syncplay_enabled", "0"),
                "auto_update_enabled":       get_setting("auto_update_enabled", "0"),
                "auto_update_days":          get_setting("auto_update_days", "0,1,2,3,4,5,6"),
                "auto_update_time":          get_setting("auto_update_time", "03:00"),
                "seerr_url":                 get_setting("seerr_url", ""),
                "seerr_api_key":             get_setting("seerr_api_key", ""),
                "seerr_configured":          bool(get_setting("seerr_url", "").strip() and get_setting("seerr_api_key", "").strip()),
                "dns_mode":                  get_setting("dns_mode", "system"),
                "dns_server":                get_setting("dns_server", ""),
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
                }
            }
        )

    @app.route("/api/console", methods=["GET"])
    def api_console():
        """Read-only tail of the live console output (admin only).

        Gated behind the ``web_console`` setting so the buffer is never exposed
        unless the feature is explicitly enabled.
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
        from .console_capture import get_console_output
        out = get_console_output(after)
        out["enabled"] = True
        return jsonify(out)

    @app.route("/api/settings/seerr", methods=["PUT"])
    def api_settings_seerr():
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
        """Return current SSO / OIDC configuration from DB (secrets masked)."""
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
        """Save SSO / OIDC configuration to DB and apply immediately."""
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
            "advanced_search": get_setting("cineinfo_advanced_search", "0"),
            "calendar":        get_setting("cineinfo_calendar",        "0"),
            "calendar_seerr":  get_setting("cineinfo_calendar_seerr",  "0"),
            "calendar_mediathek": get_setting("cineinfo_calendar_mediathek", "0"),
            "calendar_refresh_interval": get_setting("cineinfo_calendar_refresh_interval", "24"),
        })

    @app.route("/api/settings/cineinfo", methods=["PUT"])
    def api_settings_cineinfo_put():
        data = request.get_json(silent=True) or {}
        old_key = get_setting("cineinfo_tmdb_api_key", "")
        old_country = get_setting("cineinfo_country", "DE")

        for key in ["tmdb_api_key", "country", "show_providers",
                    "show_genres", "show_fsk", "show_rating", "show_recommendations", "show_trailer",
                    "show_hover_rating", "show_hover_genres", "show_hover_fsk", "advanced_search",
                    "calendar", "calendar_seerr", "calendar_mediathek", "calendar_refresh_interval"]:
            if key in data:
                set_setting("cineinfo_" + key, str(data[key]))

        new_key = get_setting("cineinfo_tmdb_api_key", "")
        new_country = get_setting("cineinfo_country", "DE")
        if new_key != old_key or new_country != old_country:
            clear_tmdb_cache()

        return jsonify({"ok": True})

    # ── Crunchyroll integration (sub-section of CineInfo) ────────────
    @app.route("/api/settings/crunchyroll", methods=["GET"])
    def api_settings_crunchyroll_get():
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
            from . import crunchyroll_service
            crunchyroll_service.invalidate_client()
        except Exception:
            logger.debug("[Crunchyroll] could not invalidate client", exc_info=True)

        # Drop the cached CR calendar targets so toggling simulcast/watchlist/lists
        # takes effect on the next /api/calendar call instead of after the TTL.
        global _cr_calendar_ids, _cr_calendar_meta, _cr_calendar_titles, _cr_targets_built_at
        _cr_calendar_ids, _cr_calendar_meta, _cr_calendar_titles = [], {}, {}
        _cr_targets_built_at = 0.0

        return jsonify({"ok": True})

    @app.route("/api/settings/crunchyroll/test", methods=["POST"])
    def api_settings_crunchyroll_test():
        """Validate Crunchyroll credentials from the settings UI.

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
            from . import crunchyroll_service
            result = crunchyroll_service.test_connection(email, password, locale, anon, profile_id)
        except Exception as exc:
            logger.debug("[Crunchyroll] test endpoint error: %s", exc)
            result = {"ok": False, "error": "unknown", "detail": str(exc)}
        return jsonify(result)

    @app.route("/api/settings/crunchyroll/profiles", methods=["GET"])
    def api_settings_crunchyroll_profiles():
        """Return the account's Crunchyroll profiles for the settings selector."""
        try:
            from . import crunchyroll_service
            return jsonify({"profiles": crunchyroll_service.list_account_profiles()})
        except Exception as exc:
            logger.debug("[Crunchyroll] profiles endpoint error: %s", exc)
            return jsonify({"profiles": []})

    @app.route("/api/crunchyroll/availability", methods=["GET"])
    def api_crunchyroll_availability():
        """Return whether a title is available on Crunchyroll (cached).

        Powers the extra "Crunchyroll" provider pill — useful for fresh
        simulcasts that TMDB's provider data hasn't picked up yet.
        """
        title = (request.args.get("title") or "").strip()
        if not title:
            return jsonify({"available": False, "reason": "no_title"})
        try:
            from . import crunchyroll_service
            if not crunchyroll_service.is_enabled() or get_setting("crunchyroll_show_providers", "1") != "1":
                return jsonify({"available": False, "reason": "disabled"})
            return jsonify({"available": bool(crunchyroll_service.is_available(title))})
        except Exception as exc:
            logger.debug("[Crunchyroll] availability endpoint error: %s", exc)
            return jsonify({"available": False, "reason": "error"})



    @app.route("/api/settings/env-file", methods=["GET"])
    def api_settings_env_file_get():
        """Check whether the legacy .env file still exists and has been migrated."""
        from pathlib import Path as _Path
        env_path = _Path.home() / ".mediaforge" / ".env"
        return jsonify({
            "exists":   env_path.exists(),
            "migrated": get_setting("env_migrated") == "1",
        })

    @app.route("/api/settings/env-file", methods=["DELETE"])
    def api_settings_env_file_delete():
        """Delete the legacy .env file after migration."""
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
        """Report whether data from a previous AniWorld install can be imported."""
        from ..legacy_import import detect_legacy
        status = detect_legacy()
        status["dismissed"] = get_setting("legacy_import_dismissed", "0") == "1"
        return jsonify(status)

    @app.route("/api/settings/legacy-import", methods=["POST"])
    def api_settings_legacy_import_run():
        """Manually import data from a previous AniWorld install (~/.aniworld).

        Non-destructive: an existing database is never overwritten (data is
        imported automatically on first start before the DB is created). This
        endpoint fills in any auxiliary files that are still missing."""
        from ..legacy_import import detect_legacy, run_import
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
        """Permanently hide the legacy-import card (already imported / not wanted)."""
        set_setting("legacy_import_dismissed", "1")
        return jsonify({"ok": True})

    @app.route("/api/settings/mediaplayer", methods=["GET"])
    def api_settings_mediaplayer_get():
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
        """Quick connectivity test: try to reach the configured mediaplayer."""
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
        """Poll whether the configured media server is currently scanning its library."""
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


    # ── Plex OAuth proxy (avoids CORS in the browser) ──────────────────────
    _PLEX_CLIENT_ID = "mediaforge-downloader"
    _PLEX_PRODUCT   = "MediaForge"

    @app.route("/api/settings/mediaplayer/scan", methods=["POST"])
    def api_mediaplayer_scan():
        """Manually trigger a library scan/refresh on the configured media server."""
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
        """Create a Plex OAuth pin and return {id, code, auth_url}."""
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
        """Poll Plex for the auth token of a pin. Returns {token} once authorized."""
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
        """Fetch library sections from the configured Plex server."""
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

    # ---------------------------------------------------------------------------
    #  MediaScan endpoints
    # ---------------------------------------------------------------------------

    @app.route("/api/settings/mediascan", methods=["GET"])
    def api_settings_mediascan_get():
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
            "plex_token_masked": (plex_token[:4] + "\u2022\u2022\u2022\u2022" + plex_token[-4:]) if len(plex_token) >= 8 else "",
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
        """Return first 50 cache entries for diagnosing match issues."""
        from .db import get_db as _get_db
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

    # ---------------------------------------------------------------------------
    # Shared TMDB lookup helper — used by API endpoint AND background prefetch
    # ---------------------------------------------------------------------------
    import requests as _rq_tmdb

    # Token-bucket rate limiter. TMDB has no hard rate limit (recommended <= ~50
    # req/s); we cap the whole app at 40 req/s to leave headroom for the other
    # TMDB users in the project (prefetch worker, batch endpoint, modal lookups).
    class _TmdbRateLimiter:
        def __init__(self, rate=3.0):
            self._rate = rate
            self._tokens = float(rate)
            self._last = time.monotonic()
            self._lock = threading.Lock()

        def acquire(self):
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    wait = 0.0
                else:
                    wait = (1.0 - self._tokens) / self._rate
                    self._tokens = 0.0
            if wait > 0:
                time.sleep(wait)

    _tmdb_rl = _TmdbRateLimiter(rate=40.0)

    # In-flight deduplication — prevents duplicate concurrent TMDB lookups
    # for the same cache_key (e.g. two cards with the same title loading at once).
    _tmdb_inflight: dict = {}
    _tmdb_inflight_lock = threading.Lock()

    def _tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang="de"):
        """
        Look up TMDB data for a title/IMDB-ID and cache in SQLite for 24 h.
        Lookup order:
          1. SQLite cache hit (checks BOTH the imdb_id key AND the title key)
          2. /find/{imdb_id}?external_source=imdb_id  (if imdb_id given)
          3. /search/multi?query={title}              (fallback)
        Results are stored under both keys so card (title) and modal (imdb_id)
        lookups always find the same cached entry.
        Returns dict with keys: found, tmdb_id, media_type, genres, providers, fsk
        """
        _lang_suffix = "|||" + ui_lang
        imdb_key  = (imdb_id + "|||" + country + _lang_suffix) if imdb_id else None
        title_key = (title   + "|||" + country + _lang_suffix) if title   else None

        # Check both cache keys — whichever was written first wins
        for ck in filter(None, [imdb_key, title_key]):
            cached = get_tmdb_cache(ck)
            if cached is not None:
                # Force refresh if missing new keys (trailers/recommendations/title/overview)
                if not cached.get("found", True) or ("trailer_key" in cached and "recommendations" in cached and "title" in cached and "overview" in cached and "title_confident" in cached):
                    # Warm the other key so next call is also a hit
                    other = title_key if ck == imdb_key else imdb_key
                    if other and get_tmdb_cache(other) is None:
                        set_tmdb_cache(other, cached)
                    return cached

        cache_key = imdb_key or title_key  # primary key for the fresh lookup

        # In-flight deduplication: if another thread is already fetching this
        # title, wait for it to finish then return whatever it cached.
        with _tmdb_inflight_lock:
            inflight_ev = _tmdb_inflight.get(cache_key)
            if inflight_ev is None:
                my_ev = threading.Event()
                _tmdb_inflight[cache_key] = my_ev
            else:
                my_ev = None  # we are the waiter

        if inflight_ev is not None:
            # Another thread is doing the lookup — poll the cache in short intervals
            # so we return as soon as the leader writes the result, up to 30 s total.
            _deadline = time.time() + 30
            while time.time() < _deadline:
                _remaining = max(0.0, _deadline - time.time())
                inflight_ev.wait(timeout=min(2.0, _remaining))
                for ck in filter(None, [imdb_key, title_key]):
                    cached = get_tmdb_cache(ck)
                    if cached is not None and "trailer_key" in cached and "recommendations" in cached:
                        return cached
                if inflight_ev.is_set():
                    break  # leader finished but nothing in cache — don't keep waiting
            return {"found": False}

        def _call(path, extra=None):
            lang = "en-US" if ui_lang == "en" else "de-DE"
            params = {"api_key": api_key}
            if extra is None or "language" not in extra:
                params["language"] = lang
            if extra:
                params.update(extra)
            _tmdb_rl.acquire()  # respect the global rate limit (40 req/s)
            r = _rq_tmdb.get(
                "https://api.themoviedb.org/3" + path,
                params=params, timeout=8,
                headers={"User-Agent": "MediaForge/1.0"},
            )
            r.raise_for_status()
            return r.json()

        try:
            tid = None
            mt  = None
            # 1. Direct IMDB-ID lookup (most accurate)
            if imdb_id:
                try:
                    find_data = _call("/find/" + imdb_id, {"external_source": "imdb_id"})
                    for media_type, key in (("tv", "tv_results"), ("movie", "movie_results")):
                        hits = find_data.get(key, [])
                        if hits:
                            tid = hits[0]["id"]
                            mt  = media_type
                            break
                except Exception as _fe:
                    logger.debug("[CineInfo] /find by imdb_id %r failed: %s", imdb_id, _fe)
            # 2. Fall back to title search
            if tid is None:
                if not title:
                    out = {"found": False}
                    set_tmdb_cache(cache_key, out)
                    return out
                search = _call("/search/multi", {"query": title})
                results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                
                # Fallback 1: Try without single quotes/apostrophes (e.g. "I'll" -> "Ill" or "I ll")
                if not results:
                    import re
                    # Sometimes TMDB prefers the word without the apostrophe entirely, or with a space.
                    # We'll try removing them first.
                    clean_title = re.sub(r"['’´`]", "", title)
                    if clean_title != title:
                        search = _call("/search/multi", {"query": clean_title})
                        results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                
                # Fallback 2: Try removing other special punctuation that might differ (like '!', '?', ':')
                if not results:
                    clean_title_2 = re.sub(r"[!\?:;]", "", title)
                    if clean_title_2 != title:
                        search = _call("/search/multi", {"query": clean_title_2})
                        results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                        
                # Fallback 3: Try removing (Year) tags like (2026)
                if not results:
                    clean_title_3 = re.sub(r"\s*\(\d{4}\)", "", title).strip()
                    if clean_title_3 != title:
                        search = _call("/search/multi", {"query": clean_title_3})
                        results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]
                        
                # Fallback 4: Split by hyphen and use first part (e.g. "Occupied - Die Besatzung" -> "Occupied")
                if not results and ("-" in title or "–" in title):
                    clean_title_4 = re.split(r"[-–]", title)[0].strip()
                    if clean_title_4 and clean_title_4 != title:
                        search = _call("/search/multi", {"query": clean_title_4})
                        results = [r for r in search.get("results", []) if r.get("media_type") in ("movie", "tv")]

                if not results:
                    out = {"found": False}
                    set_tmdb_cache(cache_key, out)
                    return out
                best = results[0]
                tid  = best["id"]
                mt   = best["media_type"]
            # 3. Fetch details, watch-providers, FSK
            details  = _call("/" + mt + "/" + str(tid))
            genres   = [g["name"] for g in details.get("genres", [])]
            wp_data  = _call("/" + mt + "/" + str(tid) + "/watch/providers")
            c_data   = wp_data.get("results", {}).get(country, {})
            flatrate = [p["provider_name"] for p in c_data.get("flatrate", [])]
            buy_list = [p["provider_name"] for p in c_data.get("buy", [])]
            rent_list= [p["provider_name"] for p in c_data.get("rent", [])]
            providers = flatrate[:]
            for p in buy_list + rent_list:
                if p not in providers:
                    providers.append(p)
            fsk = ""
            try:
                if mt == "tv":
                    cr = _call("/tv/" + str(tid) + "/content_ratings")
                    for r in cr.get("results", []):
                        if r.get("iso_3166_1") == country:
                            fsk = r.get("rating", "")
                            break
                else:
                    rd = _call("/movie/" + str(tid) + "/release_dates")
                    for entry in rd.get("results", []):
                        if entry.get("iso_3166_1") == country:
                            for rdate in entry.get("release_dates", []):
                                c = rdate.get("certification", "")
                                if c:
                                    fsk = c
                                    break
                            break
            except Exception:
                pass

            # 4. Fetch Trailers (Videos) - Fetch ALL languages first
            trailer_key = ""
            try:
                # Omit language to get all available videos
                videos = _call("/" + mt + "/" + str(tid) + "/videos", {"language": ""}) 
                results = videos.get("results", [])
                
                # Priority 1: German Trailer
                for v in results:
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("iso_639_1") == "de":
                        trailer_key = v.get("key")
                        break
                
                # Priority 2: English Trailer
                if not trailer_key:
                    for v in results:
                        if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("iso_639_1") == "en":
                            trailer_key = v.get("key")
                            break
                            
                # Priority 3: Any Trailer
                if not trailer_key:
                    for v in results:
                        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                            trailer_key = v.get("key")
                            break
            except Exception:
                pass

            if not trailer_key:
                try:
                    videos = _call("/" + mt + "/" + str(tid) + "/videos", {"language": "en-US"})
                    for v in videos.get("results", []):
                        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                            trailer_key = v.get("key", "")
                            break
                except Exception:
                    pass

            # 5. Fetch Recommendations
            recommendations = []
            try:
                rec_data = _call("/" + mt + "/" + str(tid) + "/recommendations", {"page": 1})
                for r in rec_data.get("results", [])[:6]: # Max 6
                    recommendations.append({
                        "id": r.get("id"),
                        "title": r.get("name") or r.get("title"),
                        "poster_path": r.get("poster_path"),
                        "vote_average": r.get("vote_average")
                    })
            except Exception:
                pass

            _title_cands = [details.get("name"), details.get("original_name"),
                            details.get("title"), details.get("original_title")]
            out = {"found": True, "tmdb_id": tid, "media_type": mt,
                   "title": details.get("name") or details.get("title") or "",
                   "title_confident": _title_is_confident(title, _title_cands),
                   "overview": details.get("overview") or "",
                   "genres": genres, "providers": providers, "fsk": fsk,
                   "vote_average": round(details.get("vote_average") or 0, 1),
                   "trailer_key": trailer_key,
                   "recommendations": recommendations}
            # Store under both keys so card (title) and modal (imdb_id) share the entry
            logger.info("[CineInfo] TMDB data for %r: trailer=%s, recs=%d", title, trailer_key, len(recommendations))
            for ck in filter(None, [imdb_key, title_key]):
                set_tmdb_cache(ck, out)
            return out
        except Exception as exc:
            logger.warning("[CineInfo] TMDB lookup failed for %r: %s", title or imdb_id, exc)
            return {"found": False}
        finally:
            # Always release the in-flight event so waiting threads wake up
            if my_ev is not None:
                with _tmdb_inflight_lock:
                    _tmdb_inflight.pop(cache_key, None)
                my_ev.set()

    # ---------------------------------------------------------------------------
    # Calendar — upcoming episode air dates for AutoSync jobs (TMDB based)
    # ---------------------------------------------------------------------------
    def _tmdb_calendar_episodes(tmdb_id, api_key, ui_lang="de"):
        """Return {poster, title, episodes} of dated episodes around the currently
        airing season for a TV show. Results are cached for 6 h in tmdb_cache."""
        cache_key = f"calendar|||{tmdb_id}|||{ui_lang}"
        cached = get_tmdb_cache(cache_key, ttl=21600.0)  # 6 h
        if cached is not None:
            return cached

        lang = "en-US" if ui_lang == "en" else "de-DE"

        def _call(path):
            _tmdb_rl.acquire()  # respect the global rate limit (40 req/s)
            r = _rq_tmdb.get(
                "https://api.themoviedb.org/3" + path,
                params={"api_key": api_key, "language": lang}, timeout=8,
                headers={"User-Agent": "MediaForge/1.0"},
            )
            r.raise_for_status()
            return r.json()

        poster = None
        title = ""
        episodes = []
        try:
            details = _call("/tv/" + str(tmdb_id))
            poster = details.get("poster_path")
            title = details.get("name") or details.get("original_name") or ""
            # Collect the seasons referenced by the next/last aired episodes — these
            # are the ones holding the relevant past/future air dates.
            season_numbers = set()
            for ep in (details.get("next_episode_to_air"), details.get("last_episode_to_air")):
                if ep and ep.get("season_number") is not None:
                    season_numbers.add(ep["season_number"])
            for sn in sorted(season_numbers):
                try:
                    sdata = _call("/tv/" + str(tmdb_id) + "/season/" + str(sn))
                except Exception:
                    continue
                for e in sdata.get("episodes", []):
                    ad = e.get("air_date")
                    if not ad:
                        continue
                    episodes.append({
                        "season":   e.get("season_number"),
                        "episode":  e.get("episode_number"),
                        "name":     e.get("name") or "",
                        "air_date": ad,
                        "still":    e.get("still_path"),
                    })
        except Exception as exc:
            logger.debug("[Calendar] episode lookup failed for tmdb %s: %s", tmdb_id, exc)

        out = {"poster": poster, "title": title, "episodes": episodes}
        set_tmdb_cache(cache_key, out)
        return out

    def _tmdb_movie_release(tmdb_id, api_key, ui_lang="de"):
        """Return {poster, title, release_date} for a movie. Cached 6 h."""
        cache_key = f"calmovie|||{tmdb_id}|||{ui_lang}"
        cached = get_tmdb_cache(cache_key, ttl=21600.0)
        if cached is not None:
            return cached
        lang = "en-US" if ui_lang == "en" else "de-DE"
        out = {"poster": None, "title": "", "release_date": None}
        try:
            _tmdb_rl.acquire()
            r = _rq_tmdb.get(
                "https://api.themoviedb.org/3/movie/" + str(tmdb_id),
                params={"api_key": api_key, "language": lang}, timeout=8,
                headers={"User-Agent": "MediaForge/1.0"},
            )
            r.raise_for_status()
            d = r.json()
            out = {
                "poster": d.get("poster_path"),
                "title": d.get("title") or d.get("original_title") or "",
                "release_date": d.get("release_date") or None,
            }
        except Exception as exc:
            logger.debug("[Calendar] movie lookup failed for tmdb %s: %s", tmdb_id, exc)
        set_tmdb_cache(cache_key, out)
        return out

    def _seerr_requested_media():
        """Return [{tmdb_id, media_type}] of pending/approved Seerr requests that
        are not yet available. Empty list if Seerr is not configured/reachable."""
        import urllib.request as _ur
        import urllib.parse as _up
        seerr_url = (get_setting("seerr_url") or "").rstrip("/")
        seerr_key = get_setting("seerr_api_key") or ""
        if not seerr_url or not seerr_key:
            return []

        def seerr_get(path, params=None):
            url = seerr_url + path
            if params:
                url += "?" + _up.urlencode(params)
            req = _ur.Request(url, headers={"X-Api-Key": seerr_key})
            with _ur.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        out = []
        seen = set()
        try:
            for mt in ("tv", "movie"):
                for f in ("pending", "approved"):
                    data = seerr_get("/api/v1/request", {
                        "filter": f, "mediaType": mt, "take": 100, "skip": 0,
                        "sort": "added", "sortDirection": "desc",
                    })
                    for r in data.get("results", []):
                        media = r.get("media") or {}
                        if r.get("status") not in (1, 2):
                            continue
                        if media.get("status") == 5:  # already fully available
                            continue
                        tid = media.get("tmdbId")
                        if not tid or (tid, mt) in seen:
                            continue
                        seen.add((tid, mt))
                        out.append({"tmdb_id": tid, "media_type": mt})
        except Exception as exc:
            logger.debug("[Calendar] Seerr request fetch failed: %s", exc)
        return out

    @app.route("/api/calendar")
    def api_calendar():
        """Aggregate upcoming episode air dates for the current user's AutoSync
        jobs (and optionally Seerr requests and Media Library series) using cached database tables."""
        from .db import get_setting
        if get_setting("cineinfo_calendar", "0") != "1":
            return jsonify({"error": "Calendar disabled", "events": []}), 403

        api_key = get_setting("cineinfo_tmdb_api_key", "").strip()
        if not api_key:
            return jsonify({"error": "no_key", "events": []})
        country = get_setting("cineinfo_country", "DE")
        ui_lang = session.get("ui_language", "en")
        _is_en = ui_lang == "en"

        # The calendar cache stores titles/episode names bilingually; pick the
        # column that matches the current UI language (fall back to the primary).
        def _disp_title(ep):
            return (ep["title_en"] or ep["title"]) if _is_en else ep["title"]

        def _disp_name(ep):
            return (ep["name_en"] or ep["name"]) if _is_en else ep["name"]

        def _cr_norm(t):
            return "".join(c for c in (t or "").lower() if c.isalnum())

        def _crunchyroll_calendar_events():
            """Crunchyroll calendar events. The background watcher resolves CR
            titles -> TMDB and syncs episodes; this normally just reads the cached
            ids/meta + DB. If the watcher cache is still cold, it lazily resolves
            the ids here and on-demand syncs a *bounded* batch so the calendar is
            never empty and fills in progressively across the frontend's 10s polls."""
            try:
                from . import crunchyroll_service as _crs
                if not (_crs.is_enabled() or _crs.has_account()):
                    return []
                ids = list(_cr_calendar_ids)
                if not ids:
                    # Watcher cache cold -> resolve once here (throttled; the call
                    # caches into the module globals for subsequent fast reads).
                    try:
                        ids = _cr_calendar_targets(api_key, country, ui_lang, time.time())
                    except Exception as _e:
                        logger.debug("[Calendar] CR lazy resolve failed: %s", _e)
                        ids = []
                if not ids:
                    return []
                meta = _cr_calendar_meta
                # On-demand sync a bounded number of not-yet-cached ids so the
                # request stays responsive; the watcher fills in the rest.
                cached = get_cached_calendar_media(ids)
                synced = 0
                for tid in ids:
                    if tid in cached:
                        continue
                    try:
                        _sync_calendar_item(tid, "tv", api_key)
                    except Exception as _e:
                        logger.debug("[Calendar] CR on-demand sync %s failed: %s", tid, _e)
                    synced += 1
                    if synced >= _CAL_A_BATCH:
                        break
                out = []
                cutoff = (datetime.now() - timedelta(days=_CR_CAL_PAST_DAYS)).strftime("%Y-%m-%d")
                for ep in get_calendar_episodes_from_db(ids):
                    # Trim the long tail of past episodes (a large watchlist
                    # has thousands); keep a rolling 60-day window + future.
                    if ep["air_date"] and ep["air_date"] < cutoff:
                        continue
                    tid = ep["tmdb_id"]
                    m = meta.get(tid, {})
                    out.append({
                        "job_id": None,
                        "title": m.get("title") or _disp_title(ep),
                        "tmdb_id": tid,
                        "season": ep["season"],
                        "episode": ep["episode"],
                        "name": _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster": ep["poster_path"],
                        "still": ep["still_path"],
                        "source": "crunchyroll",
                        "cr_in_watchlist": m.get("in_wl", False),
                        "cr_in_list": m.get("in_list", False),
                        "cr_lists": sorted(m.get("lists", set())),
                        "cr_kind": ("watchlist" if m.get("in_wl")
                                    else "list" if m.get("in_list")
                                    else "simulcast"),
                    })
                return out
            except Exception as _exc:
                logger.debug("[Calendar] Crunchyroll read failed: %s", _exc)
                return []

        events = []
        seen = set()

        # 1. AutoSync jobs for this user
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs(username=None if is_admin else username)

        autosync_tmdb_ids = []
        job_id_by_tmdb_id = {}
        title_by_tmdb_id = {}
        for job in jobs:
            if job.get("enabled") != 1:
                continue
            title = (job.get("title") or "").strip()
            if not title:
                continue
            try:
                info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                if info and info.get("found") and info.get("media_type") == "tv":
                    tid = info.get("tmdb_id")
                    if tid:
                        tid_int = int(tid)
                        autosync_tmdb_ids.append(tid_int)
                        job_id_by_tmdb_id[tid_int] = job.get("id")
                        title_by_tmdb_id[tid_int] = info.get("title") or title
            except Exception:
                continue

        if autosync_tmdb_ids:
            db_eps = get_calendar_episodes_from_db(autosync_tmdb_ids)
            for ep in db_eps:
                tid = ep["tmdb_id"]
                key = (tid, ep["season"], ep["episode"])
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "job_id":   job_id_by_tmdb_id.get(tid),
                    "title":    title_by_tmdb_id.get(tid) or _disp_title(ep),
                    "tmdb_id":  tid,
                    "season":   ep["season"],
                    "episode":  ep["episode"],
                    "name":     _disp_name(ep),
                    "air_date": ep["air_date"],
                    "poster":   ep["poster_path"],
                    "still":    ep["still_path"],
                    "source":   "autosync",
                })

        # 2. Seerr requests (optional overlay).
        #    Independent of the Media Library option AND of the watcher: Seerr
        #    requests are dynamic and few, so any item that isn't cached yet is
        #    synced on demand here. This guarantees Seerr works immediately even
        #    if the watcher hasn't reached it (or isn't running at all).
        seerr_active = get_setting("cineinfo_calendar_seerr", "0") == "1"
        seerr_count = 0
        if seerr_active:
            seerr_media = _seerr_requested_media()
            seerr_tv_ids = []
            seerr_movie_ids = []
            for m in seerr_media:
                tid = m.get("tmdb_id")
                if not tid:
                    continue
                if m["media_type"] == "tv":
                    seerr_tv_ids.append(int(tid))
                else:
                    seerr_movie_ids.append(int(tid))
            seerr_count = len(seerr_tv_ids) + len(seerr_movie_ids)

            # On-demand sync for any Seerr items missing from the cache.
            seerr_targets = ([(tid, "tv") for tid in seerr_tv_ids]
                             + [(tid, "movie") for tid in seerr_movie_ids])
            if seerr_targets:
                seerr_cached = get_cached_calendar_media([tid for tid, _ in seerr_targets])
                for tid, mt in seerr_targets:
                    if tid not in seerr_cached:
                        try:
                            _sync_calendar_item(tid, mt, api_key)
                        except Exception as _exc:
                            logger.debug("[Calendar] On-demand Seerr sync failed for %s: %s", tid, _exc)

            if seerr_tv_ids:
                db_eps = get_calendar_episodes_from_db(seerr_tv_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = (tid, ep["season"], ep["episode"])
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   ep["season"],
                        "episode":  ep["episode"],
                        "name":     _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    ep["still_path"],
                        "source":   "seerr",
                    })
            if seerr_movie_ids:
                db_eps = get_calendar_episodes_from_db(seerr_movie_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = ("movie", tid)
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   None,
                        "episode":  None,
                        "name":     "",
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    None,
                        "source":   "seerr",
                        "is_movie": True,
                    })

        # 3. Media Library series (optional overlay)
        if get_setting("cineinfo_calendar_mediathek", "0") == "1":
            mediathek_series = get_mediascan_series()
            mediathek_tv_ids = []
            for item in mediathek_series:
                tid = item.get("tmdb_id")
                if tid:
                    try:
                        mediathek_tv_ids.append(int(tid))
                    except ValueError:
                        pass
                elif item.get("title"):
                    title = item.get("title").strip()
                    try:
                        info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                        if info and info.get("found") and info.get("media_type") == "tv":
                            tid = info.get("tmdb_id")
                            if tid:
                                mediathek_tv_ids.append(int(tid))
                    except Exception:
                        continue

            if mediathek_tv_ids:
                db_eps = get_calendar_episodes_from_db(mediathek_tv_ids)
                for ep in db_eps:
                    tid = ep["tmdb_id"]
                    key = (tid, ep["season"], ep["episode"])
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "job_id":   None,
                        "title":    _disp_title(ep),
                        "tmdb_id":  tid,
                        "season":   ep["season"],
                        "episode":  ep["episode"],
                        "name":     _disp_name(ep),
                        "air_date": ep["air_date"],
                        "poster":   ep["poster_path"],
                        "still":    ep["still_path"],
                        "source":   "mediathek",
                    })

        # 4. Crunchyroll simulcast / watchlist / lists. If a CR episode is already
        #    shown via another source (e.g. a Seerr request), keep that event but
        #    attach the CR membership so the Crunchyroll/watchlist badge also shows.
        ev_by_key = {(e.get("tmdb_id"), e.get("season"), e.get("episode")): e
                     for e in events}
        for ev in _crunchyroll_calendar_events():
            ev["cr_member"] = True
            key = (ev.get("tmdb_id"), ev.get("season"), ev.get("episode"))
            existing = ev_by_key.get(key)
            if existing is not None:
                existing["cr_member"] = True
                existing["cr_in_watchlist"] = ev.get("cr_in_watchlist", False)
                existing["cr_in_list"] = ev.get("cr_in_list", False)
                existing["cr_lists"] = ev.get("cr_lists", [])
                existing["cr_kind"] = ev.get("cr_kind")
                continue
            seen.add(key)
            events.append(ev)
            ev_by_key[key] = ev

        # Title fallback: tag events from other sources (e.g. Seerr) whose
        # title matches a CR title but whose TMDB id differs from the
        # CR-resolved one, so they still get the Crunchyroll/watchlist badge.
        if _cr_calendar_titles:
            for ev in events:
                if ev.get("cr_member"):
                    continue
                tm = _cr_calendar_titles.get(_cr_norm(ev.get("title") or ""))
                if tm:
                    ev["cr_member"] = True
                    ev["cr_in_watchlist"] = tm["in_wl"]
                    ev["cr_in_list"] = tm["in_list"]
                    ev["cr_lists"] = sorted(tm["lists"])
                    ev["cr_kind"] = ("watchlist" if tm["in_wl"]
                                     else "list" if tm["in_list"] else "simulcast")

        events.sort(key=lambda e: (e.get("air_date") or ""))
        return jsonify({
            "events": events,
            "watcher": {
                "active": _calendar_watcher_active,
                "is_scanning": _calendar_watcher_scanning,
                "last_sync": _calendar_watcher_last_sync
            },
            "meta": {
                "seerr_active": seerr_active,
                "seerr_count": seerr_count,
            }
        })

    @app.route("/api/tmdb/info")
    def api_tmdb_info():
        title   = (request.args.get("title")   or "").strip()
        imdb_id = (request.args.get("imdb_id") or "").strip() or None
        if not title and not imdb_id:
            return jsonify({"error": "title or imdb_id required"}), 400
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        if not api_key:
            return jsonify({"found": False, "reason": "no_key"})
        country  = get_setting("cineinfo_country", "DE")
        ui_lang  = session.get("ui_language", "de")
        return jsonify(_tmdb_lookup_cached(title, imdb_id, api_key, country, ui_lang))

    @app.route("/api/tmdb/batch", methods=["POST"])
    def api_tmdb_batch():
        """Fetch TMDB data for multiple titles in one request.

        Accepts: {"titles": ["Title A", "Title B", ...]}  (max 25)
        Returns: {"Title A": {found, ...}, "Title B": {found, ...}}

        Internally uses a thread pool (max 3 workers) so lookups for
        already-cached titles complete instantly while cold ones are
        fetched concurrently. All HTTP calls to TMDB still go through
        the shared rate-limiter, so we never exceed 40 req/s globally.
        """
        import concurrent.futures as _cf
        data = request.get_json(silent=True) or {}
        raw_titles = data.get("titles") or []
        titles = [str(t).strip() for t in raw_titles if t][:25]  # cap at 25
        if not titles:
            return jsonify({})
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        if not api_key:
            return jsonify({t: {"found": False, "reason": "no_key"} for t in titles})
        country  = get_setting("cineinfo_country", "DE")
        ui_lang  = session.get("ui_language", "de")

        results = {}
        # max_workers=3 limits concurrent lookups; the rate limiter inside
        # _tmdb_lookup_cached serialises the actual TMDB HTTP calls globally.
        with _cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="tmdb-batch") as pool:
            future_to_title = {
                pool.submit(_tmdb_lookup_cached, t, None, api_key, country, ui_lang): t
                for t in titles
            }
            for fut in _cf.as_completed(future_to_title, timeout=35):
                t = future_to_title[fut]
                try:
                    results[t] = fut.result()
                except Exception as exc:
                    logger.debug("[CineInfo] batch lookup failed for %r: %s", t, exc)
                    results[t] = {"found": False}
        return jsonify(results)


    @app.route("/api/tmdb/cache/clear", methods=["POST"])
    def api_tmdb_cache_clear():
        """Clear all CineInfo/TMDB cached data and trigger a fresh prefetch cycle.

        Steps:
          1. Wipe the SQLite tmdb_cache table (24h persistent data)
          2. Clear the in-memory browse cache so the next browse request
             re-attaches TMDB data from scratch
          3. Kick off a new prefetch cycle in the background so data is
             warmed up without making the caller wait
        """
        clear_tmdb_cache()
        _browse_cache.clear()   # force re-evaluation of inline TMDB data
        # Start a fresh prefetch in background — returns immediately to caller
        threading.Thread(
            target=_prefetch_cycle,
            daemon=True,
            name="cineinfo-manual-refresh",
        ).start()
        logger.info("[CineInfo] Cache manually cleared — prefetch triggered")
        return jsonify({"ok": True, "message": "Cache geleert, Neuladen gestartet"})

    @app.route("/api/settings/dns", methods=["PUT"])
    def api_settings_dns():
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

        return jsonify({"ok": True, "active_server": _active_dns_server})

    @app.route("/api/settings/dns/test", methods=["GET"])
    def api_dns_test():
        """
        Test the current DNS configuration by:
          1. Reporting which DNS mode / server is active in memory.
          2. Resolving each hostname via the patched socket (covers ffmpeg etc.).
          3. Making a real GET request to each site via GLOBAL_SESSION (niquests/DoH)
             and verifying the response body contains a known marker so we know we
             actually reached the correct site (not a block page).
        """
        import socket as _sock
        from ..config import GLOBAL_SESSION as _GS

        _saved_mode = get_setting("dns_mode", "system")
        _saved_server = get_setting("dns_server", "")

        # (url, expected-domain-fragment, body-fallback-markers)
        # Verification priority:
        #   1. Final URL after redirects contains the expected domain → verified
        #   2. Response body contains at least one marker → verified
        #   (Cloudflare challenge pages land on the correct domain, so URL check
        #    handles CDN-fronted sites that return JS challenges to bots.)
        sites = {
            "AniWorld":   ("https://aniworld.to",   "aniworld.to",   ["aniworld", "anime"]),
            "S.TO":       ("https://s.to",          "s.to",          ["serienstream", "serie", "s.to"]),
            "FilmPalast": ("https://filmpalast.to", "filmpalast.to", ["filmpalast", "film"]),
        }

        results = {}
        for label, (url, expected_domain, markers) in sites.items():
            hostname = url.replace("https://", "").rstrip("/")
            entry = {"hostname": hostname}

            # --- socket resolve (tests getaddrinfo patch) ---
            try:
                infos = _sock.getaddrinfo(hostname, 443, proto=_sock.IPPROTO_TCP)
                entry["ip"] = infos[0][4][0] if infos else None
                entry["socket_ok"] = True
            except Exception as e:
                entry["ip"] = None
                entry["socket_ok"] = False
                entry["socket_error"] = str(e)

            # --- HTTP reachability + site identity check via GLOBAL_SESSION ---
            try:
                resp = _GS.get(url, allow_redirects=True, timeout=10)
                entry["http_status"] = resp.status_code
                entry["http_ok"] = resp.status_code < 500

                # Primary: check final URL domain (works even through Cloudflare challenges)
                final_url = str(getattr(resp, "url", url) or url)
                url_verified = expected_domain in final_url

                # Fallback: body content check
                body_lower = (resp.text or "").lower()
                body_verified = any(m.lower() in body_lower for m in markers)

                entry["site_verified"] = url_verified or body_verified
            except Exception as e:
                entry["http_ok"] = False
                entry["site_verified"] = False
                entry["http_error"] = str(e)

            results[label] = entry

        return jsonify({
            "dns_mode":          _saved_mode,
            "dns_server_saved":  _saved_server,
            "dns_active_server": _active_dns_server,
            "sites":             results,
        })

    @app.route("/api/seerr/requests")
    def api_seerr_requests():
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

    @app.route("/api/settings", methods=["PUT"])
    def api_settings_update():
        data = request.get_json(silent=True) or {}
        logger.info("[Settings] PUT /api/settings received data: %r", data)
        if "download_path" in data:
            val = str(data["download_path"]).strip()
            set_setting("download_path", val)
            os.environ["MEDIAFORGE_DOWNLOAD_PATH"] = val
        if "lang_separation" in data:
            val = "1" if data["lang_separation"] else "0"
            set_setting("lang_separation", val)
            os.environ["MEDIAFORGE_LANG_SEPARATION"] = val
        if "disable_english_sub" in data:
            val = "1" if data["disable_english_sub"] else "0"
            set_setting("disable_english_sub", val)
            os.environ["MEDIAFORGE_DISABLE_ENGLISH_SUB"] = val
        if "filmpalast_movie_subfolder" in data:
            val = "1" if data["filmpalast_movie_subfolder"] else "0"
            set_setting("filmpalast_movie_subfolder", val)
            os.environ["FILMPALAST_MOVIE_SUBFOLDER"] = val
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
            valid_langs = set(LANG_LABELS.values()) | {"All Languages"}
            if lang not in valid_langs:
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
            # Root logger — covers werkzeug and any propagating loggers.
            logging.getLogger().setLevel(level)
            # The app's own "aniworld" logger has propagate=False, so changing
            # the root level alone has no effect on it. It must be toggled
            # directly — this is what actually makes debug output start/stop
            # live without a restart (both for enabling AND disabling).
            logging.getLogger("mediaforge").setLevel(level)
            try:
                from ..logger import set_debug_mode as _set_debug_mode
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
        return jsonify({"ok": True})

    @app.route("/api/custom-paths")
    def api_custom_paths():
        paths = get_custom_paths()
        return jsonify({"paths": paths})

    @app.route("/api/custom-paths", methods=["POST"])
    def api_custom_paths_add():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        path = (data.get("path") or "").strip()
        if not name or not path:
            return jsonify({"error": "name and path are required"}), 400
        path_id = add_custom_path(name, path)
        return jsonify({"ok": True, "id": path_id})

    @app.route("/api/custom-paths/<int:path_id>", methods=["DELETE"])
    def api_custom_paths_delete(path_id):
        ok, err = remove_custom_path(path_id)
        if not ok:
            return jsonify({"error": err}), 409
        return jsonify({"ok": True})

    # ===== Auto-Sync Page =====

    @app.route("/autosync")
    def autosync_page():
        return render_template("autosync.html")

    @app.route("/stats")
    def stats_page():
        return render_template("stats.html")

    @app.route("/history")
    def history_page():
        return render_template("history.html")

    @app.route("/seerr")
    def seerr_page():
        sto_lang_labels = {"1": "German Dub", "2": "English Dub", "3": "English Dub (German Sub)"}
        return render_template(
            "seerr.html",
            lang_labels=LANG_LABELS,
            sto_lang_labels=sto_lang_labels,
            supported_providers=WORKING_PROVIDERS,
        )

    @app.route("/api/seerr/requests/<int:req_id>/approve", methods=["POST"])
    def api_seerr_approve(req_id):
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
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        data = request.get_json(silent=True) or {}
        title = str(data.get("title", "")).strip()
        poster_url = str(data.get("posterUrl", "")).strip()
        hide_seerr_request(uid, req_id, title, poster_url)
        return jsonify({"ok": True})

    @app.route("/api/seerr/requests/<int:req_id>/unhide", methods=["POST"])
    def api_seerr_unhide(req_id):
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        unhide_seerr_request(uid, req_id)
        return jsonify({"ok": True})

    @app.route("/api/seerr/hidden")
    def api_seerr_hidden():
        from flask import session as _fs
        uid = _fs.get("user_id", 0)
        items = get_hidden_seerr_requests(uid)
        return jsonify({"hidden": items})

    def _media_missing_episodes(seasons: dict) -> list:
        """Detect gaps in a series' episode numbering from library data alone.

        Returns a list of human-readable missing slots (e.g. "S1E3", "S2").
        A whole season counts as missing when it is absent within the
        1..max-season range; within a present season, any episode missing
        between 1 and the highest present episode is reported. An empty list
        means the series is considered complete."""
        notes = []
        season_nums = sorted(
            int(k) for k in seasons.keys()
            if k != "movies" and str(k).isdigit()
        )
        if not season_nums:
            return notes  # only loose/movie files — not treated as a gappy series
        for s in range(1, max(season_nums) + 1):
            skey = str(s)
            if s not in season_nums:
                notes.append(f"S{s}")  # whole season missing
                continue
            eps = sorted({
                e.get("episode") for e in seasons.get(skey, [])
                if e.get("episode") is not None and e.get("is_video", True)
            })
            if not eps:
                continue
            present = set(eps)
            for ep in range(1, max(eps) + 1):
                if ep not in present:
                    notes.append(f"S{s}E{ep}")
        return notes

    def _compute_media_stats():
        """Build the Media statistics category from the library cache.

        The library cache is kept current by the library watcher, so these
        numbers track on-disk media automatically. Series that appear in
        multiple language folders (lang-separation mode) are merged by folder
        name so each logical series is counted once; their seasons are unioned
        so an episode present in any language counts as present."""
        cache = get_all_library_cache()
        any_scanning = any(e.get("is_scanning") for e in cache.values())
        ignores = get_media_ignores()

        # Merge titles across all locations / language folders by folder name.
        series = {}  # folder -> {"seasons": {skey: set(eps)}, "episodes": int, "location": str}
        movie_folders = set()

        for path_key, entry in cache.items():
            data = entry.get("data") or {}
            location = data.get("label", path_key)
            lang_folders = data.get("lang_folders") or []
            if lang_folders:
                title_lists = [lf.get("titles") or [] for lf in lang_folders]
            else:
                title_lists = [data.get("titles") or []]
            for titles in title_lists:
                for t in titles:
                    folder = t.get("folder")
                    if not folder:
                        continue
                    if t.get("is_movie"):
                        movie_folders.add(folder.lower())
                        continue
                    agg = series.setdefault(
                        folder.lower(),
                        {"title": folder, "seasons": {}, "location": location},
                    )
                    for skey, eps in (t.get("seasons") or {}).items():
                        bucket = agg["seasons"].setdefault(skey, set())
                        for e in eps:
                            if e.get("episode") is not None and e.get("is_video", True):
                                bucket.add(e.get("episode"))

        movies_total = len(movie_folders)
        series_total = len(series)
        episodes_total = 0
        complete = 0
        incomplete_list = []

        for folder_key, agg in series.items():
            # episode count = distinct episodes across all (numeric) seasons
            for skey, eps in agg["seasons"].items():
                if skey != "movies":
                    episodes_total += len(eps)
            seasons_for_gap = {
                skey: [{"episode": ep} for ep in eps]
                for skey, eps in agg["seasons"].items()
            }
            missing = _media_missing_episodes(seasons_for_gap)
            # Subtract user-ignored slots so a series whose remaining gaps are
            # all ignored counts as complete.
            ig = ignores.get(folder_key)
            if ig:
                if "__all__" in ig["slots"]:
                    missing = []
                else:
                    missing = [m for m in missing if m not in ig["slots"]]
            if missing:
                incomplete_list.append({
                    "folder": folder_key,
                    "title": agg["title"],
                    "location": agg["location"],
                    "missing": missing,
                })
            else:
                complete += 1

        incomplete_list.sort(key=lambda x: x["title"].lower())

        # Management view: everything the user has ignored, so it can be restored.
        ignored_list = [
            {
                "folder": folder_key,
                "title": ig.get("title") or folder_key,
                "slots": sorted(ig["slots"]),
            }
            for folder_key, ig in ignores.items()
        ]
        ignored_list.sort(key=lambda x: x["title"].lower())

        return {
            "movies_total": movies_total,
            "series_total": series_total,
            "series_complete": complete,
            "series_incomplete": len(incomplete_list),
            "episodes_total": episodes_total,
            "incomplete": incomplete_list,
            "ignored": ignored_list,
            "scanning": any_scanning,
            "scanned": bool(cache),
        }

    @app.route("/api/stats")
    def api_stats():
        payload = {
            "general": get_general_stats(),
            "queue": get_queue_stats(),
            "sync": get_sync_stats(),
        }
        media_enabled = (get_setting("media_stats_enabled")
                         or os.environ.get("MEDIAFORGE_MEDIA_STATS_ENABLED", "0")) == "1"
        if media_enabled:
            # Kick off an initial library scan if nothing has been scanned yet,
            # so the Media category isn't permanently empty for fresh installs.
            if not get_all_library_cache():
                lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
                _lib_trigger_scan_async(_lib_build_scan_targets(), lang_sep)
            payload["media"] = _compute_media_stats()
        return jsonify(payload)

    @app.route("/api/media/ignore", methods=["POST"])
    def api_media_ignore():
        """Ignore missing slots (or whole series) in the Incomplete-series view."""
        data = request.get_json(silent=True) or {}
        items = data.get("items", [])
        if not isinstance(items, list) or not items:
            return jsonify({"error": "items required"}), 400
        for it in items:
            folder = str(it.get("folder", "")).strip()
            title = str(it.get("title", "")).strip()
            if it.get("all"):
                slots = ["__all__"]
            else:
                slots = [str(s).strip() for s in (it.get("slots") or []) if str(s).strip()]
            if folder and slots:
                add_media_ignores(folder, slots, title)
        return jsonify({"ok": True})

    @app.route("/api/media/unignore", methods=["POST"])
    def api_media_unignore():
        """Restore a previously ignored slot (or the whole series)."""
        data = request.get_json(silent=True) or {}
        folder = str(data.get("folder", "")).strip()
        if not folder:
            return jsonify({"error": "folder required"}), 400
        if data.get("all"):
            remove_media_ignore(folder, all_slots=True)
        else:
            slot = str(data.get("slot", "")).strip()
            if not slot:
                return jsonify({"error": "slot required"}), 400
            remove_media_ignore(folder, slot=slot)
        return jsonify({"ok": True})

    # ===== Download History =====

    def _history_since_from_range(rng):
        """Map a date-range key (1d/7d/30d/all) to a UTC cutoff string, or None."""
        from datetime import datetime, timedelta
        days = {"1d": 1, "7d": 7, "30d": 30}.get((rng or "all").strip())
        if not days:
            return None
        return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def _history_filters():
        search = (request.args.get("search") or "").strip() or None
        status = (request.args.get("status") or "all").strip()
        source = (request.args.get("source") or "all").strip()
        since = _history_since_from_range(request.args.get("range"))
        return search, status, source, since

    @app.route("/api/history")
    def api_history_list():
        username, is_admin = _get_current_user_info()
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        search, status, source, since = _history_filters()
        entries, total = get_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
            limit=limit, offset=offset,
        )
        return jsonify({"entries": entries, "total": total, "limit": limit, "offset": offset})

    @app.route("/api/history/<int:entry_id>/retry", methods=["POST"])
    def api_history_retry(entry_id):
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        if entry.get("status") not in ("failed", "cancelled"):
            return jsonify({"error": "Only failed or cancelled downloads can be retried"}), 400
        ep_url = entry.get("episode_url")
        if not ep_url:
            return jsonify({"error": "No episode URL stored for this entry"}), 400
        add_to_queue(
            title=entry.get("title") or "",
            series_url=entry.get("series_url") or ep_url,
            episodes=[ep_url],
            language=entry.get("language") or "German Dub",
            provider=entry.get("provider") or "VOE",
            username=entry.get("username"),
            source=entry.get("source") or "manual",
        )
        _ensure_queue_worker()
        return jsonify({"ok": True})

    @app.route("/api/history/delete", methods=["POST"])
    def api_history_bulk_delete():
        username, is_admin = _get_current_user_info()
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids required"}), 400
        deleted = delete_download_history_entries(
            ids, username=None if is_admin else username
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/api/history/export")
    def api_history_export():
        import csv as _csv, io as _io
        username, is_admin = _get_current_user_info()
        search, status, source, since = _history_filters()
        fmt = (request.args.get("format") or "csv").strip().lower()
        entries, _ = get_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
            limit=1000000, offset=0,
        )
        from flask import Response
        if fmt == "json":
            payload = json.dumps({"entries": entries}, ensure_ascii=False, indent=2)
            return Response(payload, mimetype="application/json",
                            headers={"Content-Disposition": 'attachment; filename="download_history.json"'})
        cols = ["title", "season", "episode", "status", "error", "language",
                "provider", "source", "size_mb", "avg_speed_mbps", "duration_sec",
                "started_at", "finished_at", "target_path", "episode_url"]
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(cols)
        for e in entries:
            w.writerow([e.get(c, "") for c in cols])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="download_history.csv"'})

    @app.route("/api/history/<int:entry_id>")
    def api_history_get(entry_id):
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        return jsonify({"entry": entry})

    @app.route("/api/history/<int:entry_id>", methods=["DELETE"])
    def api_history_delete(entry_id):
        entry = get_download_history_entry(entry_id)
        if not entry:
            return jsonify({"error": "Not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and entry.get("username") != username:
            return jsonify({"error": "Not authorized"}), 403
        delete_download_history_entry(entry_id)
        return jsonify({"ok": True})

    @app.route("/api/history/clear", methods=["POST"])
    def api_history_clear():
        username, is_admin = _get_current_user_info()
        # Honour active filters so the user can clear just the current view
        # (e.g. only failed, or only the last 7 days). No filters = clear all.
        data = request.get_json(silent=True) or {}
        search = (data.get("search") or "").strip() or None
        status = (data.get("status") or "all").strip()
        source = (data.get("source") or "all").strip()
        since = _history_since_from_range(data.get("range"))
        deleted = clear_download_history(
            username=None if is_admin else username,
            search=search, status=status, source=source, since=since,
        )
        return jsonify({"ok": True, "deleted": deleted})

    # ===== Favourites =====

    @app.route("/favourites")
    def favourites_page():
        return render_template("favourites.html")

    @app.route("/api/favourites")
    def api_get_favourites():
        username = None
        if auth_enabled:
            user = get_current_user()
            username = user.get("username") if user else None
        favs = get_favourites(added_by=username)
        # Proxy poster URLs so the client never hits source sites directly
        for f in favs:
            if f.get("poster_url") and not f["poster_url"].startswith("/api/img"):
                f["poster_url"] = _poster_proxy(f["poster_url"])
        return jsonify({"favourites": favs})

    @app.route("/api/favourites", methods=["POST"])
    def api_add_favourite():
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
        username = None
        if auth_enabled:
            user = get_current_user()
            username = user.get("username") if user else None
        add_favourite(series_url, title, poster_url, username)
        return jsonify({"ok": True})

    @app.route("/api/favourites", methods=["DELETE"])
    def api_remove_favourite():
        data = request.get_json(silent=True) or {}
        series_url = (data.get("series_url") or "").strip()
        if not series_url:
            return jsonify({"error": "series_url required"}), 400
        username = None
        if auth_enabled:
            user = get_current_user()
            username = user.get("username") if user else None
        remove_favourite(series_url, username)
        return jsonify({"ok": True})

    @app.route("/api/favourites/check")
    def api_check_favourite():
        series_url = request.args.get("series_url", "").strip()
        if not series_url:
            return jsonify({"is_favourite": False})
        username = None
        if auth_enabled:
            user = get_current_user()
            username = user.get("username") if user else None
        return jsonify({"is_favourite": is_favourite(series_url, username)})

    # ===== Auto-Sync API =====

    def _get_current_user_info():
        """Return (username, is_admin) for the current request."""
        if not auth_enabled:
            return None, True  # no auth → treat as admin
        user = get_current_user()
        if not user:
            return None, False
        username = (
            user.get("username")
            if isinstance(user, dict)
            else getattr(user, "username", None)
        )
        role = (
            user.get("role")
            if isinstance(user, dict)
            else getattr(user, "role", "user")
        )
        return username, role == "admin"

    @app.route("/api/autosync")
    def api_autosync_list():
        username, is_admin = _get_current_user_info()
        # Admins see all jobs; regular users see only their own
        jobs = get_autosync_jobs(username=None if is_admin else username)
        for job in jobs:
            job["adaptive_paused"] = _is_job_adaptive_paused(job)
        return jsonify({"jobs": jobs})

    @app.route("/api/autosync", methods=["POST"])
    def api_autosync_create():
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        series_url = (data.get("series_url") or "").strip()
        language = data.get("language", "German Dub")
        provider = data.get("provider", "VOE")
        custom_path_id = data.get("custom_path_id")
        movie_custom_path_id = data.get("movie_custom_path_id")
        episode_filter = _normalize_episode_filter(data.get("episode_filter"))

        if not title or not series_url:
            return jsonify({"error": "title and series_url are required"}), 400

        existing = find_autosync_by_url(series_url)
        if existing:
            return jsonify(
                {"error": "A sync job for this series already exists", "job": existing}
            ), 409

        username, _ = _get_current_user_info()
        # Resolve path_unavailable_action: request body > global setting > "skip"
        path_action = (
            data.get("path_unavailable_action")
            or get_setting("sync_path_unavailable_action")
            or os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip")
        ).strip().lower()
        if path_action not in ("skip", "hold"):
            path_action = "skip"
        job_id = add_autosync_job(
            title=title,
            series_url=series_url,
            language=language,
            provider=provider,
            custom_path_id=custom_path_id,
            added_by=username,
            path_unavailable_action=path_action,
            episode_filter=episode_filter,
            movie_custom_path_id=movie_custom_path_id,
        )
        return jsonify({"ok": True, "id": job_id})

    @app.route("/api/autosync/site-search", methods=["POST"])
    def api_autosync_site_search():
        """Resolve a (library) title to candidate series on AniWorld / S.TO so
        it can be added to Auto-Sync. Performs the "is it actually findable on
        a site" check, and returns every match (with its source site) so the
        caller can let the user choose when more than one is found."""
        import difflib
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400

        def _norm(s):
            return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        target = _norm(title)

        candidates = []
        seen = set()

        def _collect(items, site, site_label, base, pattern):
            if isinstance(items, dict):
                items = [items]
            for item in (items or []):
                link = item.get("link") or item.get("url", "")
                if not pattern.match(link):
                    continue
                name = _html_unescape(
                    item.get("title") or item.get("name", "Unknown")
                ).replace("<em>", "").replace("</em>", "")
                url = base + link
                if url in seen:
                    continue
                seen.add(url)
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": site, "site_label": site_label,
                    "title": name, "url": url, "score": round(score, 3),
                })

        try:
            _collect(aniworld_query(title), "aniworld", "AniWorld",
                     "https://aniworld.to", _SERIES_LINK_PATTERN)
        except Exception as e:
            logger.debug("[AutosyncSearch] AniWorld search failed: %s", e)
        try:
            _collect(query_s_to(title), "sto", "S.TO",
                     "https://s.to", _STO_SERIES_LINK_PATTERN)
        except Exception as e:
            logger.debug("[AutosyncSearch] S.TO search failed: %s", e)

        candidates.sort(key=lambda c: c["score"], reverse=True)
        return jsonify({"results": candidates[:12]})

    @app.route("/api/autosync/<int:job_id>", methods=["PUT"])
    def api_autosync_update(job_id):
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to edit this job"}), 403
        data = request.get_json(silent=True) or {}
        allowed = {"language", "provider", "enabled", "custom_path_id",
                   "path_unavailable_action", "episode_filter", "movie_custom_path_id",
                   "group_name"}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if "group_name" in filtered:
            gn = filtered["group_name"]
            gn = (str(gn).strip() if gn is not None else "")
            filtered["group_name"] = gn or None
        filter_changed = "episode_filter" in filtered
        if filter_changed:
            filtered["episode_filter"] = _normalize_episode_filter(filtered["episode_filter"])
            # Mark for a silent baseline recompute on the next sync so the
            # "new episodes" badge is not skewed by the changed filter scope.
            filtered["filter_dirty"] = 1
        update_autosync_job(job_id, **filtered)
        # When the filter changed, kick off a background sync immediately so the
        # card counts reflect the new scope right away (and in-scope missing
        # episodes are queued).
        if filter_changed:
            fresh = get_autosync_job(job_id)
            if fresh and fresh.get("enabled"):
                with _syncing_jobs_lock:
                    _busy = job_id in _syncing_jobs
                if not _busy:
                    threading.Thread(
                        target=_run_autosync_for_job, args=(fresh,), daemon=True
                    ).start()
        return jsonify({"ok": True})

    @app.route("/api/autosync/<int:job_id>", methods=["DELETE"])
    def api_autosync_delete(job_id):
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to delete this job"}), 403
        ok, err = remove_autosync_job(job_id)
        if not ok:
            return jsonify({"error": err}), 404
        return jsonify({"ok": True})

    @app.route("/api/autosync/<int:job_id>/sync", methods=["POST"])
    def api_autosync_trigger(job_id):
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized"}), 403
        with _syncing_jobs_lock:
            if job_id in _syncing_jobs:
                return jsonify({"error": "Sync already running for this job"}), 409
        threading.Thread(target=_run_autosync_for_job, args=(job, True), daemon=True).start()
        return jsonify({"ok": True, "message": "Sync started"})

    @app.route("/api/autosync/running")
    def api_autosync_running():
        """Return the set of currently running sync job IDs."""
        with _syncing_jobs_lock:
            return jsonify({"running": list(_syncing_jobs)})

    @app.route("/api/autosync/sync-all", methods=["POST"])
    def api_autosync_sync_all():
        """Trigger sync for all enabled jobs the current user owns (or all if admin)."""
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs()
        started = 0
        skipped = 0
        for job in jobs:
            if not job.get("enabled"):
                continue
            if not is_admin and job.get("added_by") != username:
                continue
            job_id = job["id"]
            with _syncing_jobs_lock:
                if job_id in _syncing_jobs:
                    skipped += 1
                    continue
            threading.Thread(target=_run_autosync_for_job, args=(job,), daemon=True).start()
            started += 1
        return jsonify({"ok": True, "started": started, "skipped": skipped})

    @app.route("/api/autosync/check", methods=["GET"])
    def api_autosync_check():
        """Check if a sync job exists for a given series URL."""
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"exists": False})
        job = find_autosync_by_url(url)
        if not job:
            return jsonify({"exists": False})
        # Only expose job details to the owner or admins
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "job": job})


    @app.route("/api/autosync/export", methods=["GET"])
    def api_autosync_export():
        """Export all autosync jobs the current user can see as JSON."""
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs(username=None if is_admin else username)
        # Strip runtime-only fields that make no sense on import
        export_fields = {"title", "series_url", "language", "provider", "enabled", "episode_filter"}
        clean = [{k: j[k] for k in export_fields if k in j} for j in jobs]
        payload = json.dumps({"version": 1, "jobs": clean}, ensure_ascii=False, indent=2)
        from flask import Response
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="autosync_backup.json"'},
        )

    @app.route("/api/autosync/import", methods=["POST"])
    def api_autosync_import():
        """Import autosync jobs from a JSON backup. Skips duplicates."""
        username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "Nur Admins können Jobs importieren"}), 403
        try:
            data = request.get_json(silent=True)
            if data is None:
                # try raw text body
                data = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Ungültiges JSON"}), 400

        jobs_in = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs_in, list):
            return jsonify({"error": "Erwartet: {jobs: [...]}"}), 400

        imported = 0
        skipped  = 0
        errors   = []
        for entry in jobs_in:
            title      = (entry.get("title") or "").strip()
            series_url = (entry.get("series_url") or "").strip()
            language   = entry.get("language", "German Dub")
            provider   = entry.get("provider", "VOE")
            enabled    = int(entry.get("enabled", 1))
            episode_filter = _normalize_episode_filter(entry.get("episode_filter"))
            if not title or not series_url:
                errors.append(f"Übersprungen (kein title/series_url): {entry}")
                continue
            if find_autosync_by_url(series_url):
                skipped += 1
                continue
            try:
                job_id = add_autosync_job(
                    title=title,
                    series_url=series_url,
                    language=language,
                    provider=provider,
                    added_by=username,
                    episode_filter=episode_filter,
                )
                if not enabled:
                    update_autosync_job(job_id, enabled=0)
                imported += 1
            except Exception as exc:
                errors.append(f"{title}: {exc}")
        return jsonify({"ok": True, "imported": imported, "skipped": skipped, "errors": errors})

    @app.route("/api/autosync/batch", methods=["POST"])
    def api_autosync_batch():
        """Batch-update multiple autosync jobs at once.

        Body: { ids: [int, ...], action: "enable"|"disable"|"set_path", custom_path_id: int|null }
        """
        username, is_admin = _get_current_user_info()
        data   = request.get_json(silent=True) or {}
        ids    = data.get("ids", [])
        action = data.get("action", "")
        if not ids or action not in ("enable", "disable", "set_path", "delete",
                                     "set_group", "remove_group"):
            return jsonify({"error": "ids und action (enable|disable|set_path|delete|set_group|remove_group) erforderlich"}), 400

        updated = 0
        for job_id in ids:
            job = get_autosync_job(job_id)
            if not job:
                continue
            if not is_admin and job.get("added_by") != username:
                continue
            if action == "enable":
                update_autosync_job(job_id, enabled=1)
            elif action == "disable":
                update_autosync_job(job_id, enabled=0)
            elif action == "set_path":
                cp_id = data.get("custom_path_id")  # None = Standard
                update_autosync_job(job_id, custom_path_id=cp_id)
            elif action == "set_group":
                gname = data.get("group_name")
                gname = (str(gname).strip() if gname is not None else "")
                update_autosync_job(job_id, group_name=(gname or None))
            elif action == "remove_group":
                update_autosync_job(job_id, group_name=None)
            elif action == "delete":
                ok, _ = remove_autosync_job(job_id)
                if not ok:
                    continue
            updated += 1
        return jsonify({"ok": True, "updated": updated})

    @app.route("/api/autosync/group/rename", methods=["POST"])
    def api_autosync_group_rename():
        """Rename a manual group: set group_name = new on all jobs the user may
        edit whose group_name == old.

        Body: { old: str, new: str }
        """
        username, is_admin = _get_current_user_info()
        data = request.get_json(silent=True) or {}
        old_name = (data.get("old") or "").strip()
        new_name = (data.get("new") or "").strip()
        if not old_name or not new_name:
            return jsonify({"error": "old und new erforderlich"}), 400
        jobs = get_autosync_jobs(username=None if is_admin else username)
        updated = 0
        for job in jobs:
            if (job.get("group_name") or "") == old_name:
                update_autosync_job(job["id"], group_name=new_name)
                updated += 1
        return jsonify({"ok": True, "updated": updated})

    # ===== Stats API =====

    @app.route("/api/stats/sync")
    def api_stats_sync():
        stats = get_sync_stats()
        # Compute next_run_at from last check + schedule interval
        schedule_key = os.environ.get("MEDIAFORGE_SYNC_SCHEDULE", "0")
        interval = SYNC_SCHEDULE_MAP.get(schedule_key, 0)
        stats["schedule"] = schedule_key
        stats["next_run_at"] = None
        if interval and stats.get("last_check"):
            from datetime import datetime, timedelta

            try:
                last = datetime.strptime(stats["last_check"], "%Y-%m-%d %H:%M:%S")
                nxt = last + timedelta(seconds=interval)
                stats["next_run_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return jsonify(stats)

    @app.route("/api/stats/queue")
    def api_stats_queue():
        return jsonify(get_queue_stats())

    @app.route("/api/stats/general")
    def api_stats_general():
        return jsonify(get_general_stats())

    # ---- Image proxy with disk cache ----

    _ALLOWED_IMAGE_HOSTS = {
        "aniworld.to", "www.aniworld.to",
        "s.to", "www.s.to", "serienstream.to",
        "filmpalast.to", "www.filmpalast.to",
        "image.tmdb.org", "cdn.myanimelist.net",
        "cdn.aniworld.to",
        # Crunchyroll image CDNs (calendar thumbnails / series art)
        "imgsrv.crunchyroll.com", "static.crunchyroll.com",
        "img1.ak.crunchyroll.com", "www.crunchyroll.com",
    }

    import hashlib as _hashlib
    from pathlib import Path as _Path

    _IMAGE_CACHE_DIR = MEDIAFORGE_CONFIG_DIR / "image_cache"
    _IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    _IMG_FETCH_RETRIES = 3
    _IMG_FETCH_TIMEOUT = 20

    def _img_upstream_headers(raw_url: str) -> dict:
        """Referer + Accept so CDNs don't drop requests that look like off-site hotlinks."""
        from urllib.parse import urlparse as _urlp_img

        try:
            netloc = _urlp_img(raw_url).netloc.lower()
        except Exception:
            return {}
        host = netloc.removeprefix("www.")
        referer_by_host = {
            "filmpalast.to": "https://filmpalast.to/",
            "s.to": "https://s.to/",
            "serienstream.to": "https://s.to/",
            "aniworld.to": "https://aniworld.to/",
            "cdn.aniworld.to": "https://aniworld.to/",
        }
        ref = referer_by_host.get(host)
        if not ref:
            return {}
        return {
            "Referer": ref,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }

    def _img_fetch_with_retries(raw_url: str):
        """
        GET with retries over classic HTTPS (TCP).

        GLOBAL_SESSION uses niquests, which may negotiate HTTP/3 (QUIC). Cloudflare
        often resets those connections from Python (logs: quic … Connection close
        0x128).  Plain ``requests`` stays on HTTP/1.1 or HTTP/2 over TLS — same
        approach as FilmPalastEpisode._html (see episode.py).

        Several source CDNs (aniworld/s.to/filmpalast/Crunchyroll) sit behind
        Cloudflare bot protection. Plain ``requests`` exposes a Python/OpenSSL TLS
        fingerprint that Cloudflare blocks on Windows builds — the reason posters
        "barely load" there while Docker (Linux OpenSSL) is fine. curl_cffi
        replays a real Chrome TLS handshake so the fingerprint matches the
        User-Agent; we fall back to plain ``requests`` when it is unavailable.
        """
        import time as _time

        import requests as _rq

        from ..config import DEFAULT_USER_AGENT

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        }
        headers.update(_img_upstream_headers(raw_url))

        # Prefer a Chrome-impersonating client to defeat Cloudflare fingerprinting.
        try:
            from curl_cffi import requests as _curl_requests  # type: ignore

            def _do_get():
                return _curl_requests.get(
                    raw_url, timeout=_IMG_FETCH_TIMEOUT,
                    headers=headers, impersonate="chrome120",
                )
        except Exception:
            def _do_get():
                return _rq.get(raw_url, timeout=_IMG_FETCH_TIMEOUT, headers=headers)

        last_exc = None
        for attempt in range(_IMG_FETCH_RETRIES):
            try:
                resp = _do_get()
                if resp.status_code in (502, 503, 504) and attempt + 1 < _IMG_FETCH_RETRIES:
                    _time.sleep(0.25 * (2**attempt))
                    continue
                return resp
            except Exception as e:
                last_exc = e
                if attempt + 1 < _IMG_FETCH_RETRIES:
                    _time.sleep(0.25 * (2**attempt))
                    continue
                raise last_exc from None

    def _img_cache_path(url: str, content_type: str = "image/jpeg") -> _Path:
        """Return the cache file path for a given URL."""
        url_hash = _hashlib.sha256(url.encode()).hexdigest()[:32]
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "image/avif": ".avif"}.get(content_type, ".jpg")
        return _IMAGE_CACHE_DIR / (url_hash + ext)

    def _img_cache_path_any(url: str) -> "_Path | None":
        """Return existing cache file for a URL (regardless of extension), or None."""
        url_hash = _hashlib.sha256(url.encode()).hexdigest()[:32]
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
            p = _IMAGE_CACHE_DIR / (url_hash + ext)
            if p.exists():
                return p
        return None

    def cleanup_image_cache(max_age_days: int = 30):
        """Delete cached image files not accessed in the last max_age_days days."""
        import time
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        try:
            for f in _IMAGE_CACHE_DIR.iterdir():
                if f.is_file() and f.stat().st_atime < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"Image cache cleanup error: {e}")
        if removed:
            logger.debug(f"Image cache: removed {removed} stale file(s)")

    # Run cleanup at startup in background
    threading.Thread(target=cleanup_image_cache, daemon=True).start()

    # Thread pool for background image pre-caching
    import concurrent.futures as _cf
    import urllib.parse as _up_img
    import atexit as _atexit
    _img_pool = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-precache")
    _atexit.register(_img_pool.shutdown, wait=False)

    def _precache_image_bg(url: str):
        """Fetch and save a single image to disk cache. Runs in background pool."""
        if not url or not url.startswith("http"):
            return
        if _img_cache_path_any(url):
            return  # already on disk
        try:
            resp = _img_fetch_with_retries(url)
            if not resp.ok:
                return
            ct = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not ct.startswith("image/"):
                return
            _img_cache_path(url, ct).write_bytes(resp.content)
        except Exception as exc:
            logger.debug(f"img pre-cache failed for {url}: {exc}")

    def _poster_proxy(url: str) -> str:
        """
        Convert a raw source poster URL to the server-side proxy URL AND
        kick off a background pre-cache fetch.  The client browser will
        NEVER receive a direct URL to aniworld.to / s.to / filmpalast.to /
        image.tmdb.org — it always gets /api/img?url=… served by this server.
        """
        if not url:
            return ""
        if url.startswith("/api/img"):
            return url  # already proxied — no-op
        _img_pool.submit(_precache_image_bg, url)
        return "/api/img?url=" + _up_img.quote(url, safe="")

    def _proxy_result_list(results: list) -> list:
        """Return results with proxied poster URLs and inline cached TMDB data."""
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        country = get_setting("cineinfo_country", "DE")
        ui_lang = session.get("ui_language", "de")
        tmdb_on = bool(api_key)

        cache_hits = {}
        if tmdb_on and results:
            keys = []
            for r in results:
                if hasattr(r, "get"):
                    title = r.get("title", "")
                    if title:
                        keys.append(title + "|||" + country + "|||" + ui_lang)
            if keys:
                cache_hits = get_tmdb_cache_bulk(keys)

        out = []
        for r in results:
            r = dict(r)
            if r.get("poster_url"):
                r["poster_url"] = _poster_proxy(r["poster_url"])
            if tmdb_on:
                title = r.get("title", "")
                if title:
                    cached = cache_hits.get(title + "|||" + country + "|||" + ui_lang)
                    if cached is not None:
                        r["tmdb"] = cached
            out.append(r)
        return out

    @app.route("/api/img")
    def api_image_proxy():
        """
        Server-side image proxy with disk cache.

        Fetches poster/cover images on behalf of the client so mobile devices
        don't need a direct connection to source sites (avoids ISP DNS blocks,
        hotlink protection, and mixed-content issues).  Images are cached to
        disk for 30 days; the cache is served directly without re-fetching.

        Only whitelisted source domains are allowed.
        """
        from urllib.parse import urlparse
        from flask import Response, send_file

        raw_url = request.args.get("url", "").strip()
        if not raw_url:
            return ("", 400)

        try:
            parsed = urlparse(raw_url)
        except Exception:
            return ("Bad URL", 400)

        netloc = parsed.netloc.lower()
        host_stripped = netloc.removeprefix("www.")
        if netloc not in _ALLOWED_IMAGE_HOSTS and host_stripped not in _ALLOWED_IMAGE_HOSTS:
            return ("Forbidden host", 403)

        # --- Serve from disk cache if available ---
        cached = _img_cache_path_any(raw_url)
        if cached and cached.exists():
            # Touch the file to reset the LRU timer
            try:
                cached.touch()
            except OSError:
                pass
            ext = cached.suffix.lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif"}.get(ext, "image/jpeg")
            r = send_file(cached, mimetype=mime)
            r.headers["Cache-Control"] = "public, max-age=604800"  # 7 days browser cache
            return r

        # --- Fetch from source ---
        try:
            resp = _img_fetch_with_retries(raw_url)
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                return ("Not an image", 400)
            data = resp.content
        except Exception as e:
            logger.debug(f"Image proxy fetch failed for {raw_url}: {e}")
            return ("", 502)

        # --- Save to disk cache ---
        cache_file = _img_cache_path(raw_url, content_type)
        try:
            cache_file.write_bytes(data)
        except OSError as e:
            logger.debug(f"Image cache write failed: {e}")

        r = Response(data, content_type=content_type)
        r.headers["Cache-Control"] = "public, max-age=604800"
        return r

    # ---- Library: shared scan helpers (module-level closure) ----

    _LIB_LANG_FOLDERS = ["german-dub", "english-sub", "german-sub", "english-dub"]
    _LIB_VIDEO_EXTS = {".mkv", ".mp4", ".ts"}
    _LIB_EP_RE = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)
    _LIB_FALLBACK_EP_RE = re.compile(r"\bE(\d{2,3})\b", re.IGNORECASE)
    _lib_scan_lock = __import__("threading").Lock()

    def _lib_get_resolution(file_path):
        fname = file_path.name.lower()
        if "4k" in fname or "2160p" in fname or "3840x2160" in fname:
            return "4K"
        if "2k" in fname or "1440p" in fname or "2560x1440" in fname:
            return "2K"
        if "1080p" in fname or "1080i" in fname or "1920x1080" in fname:
            return "1080p"
        if "720p" in fname or "1280x720" in fname:
            return "720p"
        if "480p" in fname or "854x480" in fname or "640x480" in fname:
            return "480p"
        if "360p" in fname or "640x360" in fname:
            return "360p"
            
        m = re.search(r"\b(2160|1440|1080|720|480|360|240)p?\b", fname)
        if m:
            val = m.group(1)
            if val == "2160": return "4K"
            if val == "1440": return "2K"
            return val + "p"
            
        try:
            from .transcoder import probe_file
            info = probe_file(file_path)
            if info and info.get("height"):
                h = info["height"]
                if h >= 2160: return "4K"
                if h >= 1440: return "2K"
                if h >= 1080: return "1080p"
                if h >= 720: return "720p"
                if h >= 480: return "480p"
                if h >= 360: return "360p"
                return f"{h}p"
        except Exception:
            pass
        return None

    def _lib_resolve_base():
        from pathlib import Path
        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            dl_base = Path(raw).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            dl_base = Path.home() / "Downloads"
        return dl_base

    def _lib_scan_base(base, old_cache_lookup=None):
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor
        lang_folder_set = set(_LIB_LANG_FOLDERS)
        titles = {}
        if not base.is_dir():
            return []

        # Helper to check if file is video
        def is_video_file(f):
            if not f.is_file(): return False
            fname = f.name
            if fname.startswith(".temp_") or fname.startswith("."): return False
            if ".part" in fname or fname.endswith(".part"): return False
            fname_lower = fname.lower()
            return any(fname_lower.endswith(ext) for ext in _LIB_VIDEO_EXTS)

        # 1. Collect all video files
        all_videos = []
        
        # Zero-th pass candidates
        for f in base.iterdir():
            if is_video_file(f):
                if _LIB_EP_RE.search(f.name) or _LIB_FALLBACK_EP_RE.search(f.name):
                    continue
                all_videos.append(f)

        # First and Second pass candidates
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            name = folder.name
            if name in lang_folder_set:
                continue
            for f in folder.iterdir():
                if is_video_file(f):
                    if _LIB_EP_RE.search(f.name) or _LIB_FALLBACK_EP_RE.search(f.name):
                        continue
                    all_videos.append(f)
            for f in folder.rglob("*"):
                if is_video_file(f):
                    if _LIB_EP_RE.search(f.name) or _LIB_FALLBACK_EP_RE.search(f.name):
                        all_videos.append(f)

        # Remove duplicates while preserving order
        seen = set()
        unique_videos = []
        for f in all_videos:
            if f not in seen:
                seen.add(f)
                unique_videos.append(f)

        # 2. Determine which videos need probing
        probe_candidates = []
        resolved_media_data = {} # Path -> {"resolution": ..., "video_codec": ..., "audio_codec": ...}
        
        for f in unique_videos:
            try:
                fsize = f.stat().st_size
            except OSError:
                fsize = 0
            
            # Check old cache lookup
            cached = old_cache_lookup.get((str(f), fsize)) if old_cache_lookup else None
            if cached and cached.get("video_codec"):
                resolved_media_data[f] = cached
                continue
                
            # Check filename keywords/regex
            fname = f.name.lower()
            res_fast = None
            if "4k" in fname or "2160p" in fname or "3840x2160" in fname: res_fast = "4K"
            elif "2k" in fname or "1440p" in fname or "2560x1440" in fname: res_fast = "2K"
            elif "1080p" in fname or "1080i" in fname or "1920x1080" in fname: res_fast = "1080p"
            elif "720p" in fname or "1280x720" in fname: res_fast = "720p"
            elif "480p" in fname or "854x480" in fname or "640x480" in fname: res_fast = "480p"
            elif "360p" in fname or "640x360" in fname: res_fast = "360p"
            else:
                m = re.search(r"\b(2160|1440|1080|720|480|360|240)p?\b", fname)
                if m:
                    val = m.group(1)
                    if val == "2160": res_fast = "4K"
                    elif val == "1440": res_fast = "2K"
                    else: res_fast = val + "p"
                    
            vc_fast = None
            if "hevc" in fname or "x265" in fname or "h.265" in fname: vc_fast = "HEVC"
            elif "h264" in fname or "x264" in fname or "h.264" in fname or "avc" in fname: vc_fast = "H.264"
            elif "av1" in fname: vc_fast = "AV1"
            
            if res_fast:
                resolved_media_data[f] = {"resolution": res_fast, "video_codec": vc_fast, "audio_codec": None}
            else:
                probe_candidates.append(f)

        # 3. Probe candidates in parallel
        if probe_candidates:
            logger.info("[LibraryScan] Probing %d files in parallel...", len(probe_candidates))
            
            def probe_one(file_path):
                try:
                    from .transcoder import probe_file
                    info = probe_file(file_path)
                    if info:
                        res = None
                        if info.get("height"):
                            h = info["height"]
                            if h >= 2160: res = "4K"
                            elif h >= 1440: res = "2K"
                            elif h >= 1080: res = "1080p"
                            elif h >= 720: res = "720p"
                            elif h >= 480: res = "480p"
                            elif h >= 360: res = "360p"
                            else: res = f"{h}p"
                            
                        vc = info.get("video_codec")
                        if vc:
                            vc = vc.lower()
                            if vc in ["hevc", "x265", "h265"]: vc = "HEVC"
                            elif vc in ["h264", "x264", "avc"]: vc = "H.264"
                            elif vc == "av1": vc = "AV1"
                            else: vc = vc.upper()
                            
                        ac = info.get("audio_codec")
                        if ac:
                            ac = ac.upper()
                            
                        return {"resolution": res, "video_codec": vc, "audio_codec": ac}
                except Exception:
                    pass
                return None

            with ThreadPoolExecutor(max_workers=16) as executor:
                results = executor.map(probe_one, probe_candidates)
                for f, res_dict in zip(probe_candidates, results):
                    if res_dict:
                        resolved_media_data[f] = res_dict

        # 4. Perform the actual build of titles/seasons structure using pre-resolved resolutions
        # Zero-th pass: video files sitting DIRECTLY in base (no title subfolder).
        for f in base.iterdir():
            if not is_video_file(f):
                continue
            if _LIB_EP_RE.search(f.name) or _LIB_FALLBACK_EP_RE.search(f.name):
                continue
            title_name = f.stem
            try:
                fsize = f.stat().st_size
            except OSError:
                fsize = 0
            if title_name not in titles:
                titles[title_name] = {"folder": title_name, "seasons": {}, "total_size": 0, "is_movie": False}
            entry = titles[title_name]
            if "movies" not in entry["seasons"]:
                entry["seasons"]["movies"] = []
            if not any(e["file"] == f.name for e in entry["seasons"]["movies"]):
                mdata = resolved_media_data.get(f) or {}
                entry["seasons"]["movies"].append({
                    "episode": 1, "file": f.name, "size": fsize, "is_video": True,
                    "is_movie_file": True, "path": str(f),
                    "resolution": mdata.get("resolution"),
                    "video_codec": mdata.get("video_codec"),
                    "audio_codec": mdata.get("audio_codec")
                })
                entry["total_size"] += fsize
                entry["is_movie"] = True

        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            name = folder.name
            if name in lang_folder_set:
                continue
            if name not in titles:
                titles[name] = {"folder": name, "seasons": {}, "total_size": 0, "is_movie": False}
            entry = titles[name]

            # First pass: direct video files in the title folder (no season subfolder)
            for f in folder.iterdir():
                if not is_video_file(f):
                    continue
                if _LIB_EP_RE.search(f.name) or _LIB_FALLBACK_EP_RE.search(f.name):
                    continue
                try:
                    fsize = f.stat().st_size
                except OSError:
                    fsize = 0
                skey = "movies"
                if skey not in entry["seasons"]:
                    entry["seasons"][skey] = []
                if not any(e["file"] == f.name for e in entry["seasons"][skey]):
                    mdata = resolved_media_data.get(f) or {}
                    entry["seasons"][skey].append({
                        "episode": 1, "file": f.name, "size": fsize, "is_video": True,
                        "is_movie_file": True, "path": str(f),
                        "resolution": mdata.get("resolution"),
                        "video_codec": mdata.get("video_codec"),
                        "audio_codec": mdata.get("audio_codec")
                    })
                    entry["total_size"] += fsize
                    entry["is_movie"] = True

            # Second pass: recurse into subfolders for SxxExx episodes
            for f in folder.rglob("*"):
                if not is_video_file(f):
                    continue
                m = _LIB_EP_RE.search(f.name)
                if m:
                    snum = int(m.group(1))
                    enum = int(m.group(2))
                else:
                    m2 = _LIB_FALLBACK_EP_RE.search(f.name)
                    if m2:
                        snum = 1
                        enum = int(m2.group(1))
                    else:
                        continue
                try:
                    fsize = f.stat().st_size
                except OSError:
                    fsize = 0
                skey = str(snum)
                if skey not in entry["seasons"]:
                    entry["seasons"][skey] = []
                if not any(e["episode"] == enum and e["file"] == f.name for e in entry["seasons"][skey]):
                    mdata = resolved_media_data.get(f) or {}
                    entry["seasons"][skey].append({
                        "episode": enum, "file": f.name, "size": fsize, "is_video": True,
                        "path": str(f),
                        "resolution": mdata.get("resolution"),
                        "video_codec": mdata.get("video_codec"),
                        "audio_codec": mdata.get("audio_codec")
                    })
                    entry["total_size"] += fsize

        result = []
        for entry in sorted(titles.values(), key=lambda x: x["folder"].lower()):
            if not any(entry["seasons"].values()):
                continue
            total_eps = sum(sum(1 for e in eps if e.get("is_video", True)) for eps in entry["seasons"].values())
            for skey in entry["seasons"]:
                if skey != "movies":
                    entry["seasons"][skey].sort(key=lambda e: e["episode"])
            result.append({"folder": entry["folder"], "seasons": entry["seasons"],
                           "total_episodes": total_eps, "total_size": entry["total_size"],
                           "is_movie": entry["is_movie"]})
        return result

    def _lib_build_scan_targets():
        from pathlib import Path
        dl_base = _lib_resolve_base()
        targets = [("Default", None, dl_base)]
        for cp in get_custom_paths():
            cp_base = Path(cp["path"]).expanduser()
            if not cp_base.is_absolute():
                cp_base = Path.home() / cp_base
            targets.append((cp["name"], cp["id"], cp_base))
        return targets

    def _lib_do_scan(targets, lang_sep):
        """Perform a full scan and store results in the cache. Runs in background thread."""
        from pathlib import Path
        
        # Build lookup from old cache to optimize scans
        old_cache_lookup = {}
        try:
            cache = get_all_library_cache()
            for pk, entry in cache.items():
                if entry and entry.get("data"):
                    data = entry["data"]
                    t_list = []
                    if data.get("titles"):
                        t_list.extend(data["titles"])
                    if data.get("lang_folders"):
                        for lf in data["lang_folders"]:
                            if lf.get("titles"):
                                t_list.extend(lf["titles"])
                    for t in t_list:
                        for skey, eps in t.get("seasons", {}).items():
                            for ep in eps:
                                if ep.get("path"):
                                    old_cache_lookup[(ep["path"], ep.get("size"))] = {
                                        "resolution": ep.get("resolution"),
                                        "video_codec": ep.get("video_codec"),
                                        "audio_codec": ep.get("audio_codec")
                                    }
        except Exception as e:
            logger.warning("[LibraryScan] Failed to build resolution cache lookup: %s", e)

        for (label, cp_id, base_path) in targets:
            path_key = "default" if cp_id is None else str(cp_id)
            set_library_scanning(path_key, True)
            try:
                if lang_sep:
                    loc_lang_folders = []
                    for lf in _LIB_LANG_FOLDERS:
                        lf_titles = _lib_scan_base(base_path / lf, old_cache_lookup)
                        if lf_titles:
                            loc_lang_folders.append({"name": lf, "titles": lf_titles})
                    set_library_cache(path_key, {
                        "label": label, "custom_path_id": cp_id,
                        "lang_folders": loc_lang_folders, "titles": None,
                    })
                else:
                    loc_titles = _lib_scan_base(base_path, old_cache_lookup)
                    set_library_cache(path_key, {
                        "label": label, "custom_path_id": cp_id,
                        "lang_folders": None, "titles": loc_titles,
                    })
            except Exception:
                set_library_scanning(path_key, False)
            else:
                # is_scanning is already set to 0 by set_library_cache
                pass

    def _lib_trigger_scan_async(targets, lang_sep):
        import threading
        t = threading.Thread(target=_lib_do_scan, args=(targets, lang_sep), daemon=True)
        t.start()

    @app.route("/api/library")
    def api_library():
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        targets = _lib_build_scan_targets()
        cache = get_all_library_cache()

        locations = []
        any_scanning = False
        needs_initial_scan = []

        for (label, cp_id, base_path) in targets:
            path_key = "default" if cp_id is None else str(cp_id)
            entry = cache.get(path_key)
            if entry:
                if entry["is_scanning"]:
                    any_scanning = True
                if entry["data"]:
                    locations.append(entry["data"])
            else:
                # Never scanned yet — trigger once
                needs_initial_scan.append((label, cp_id, base_path))

        if needs_initial_scan and not any_scanning:
            _lib_trigger_scan_async(needs_initial_scan, lang_sep)
            any_scanning = True

        # Watcher status
        watcher = _get_lib_watcher()
        last_updated = max((e["scanned_at"] for e in cache.values()), default=0)

        return jsonify({
            "lang_sep": lang_sep,
            "locations": locations,
            "is_scanning": any_scanning,
            "last_updated": last_updated,
            "watcher": {
                "available": watcher.available,
                "active": watcher.active,
                "watched": watcher.watched,
            },
        })

    @app.route("/api/library/refresh", methods=["POST"])
    def api_library_refresh():
        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        targets = _lib_build_scan_targets()
        invalidate_library_cache()
        _lib_trigger_scan_async(targets, lang_sep)
        # Restart watcher so it picks up any newly configured paths
        _lib_watcher.restart(targets, _lib_watcher_scan_callback)
        return jsonify({"ok": True, "scanning": True})

    @app.route("/api/library/status")
    def api_library_status():
        """Lightweight endpoint: returns only scanning state + last_updated timestamp.
        Used by the UI to detect watcher-triggered rescans without transferring location data."""
        cache = get_all_library_cache()
        any_scanning = any(e["is_scanning"] for e in cache.values())
        last_updated = max((e["scanned_at"] for e in cache.values()), default=0)
        return jsonify({"is_scanning": any_scanning, "last_updated": last_updated})

    @app.route("/api/library/watcher")
    def api_library_watcher():
        watcher = _get_lib_watcher()
        return jsonify({
            "available": watcher.available,
            "active": watcher.active,
            "watched": watcher.watched,
        })

    def _lib_assert_within_root(path, root):
        """Resolve path and verify it stays within root — blocks symlink escapes.
        Returns the resolved Path on success, raises ValueError on violation."""
        from pathlib import Path as _P
        resolved = _P(path).resolve()
        resolved_root = _P(root).resolve()
        resolved.relative_to(resolved_root)  # raises ValueError if outside
        return resolved

    @app.route("/api/library/delete", methods=["POST"])
    def api_library_delete():
        import shutil
        from pathlib import Path

        data = request.get_json(silent=True) or {}
        folder = data.get("folder", "")
        season = data.get("season")  # int or null
        episode = data.get("episode")  # int or null
        custom_path_id = data.get("custom_path_id")  # int or null

        # Security: reject dangerous folder names
        if (
            not folder
            or ".." in folder
            or "/" in folder
            or "\\" in folder
            or "\x00" in folder
        ):
            return jsonify({"error": "Invalid folder name"}), 400

        # Resolve base path from custom_path_id or default
        if custom_path_id:
            cp = get_custom_path_by_id(custom_path_id)
            if not cp:
                return jsonify({"error": "Custom path not found"}), 404
            dl_base = Path(cp["path"]).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            if raw:
                dl_base = Path(raw).expanduser()
                if not dl_base.is_absolute():
                    dl_base = Path.home() / dl_base
            else:
                dl_base = Path.home() / "Downloads"

        # Resolve the base itself to eliminate symlinks in the configured path
        dl_base = dl_base.resolve()

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        lang_folders = ["german-dub", "english-sub", "german-sub", "english-dub"]
        lang_folder = data.get("lang_folder")  # str or null

        if lang_sep and lang_folder:
            if lang_folder not in lang_folders:
                return jsonify({"error": "Invalid language folder"}), 400
            bases = [dl_base / lang_folder]
        elif lang_sep:
            bases = [dl_base / lf for lf in lang_folders]
        else:
            bases = [dl_base]

        deleted = 0
        for base in bases:
            title_path = base / folder
            # Verify resolved path stays within the allowed base (blocks symlink escapes)
            try:
                title_path = _lib_assert_within_root(title_path, base)
            except ValueError:
                continue
            if not title_path.is_dir():
                continue

            if season is None and episode is None:
                # Delete entire title
                shutil.rmtree(title_path, ignore_errors=True)
                deleted += 1
            else:
                # Build regex pattern
                if episode is not None:
                    pat = re.compile(
                        rf"S{int(season):02d}E{int(episode):03d}(?!\d)", re.IGNORECASE
                    )
                else:
                    pat = re.compile(rf"S{int(season):02d}E\d{{2,3}}", re.IGNORECASE)

                for f in list(title_path.rglob("*")):
                    if f.is_file() and pat.search(f.name):
                        try:
                            f.unlink()
                            deleted += 1
                        except OSError:
                            pass

                # Cleanup empty directories bottom-up
                for dirpath in sorted(
                    title_path.rglob("*"), key=lambda p: len(p.parts), reverse=True
                ):
                    if dirpath.is_dir():
                        try:
                            dirpath.rmdir()  # only succeeds if empty
                        except OSError:
                            pass
                # Remove title folder itself if empty
                try:
                    title_path.rmdir()
                except OSError:
                    pass

        if deleted == 0:
            return jsonify({"error": "Nothing found to delete"}), 404
        invalidate_library_cache()
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/api/library/media_info", methods=["POST"])
    def api_library_media_info():
        from pathlib import Path
        import subprocess
        import json

        data = request.get_json(silent=True) or {}
        path = data.get("path")
        if not path:
            return jsonify({"error": "Path required"}), 400

        # Security check: check if the path is within any scanned library base
        targets = _lib_build_scan_targets()
        path_obj = Path(path).resolve()

        allowed = False
        for (_, _, base_path) in targets:
            try:
                base_resolved = base_path.resolve()
                path_obj.relative_to(base_resolved)
                allowed = True
                break
            except ValueError:
                continue

        if not allowed:
            return jsonify({"error": "Access denied"}), 403

        if not path_obj.is_file():
            return jsonify({"error": "File not found"}), 404

        # Run ffprobe
        try:
            from .transcoder import _ffprobe_bin
            ffprobe = _ffprobe_bin()
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path_obj)],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                return jsonify({"error": "ffprobe failed"}), 500
            probe_data = json.loads(r.stdout)
        except Exception as e:
            return jsonify({"error": f"Failed to run ffprobe: {e}"}), 500

        fmt = probe_data.get("format", {})
        streams = probe_data.get("streams", [])

        # 1. Basic properties
        info = {
            "filename": path_obj.name,
            "container": path_obj.suffix.lstrip(".").lower(),
            "path": str(path_obj),
            "size_bytes": path_obj.stat().st_size,
        }

        # 2. Extract Video & Audio Streams
        video = None
        audio = None

        for s in streams:
            ct = s.get("codec_type")
            if ct == "video" and not video:
                v_codec = s.get("codec_name", "").upper()
                v_profile = s.get("profile", "Unknown")
                v_level = s.get("level")
                v_level_str = str(v_level) if v_level is not None else ""

                # Width x Height
                w = s.get("width", 0)
                h = s.get("height", 0)
                res_str = f"{w}x{h}" if w and h else ""

                # Aspect ratio
                dar = s.get("display_aspect_ratio", "")

                # Framerate
                r_fr = s.get("r_frame_rate", "")
                framerate = ""
                if r_fr and "/" in r_fr:
                    try:
                        num, den = map(int, r_fr.split("/"))
                        if den > 0:
                            framerate = f"{round(num / den)}"
                    except ValueError:
                        pass

                # Bit depth
                pix_fmt = s.get("pix_fmt", "")
                bit_depth = 8
                if "10" in pix_fmt:
                    bit_depth = 10
                elif "12" in pix_fmt:
                    bit_depth = 12

                # Video range
                color_tr = s.get("color_transfer", "")
                v_range = "SDR"
                if color_tr in ["smpte2084", "arib-std-b67"]:
                    v_range = "HDR"

                # Bitrate
                v_br = s.get("bit_rate") or fmt.get("bit_rate")
                v_bitrate_kbps = ""
                if v_br:
                    try:
                        v_bitrate_kbps = f"{int(v_br) // 1000} kbps"
                    except ValueError:
                        pass

                # AVC
                is_avc = "Yes" if s.get("is_avc") in [True, "true", "1", 1] else "No"

                # Refs & NAL
                refs = s.get("refs", "")
                nal = s.get("nal_length_size", "")

                video = {
                    "codec": v_codec,
                    "profile": v_profile,
                    "level": v_level_str,
                    "resolution": res_str,
                    "aspect_ratio": dar,
                    "framerate": framerate,
                    "bit_depth": f"{bit_depth} bit",
                    "video_range": v_range,
                    "pixel_format": pix_fmt,
                    "bitrate": v_bitrate_kbps,
                    "avc": is_avc,
                    "refs": str(refs) if refs != "" else "",
                    "nal": str(nal) if nal != "" else "",
                }

            elif ct == "audio" and not audio:
                a_codec = s.get("codec_name", "").upper()
                a_profile = s.get("profile", "Unknown")

                # Channels & Layout
                channels = s.get("channels", "")
                layout = s.get("channel_layout", "")

                # Language
                lang = s.get("tags", {}).get("language", "und")

                # Bitrate
                a_br = s.get("bit_rate")
                a_bitrate_kbps = ""
                if a_br:
                    try:
                        a_bitrate_kbps = f"{int(a_br) // 1000} kbps"
                    except ValueError:
                        pass

                # Sample rate
                sr = s.get("sample_rate", "")
                sr_str = f"{sr} Hz" if sr else ""

                # Default / Forced
                disp = s.get("disposition", {})
                is_default = "Yes" if disp.get("default") == 1 else "No"
                is_forced = "Yes" if disp.get("forced") == 1 else "No"

                audio = {
                    "codec": a_codec,
                    "profile": a_profile,
                    "channels": f"{channels} ch" if channels else "",
                    "layout": layout,
                    "language": lang,
                    "bitrate": a_bitrate_kbps,
                    "sample_rate": sr_str,
                    "default": is_default,
                    "forced": is_forced,
                }

        info["video"] = video
        info["audio"] = audio
        return jsonify(info)

    @app.route("/api/library/rename", methods=["POST"])
    def api_library_rename():
        from pathlib import Path
        data = request.get_json(silent=True) or {}
        folder    = data.get("folder", "")
        new_name  = data.get("new_name", "").strip()
        season    = data.get("season")      # int → rename season folder; None → rename title folder
        episode   = data.get("episode")     # int → rename specific episode file; None → season/title level
        old_file  = data.get("old_file")    # original filename for episode rename
        custom_path_id = data.get("custom_path_id")
        lang_folder    = data.get("lang_folder")

        # Validate inputs
        def _safe(name):
            return name and ".." not in name and "/" not in name and "\\" not in name and "\x00" not in name

        if not _safe(folder) or not new_name:
            return jsonify({"error": "Invalid folder or new name"}), 400
        if not _safe(new_name):
            return jsonify({"error": "New name contains invalid characters"}), 400

        # Resolve base path
        if custom_path_id:
            cp = get_custom_path_by_id(custom_path_id)
            if not cp:
                return jsonify({"error": "Custom path not found"}), 404
            dl_base = Path(cp["path"]).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            dl_base = Path(raw).expanduser() if raw else Path.home() / "Downloads"
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        if lang_sep and lang_folder:
            if lang_folder not in _LIB_LANG_FOLDERS:
                return jsonify({"error": "Invalid language folder"}), 400
            base = dl_base / lang_folder
        else:
            base = dl_base

        # Resolve base to eliminate symlinks in the configured path
        base = base.resolve()

        try:
            title_path = _lib_assert_within_root(base / folder, base)
        except ValueError:
            return jsonify({"error": "Path traversal detected"}), 400

        if episode is not None and old_file:
            # Rename a specific episode file
            if season is None:
                return jsonify({"error": "season required for episode rename"}), 400
            season_path = title_path / ("Staffel " + str(int(season)))
            if not season_path.is_dir():
                # Try without Staffel prefix — flat layout
                season_path = title_path
            try:
                src = _lib_assert_within_root(season_path / old_file, base)
            except ValueError:
                return jsonify({"error": "Path traversal detected"}), 400
            if not src.is_file():
                return jsonify({"error": "File not found"}), 404
            dst = src.parent / new_name
            if dst.exists():
                return jsonify({"error": "Target name already exists"}), 409
            src.rename(dst)
        else:
            # Rename title folder
            if not title_path.is_dir():
                return jsonify({"error": "Folder not found"}), 404
            dst = title_path.parent / new_name
            if dst.exists():
                return jsonify({"error": "Target name already exists"}), 409
            title_path.rename(dst)

        invalidate_library_cache()
        return jsonify({"ok": True})

    def _lib_move_resolve_base(cp_id):
        """Resolve a custom_path_id (or None for default) to an absolute, symlink-free Path."""
        from pathlib import Path
        if cp_id:
            cp = get_custom_path_by_id(cp_id)
            if not cp:
                return None
            p = Path(cp["path"]).expanduser()
        else:
            raw = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
            p = Path(raw).expanduser() if raw else Path.home() / "Downloads"
        p = p if p.is_absolute() else Path.home() / p
        return p.resolve()

    def _lib_move_worker(job_id, src, dst):
        """Background thread: copy src→dst with progress tracking, then delete src."""
        import shutil
        from pathlib import Path
        job = _move_jobs[job_id]
        try:
            # Calculate total bytes
            all_files = [f for f in Path(src).rglob("*") if f.is_file()]
            total = sum(f.stat().st_size for f in all_files)
            with _move_jobs_lock:
                job["total_bytes"] = total
                job["status"] = "running"

            copied = 0
            dst_path = Path(dst)
            src_path = Path(src)
            dst_path.mkdir(parents=True, exist_ok=True)

            for src_file in all_files:
                rel = src_file.relative_to(src_path)
                dst_file = dst_path / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                with _move_jobs_lock:
                    job["current_file"] = str(rel)
                # buffered copy for progress
                with open(src_file, "rb") as fin, open(dst_file, "wb") as fout:
                    while True:
                        buf = fin.read(256 * 1024)  # 256 KB chunks
                        if not buf:
                            break
                        fout.write(buf)
                        copied += len(buf)
                        with _move_jobs_lock:
                            job["copied_bytes"] = copied
                try:
                    shutil.copystat(str(src_file), str(dst_file))
                except Exception:
                    pass

            # Also copy empty directories
            for src_dir in sorted(Path(src).rglob("*")):
                if src_dir.is_dir():
                    rel = src_dir.relative_to(src_path)
                    (dst_path / rel).mkdir(parents=True, exist_ok=True)

            # Delete source
            shutil.rmtree(str(src))
            invalidate_library_cache()
            with _move_jobs_lock:
                job["status"] = "done"
                job["current_file"] = ""
        except Exception as exc:
            logger.error("[LibMove] Move job %s failed: %s", job_id, exc, exc_info=True)
            # Clean up partial destination
            try:
                import shutil as _sh
                _sh.rmtree(str(dst), ignore_errors=True)
            except Exception:
                pass
            with _move_jobs_lock:
                job["status"] = "error"
                job["error"] = str(exc)


    def _lib_move_loose_files_worker(job_id, file_paths, dst_dir):
        """Background thread: move individual loose files (movies in root) to dst_dir."""
        import shutil
        from pathlib import Path
        job = _move_jobs[job_id]
        try:
            files = [Path(p) for p in file_paths]
            total = sum(f.stat().st_size for f in files if f.exists())
            with _move_jobs_lock:
                job["total_bytes"] = total
                job["status"] = "running"

            dst_path = Path(dst_dir)
            dst_path.mkdir(parents=True, exist_ok=True)
            copied = 0
            for src_file in files:
                if not src_file.exists():
                    continue
                dst_file = dst_path / src_file.name
                with _move_jobs_lock:
                    job["current_file"] = src_file.name
                with open(src_file, "rb") as fin, open(dst_file, "wb") as fout:
                    while True:
                        buf = fin.read(256 * 1024)
                        if not buf:
                            break
                        fout.write(buf)
                        copied += len(buf)
                        with _move_jobs_lock:
                            job["copied_bytes"] = copied
                try:
                    shutil.copystat(str(src_file), str(dst_file))
                except Exception:
                    pass
                src_file.unlink()

            invalidate_library_cache()
            with _move_jobs_lock:
                job["status"] = "done"
                job["current_file"] = ""
        except Exception as exc:
            logger.error("[LibMove] Loose file move job %s failed: %s", job_id, exc, exc_info=True)
            with _move_jobs_lock:
                job["status"] = "error"
                job["error"] = str(exc)

    @app.route("/api/library/move", methods=["POST"])
    def api_library_move():
        """Start an async move job. Returns {job_id} immediately."""
        import uuid
        from pathlib import Path
        data = request.get_json(silent=True) or {}
        folder      = data.get("folder", "")
        from_cp_id  = data.get("from_custom_path_id")
        to_cp_id    = data.get("to_custom_path_id")
        lang_folder = data.get("lang_folder")

        def _safe(name):
            return name and ".." not in name and "/" not in name and "\\" not in name and "\x00" not in name

        if not _safe(folder):
            return jsonify({"error": "Invalid folder name"}), 400

        from_base = _lib_move_resolve_base(from_cp_id)
        to_base   = _lib_move_resolve_base(to_cp_id)
        if from_base is None or to_base is None:
            return jsonify({"error": "Invalid path configuration"}), 400

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        if lang_sep and lang_folder:
            if lang_folder not in _LIB_LANG_FOLDERS:
                return jsonify({"error": "Invalid language folder"}), 400
            from_base = from_base / lang_folder
            to_base   = to_base   / lang_folder

        src = (from_base / folder).resolve()
        try:
            src.relative_to(from_base.resolve())
        except ValueError:
            return jsonify({"error": "Path traversal detected"}), 400

        # Check if source is a directory (series) or loose files directly in base (movie)
        loose_files = []
        if not src.is_dir():
            # Movie files sitting directly in the base folder (e.g. Film.mkv, Film.srt)
            loose_files = [f for f in from_base.iterdir()
                           if f.is_file() and f.stem == folder]
            if not loose_files:
                return jsonify({"error": "Source folder not found"}), 404

        if loose_files:
            # Loose files → move each file to to_base (no subfolder)
            dst = to_base
            for lf in loose_files:
                if (dst / lf.name).exists():
                    return jsonify({"error": "Ziel existiert bereits am Speicherort"}), 409
        else:
            dst = to_base / folder
            if dst.resolve() == src.resolve():
                return jsonify({"error": "Quelle und Ziel sind identisch"}), 400
            if dst.exists():
                return jsonify({"error": "Ziel existiert bereits am Speicherort"}), 409

        try:
            to_base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"Zielordner konnte nicht erstellt werden: {exc}"}), 500

        job_id = uuid.uuid4().hex[:12]
        with _move_jobs_lock:
            _move_jobs[job_id] = {
                "status": "starting",
                "copied_bytes": 0,
                "total_bytes": 0,
                "current_file": "",
                "error": None,
                "folder": folder,
            }

        if loose_files:
            t = threading.Thread(
                target=_lib_move_loose_files_worker,
                args=(job_id, [str(f) for f in loose_files], str(dst)),
                daemon=True,
            )
        else:
            t = threading.Thread(target=_lib_move_worker, args=(job_id, str(src), str(dst)), daemon=True)
        t.start()
        return jsonify({"job_id": job_id})

    @app.route("/api/library/move_status/<job_id>")
    def api_library_move_status(job_id):
        """Poll move job progress."""
        with _move_jobs_lock:
            job = _move_jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Job nicht gefunden"}), 404
            result = dict(job)
            # Clean up finished jobs after first poll of final state
            if job["status"] in ("done", "error"):
                _move_jobs.pop(job_id, None)
        return jsonify(result)

    # ===== External REST API v1 =====
    #
    # All endpoints live under /api/v1/ and require authentication via:
    #   - HTTP header:  X-Api-Key: <key>
    #
    # The key is auto-generated on first run and stored in app_settings.
    # It can be viewed / regenerated from the Settings page (Admin only).

    from flask import Response as _FlaskResponse

    def _v1_json(data, status=200):
        """Pretty-printed JSON response for all /api/v1/ endpoints."""
        body = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
        return _FlaskResponse(body, status=status, mimetype="application/json")

    def _check_api_key():
        """Return a 401 JSON response if the API key is invalid, else None."""
        stored = get_setting("external_api_key", "")
        if not stored:
            return jsonify({"error": "API key not configured"}), 500
        provided = request.headers.get("X-Api-Key", "")
        if not provided or not secrets.compare_digest(provided, stored):
            return _v1_json({
                "error": "Unauthorized",
                "message": "Provide your API key via the X-Api-Key header.",
            }, status=401)
        return None

    @app.route("/api/v1/status")
    def api_v1_status():
        """Overall downloader status — safe to poll frequently."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        from ..models.common.common import get_ffmpeg_progress
        stats  = get_queue_stats()
        ffmpeg = get_ffmpeg_progress()
        r      = stats["currently_running"]

        if r:
            cur = r.get("current_episode") or 0
            tot = r.get("total_episodes") or 0
            # current_episode = i (0-based loop index) → i episodes fully done,
            # episode i+1 is in progress.  Mirror queue.js logic exactly:
            #   epPct  = cur / tot * 100
            #   inEpPct = 100 if ffmpeg phase (download done), else dl percent
            #   overall = epPct + inEpPct / tot
            ep_pct   = round(ffmpeg.get("percent") or 0) if ffmpeg.get("active") else 0
            in_ep    = 100 if (ffmpeg.get("active") and ffmpeg.get("phase") == "ffmpeg") else ep_pct
            overall_pct = round(((cur + in_ep / 100) / tot * 100) if tot > 0 else 0)
            r["episode_progress"] = {
                "percent":       ep_pct,
                "phase":         ffmpeg.get("phase", ""),
                "speed":         ffmpeg.get("speed", ""),
                "bandwidth":     ffmpeg.get("bandwidth", ""),
                "downloaded_mb": round(ffmpeg.get("downloaded_mb", 0.0), 1),
                "active":        ffmpeg.get("active", False),
            }
            r["overall_progress_percent"] = overall_pct

        return _v1_json({
            "version": app_version,
            "paused": is_queue_paused(),
            "queue": {
                "total":     stats["total"],
                "queued":    stats["by_status"].get("queued", 0),
                "running":   stats["by_status"].get("running", 0),
                "completed": stats["by_status"].get("completed", 0),
                "failed":    stats["by_status"].get("failed", 0),
                "cancelled": stats["by_status"].get("cancelled", 0),
            },
            "currently_running": r,
        })

    @app.route("/api/v1/queue")
    def api_v1_queue():
        """All queue items, optionally filtered by ?status=<status>."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        items = get_queue()
        status_filter = request.args.get("status", "").strip().lower()
        if status_filter:
            items = [i for i in items if i.get("status") == status_filter]
        for item in items:
            if isinstance(item.get("episodes"), str):
                try:
                    item["episodes"] = json.loads(item["episodes"])
                except Exception:
                    pass
        return _v1_json(items)

    @app.route("/api/v1/queue/<int:queue_id>")
    def api_v1_queue_item(queue_id):
        """Single queue item detail."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        item = get_queue_item(queue_id)
        if not item:
            return _v1_json({"error": "Not found"}, status=404)
        if isinstance(item.get("episodes"), str):
            try:
                item["episodes"] = json.loads(item["episodes"])
            except Exception:
                pass
        return _v1_json(item)

    def _v1_library_data(only_movies: bool | None = None):
        """Return library cache as a clean list of location objects."""
        cache = get_all_library_cache()
        locations = []
        for path_key, entry in cache.items():
            loc_data = entry.get("data") or {}
            label        = loc_data.get("label", path_key)
            cp_id        = loc_data.get("custom_path_id")
            is_scanning  = entry.get("is_scanning", False)
            scanned_at   = entry.get("scanned_at")

            all_titles = []
            lang_folders = loc_data.get("lang_folders") or []
            if lang_folders:
                for lf in lang_folders:
                    for t in (lf.get("titles") or []):
                        all_titles.append({**t, "_lang_folder": lf.get("name")})
            else:
                for t in (loc_data.get("titles") or []):
                    all_titles.append(t)

            if only_movies is True:
                all_titles = [t for t in all_titles if t.get("is_movie")]
            elif only_movies is False:
                all_titles = [t for t in all_titles if not t.get("is_movie")]

            clean_titles = []
            for t in all_titles:
                seasons_clean = {}
                for skey, eps in (t.get("seasons") or {}).items():
                    seasons_clean[skey] = [
                        {
                            "episode":       e.get("episode"),
                            "file":          e.get("file"),
                            "size":          e.get("size", 0),
                            "is_movie_file": e.get("is_movie_file", False),
                        }
                        for e in eps
                    ]
                clean_titles.append({
                    "folder":         t.get("folder"),
                    "is_movie":       t.get("is_movie", False),
                    "total_episodes": t.get("total_episodes", 0),
                    "total_size":     t.get("total_size", 0),
                    "lang_folder":    t.get("_lang_folder"),
                    "seasons":        seasons_clean,
                })

            locations.append({
                "location":       label,
                "custom_path_id": cp_id,
                "is_scanning":    is_scanning,
                "scanned_at":     scanned_at,
                "title_count":    len(clean_titles),
                "titles":         clean_titles,
            })
        return _v1_json(locations)

    @app.route("/api/v1/library")
    def api_v1_library():
        """Full library — all titles (series + movies)."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=None)

    @app.route("/api/v1/library/series")
    def api_v1_library_series():
        """Library — series only (no movies)."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=False)

    @app.route("/api/v1/library/movies")
    def api_v1_library_movies():
        """Library — movies only."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_library_data(only_movies=True)

    @app.route("/api/v1/stats")
    def api_v1_stats():
        """Download statistics."""
        auth_err = _check_api_key()
        if auth_err:
            return auth_err
        return _v1_json(get_general_stats())

    @app.route("/api/settings/api-key", methods=["GET"])
    def api_settings_api_key_get():
        """Return the current external API key."""
        key = get_setting("external_api_key", "")
        return jsonify({"key": key})

    @app.route("/api/settings/api-key/regenerate", methods=["POST"])
    def api_settings_api_key_regenerate():
        """Generate a new external API key."""
        new_key = secrets.token_hex(32)
        set_setting("external_api_key", new_key)
        return jsonify({"ok": True, "key": new_key})

    # ===== Captcha API =====

    def _captcha_access_allowed(queue_id):
        """Return True if the current session may interact with this captcha."""
        from flask import session as _sess
        if _sess.get("user_role") == "admin":
            return True
        item = get_queue_item(queue_id)
        if not item:
            return False
        return item.get("username") == _sess.get("user_name")

    @app.route("/api/captcha/<int:queue_id>/screenshot")
    def api_captcha_screenshot(queue_id):
        """Stream the latest captcha screenshot for a running queue item."""
        if not _captcha_access_allowed(queue_id):
            return "", 403
        from ..playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            captcha_sess = _captcha_mod._active_sessions.get(queue_id)
        if captcha_sess is None:
            return "", 204
        data = captcha_sess.get_screenshot()
        if not data:
            return "", 204
        from flask import Response
        return Response(data, mimetype="image/jpeg")

    @app.route("/api/captcha/<int:queue_id>/click", methods=["POST"])
    def api_captcha_click(queue_id):
        """Forward a click coordinate to the captcha browser."""
        if not _captcha_access_allowed(queue_id):
            return jsonify({"error": "Forbidden"}), 403
        from ..playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            captcha_sess = _captcha_mod._active_sessions.get(queue_id)
        if captcha_sess is None:
            return jsonify({"error": "No active captcha session"}), 404
        data = request.get_json(silent=True) or {}
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        captcha_sess.enqueue_click(x, y)
        return jsonify({"ok": True})

    @app.route("/api/captcha/<int:queue_id>/status")
    def api_captcha_status(queue_id):
        """Return whether a captcha session is active for the given queue item."""
        if not _captcha_access_allowed(queue_id):
            return jsonify({"error": "Forbidden"}), 403
        from ..playwright import captcha as _captcha_mod
        with _captcha_mod._active_sessions_lock:
            active = queue_id in _captcha_mod._active_sessions
        return jsonify({"active": active})

    # ================================================================
    # Streaming / Transcoder
    # ================================================================

    def _stream_cors_origin():
        """Return the allowed CORS origin for HLS stream responses.
        Reflects the request Origin only when it matches the app host,
        so the streams are not accessible cross-origin if the token leaks."""
        req_origin = request.headers.get("Origin", "")
        app_origin = request.host_url.rstrip("/")
        return req_origin if req_origin == app_origin else app_origin

    @app.route("/api/stream/check")
    def api_stream_check():
        """Return available encoder info (no ffmpeg process started)."""
        from .transcoder import get_best_encoder, detect_available_encoders
        import shutil as _s
        if not _s.which("ffmpeg"):
            return jsonify({"available": False, "reason": "ffmpeg nicht gefunden"})
        all_enc = detect_available_encoders()
        encoder, is_hw = get_best_encoder()
        if not encoder:
            return jsonify({"available": False, "reason": "Kein kompatibler H.264-Encoder gefunden",
                            "all": all_enc})
        return jsonify({"available": True, "encoder": encoder, "is_hardware": is_hw,
                        "all": all_enc})

    @app.route("/api/stream/reset-encoders", methods=["POST"])
    def api_stream_reset_encoders():
        """Clear encoder cache — forces re-detection on next request."""
        nonlocal _detect_hw_cache, _detect_hw_cache_at
        from .transcoder import reset_encoder_cache
        reset_encoder_cache()
        with _detect_hw_lock:
            _detect_hw_cache = None
            _detect_hw_cache_at = 0.0
        return jsonify({"ok": True, "message": "Encoder-Cache geleert"})

    @app.route("/api/stream/start-source", methods=["POST"])
    def api_stream_start_source():
        """Stream an episode directly from its provider (no prior download).

        Body: {episode_url, provider?, language?, start_pos?}
        Resolves the provider's direct stream URL on demand and feeds it to the
        transcoder with the provider's HTTP headers.
        """
        from .transcoder import start_session, probe_file

        data        = request.get_json(force=True, silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        provider    = (data.get("provider") or "VOE").strip()
        language    = (data.get("language") or "German Dub").strip()
        start_pos   = float(data.get("start_pos", 0) or 0)

        if not episode_url:
            return jsonify({"error": "episode_url fehlt"}), 400

        # ── Resolve the direct stream URL via the model/extractor layer ──
        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(
                url=episode_url,
                selected_language=language,
                selected_provider=provider,
            )
            stream_url = episode.stream_url
        except Exception as exc:
            logger.warning("[StreamSource] resolve failed for %s (%s/%s): %s",
                           episode_url, provider, language, exc)
            return jsonify({"error": f"Stream konnte nicht aufgelöst werden: {exc}"}), 502

        if not stream_url:
            return jsonify({"error": "Kein Stream-Link gefunden"}), 502

        # Provider-specific HTTP headers (Referer / User-Agent) for ffmpeg.
        try:
            from ..config import PROVIDER_HEADERS_D
            headers = dict(PROVIDER_HEADERS_D.get(provider, {}) or {})
        except Exception:
            headers = {}
        # Ensure ffmpeg treats the input as a remote source even if the
        # provider has no special headers configured.
        if not headers:
            headers = {"User-Agent": os.environ.get("MEDIAFORGE_USER_AGENT", "Mozilla/5.0")}

        # Probe the resolved stream so we can stream-copy when the source is
        # already browser-compatible (H.264/AAC) — this avoids re-encoding,
        # which is the main cause of stutter on slower machines.
        # Stream-copy when the source is already browser-compatible (least bad
        # of the ffmpeg options). The real fix for the residual stutter is the
        # passthrough proxy below, which avoids ffmpeg entirely for HLS sources.
        info = {}
        copy_video = False
        copy_audio = False
        try:
            info = probe_file(stream_url, headers=headers) or {}
            vcodec = (info.get("video_codec") or "").lower()
            acodec = (info.get("audio_codec") or "").lower()
            copy_video = vcodec in ("h264", "avc1")
            copy_audio = acodec in ("aac", "mp4a")
        except Exception as exc:
            logger.debug("[StreamSource] probe failed: %s", exc)

        actual_start = max(0.0, start_pos - 5.0)
        try:
            token, session = start_session(
                stream_url, actual_start, headers=headers,
                copy_video=copy_video, copy_audio=copy_audio,
            )
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        return jsonify({
            "token":       token,
            "encoder":     "copy" if copy_video else session.encoder,
            "start_pos":   actual_start,
            "duration":    info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "source":      True,
        })

    @app.route("/api/stream/start-proxy", methods=["POST"])
    def api_stream_start_proxy():
        """Play an episode by proxying its native provider HLS (no ffmpeg).

        Resolves the provider's stream URL + headers, then returns a proxied
        playlist URL the browser can hand straight to hls.js. This avoids the
        transcoder entirely and is the smooth, low-CPU path for HLS sources.
        """
        from .stream_proxy import create_proxy_session, b64e, is_safe_url

        data        = request.get_json(force=True, silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        provider    = (data.get("provider") or "VOE").strip()
        language    = (data.get("language") or "German Dub").strip()
        if not episode_url:
            return jsonify({"error": "episode_url fehlt"}), 400

        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(
                url=episode_url, selected_language=language, selected_provider=provider,
            )
            stream_url = episode.stream_url
        except Exception as exc:
            logger.warning("[StreamProxy] resolve failed for %s (%s/%s): %s",
                           episode_url, provider, language, exc)
            return jsonify({"error": f"Stream konnte nicht aufgelöst werden: {exc}"}), 502

        if not stream_url:
            return jsonify({"error": "Kein Stream-Link gefunden"}), 502
        # Only HLS can be proxied as a playlist; signal the client to fall back
        # to the transcoder otherwise (e.g. a direct .mp4).
        is_hls = ".m3u8" in stream_url.lower()
        if not is_safe_url(stream_url):
            return jsonify({"error": "Unsichere Stream-URL", "hls": is_hls}), 400

        try:
            from ..config import PROVIDER_HEADERS_D
            headers = dict(PROVIDER_HEADERS_D.get(provider, {}) or {})
        except Exception:
            headers = {}
        if not headers:
            headers = {"User-Agent": os.environ.get("MEDIAFORGE_USER_AGENT", "Mozilla/5.0")}

        token = create_proxy_session(headers)
        playlist_url = f"/api/proxy/{token}/r/{b64e(stream_url)}"
        return jsonify({"token": token, "playlist_url": playlist_url, "hls": is_hls, "source": True})

    @app.route("/api/proxy/<token>/r/<path:b64>")
    def api_proxy_resource(token, b64):
        """Fetch + (for playlists) rewrite a provider resource through the proxy."""
        from flask import Response as _Response
        from .stream_proxy import (get_proxy_session, b64d, fetch,
                                    is_playlist, rewrite_playlist, is_safe_url)
        sess = get_proxy_session(token)
        if not sess:
            return "Session not found", 404
        try:
            url = b64d(b64)
        except Exception:
            return "Bad resource", 400
        if not is_safe_url(url):
            return "Forbidden", 403
        try:
            code, up_headers, data, final_url = fetch(
                url, sess["headers"], request.headers.get("Range"))
        except Exception as exc:
            logger.debug("[StreamProxy] fetch failed: %s", exc)
            return jsonify({"error": "Upstream nicht erreichbar"}), 502

        if is_playlist(data):
            text = data.decode("utf-8", "replace")
            proxy_base = f"/api/proxy/{token}/r/"
            body = rewrite_playlist(text, final_url, proxy_base)
            resp = _Response(body, mimetype="application/vnd.apple.mpegurl")
        else:
            resp = _Response(data, status=code)
            for h in ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Length"):
                if h in up_headers:
                    resp.headers[h] = up_headers[h]
            if "Content-Type" not in up_headers:
                resp.headers["Content-Type"] = "video/mp2t"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/api/stream/close-proxy", methods=["POST"])
    def api_stream_close_proxy():
        from .stream_proxy import close_proxy_session
        data = request.get_json(silent=True) or {}
        tok = (data.get("token") or "").strip()
        if tok:
            close_proxy_session(tok)
        return jsonify({"ok": True})

    @app.route("/api/stream/start", methods=["POST"])
    def api_stream_start():
        """Start a transcode session. Body: {path, start_pos?}"""
        from .transcoder import start_session, probe_file
        from pathlib import Path as _Path
        from .db import get_custom_paths as _get_custom_paths

        data       = request.get_json(force=True, silent=True) or {}
        file_path  = data.get("path", "")
        start_pos  = float(data.get("start_pos", 0) or 0)

        if not file_path:
            return jsonify({"error": "Datei nicht gefunden"}), 404

        # Resolve path and validate against allowed library roots
        try:
            resolved = _Path(file_path).resolve()
        except Exception:
            return jsonify({"error": "Ungültiger Pfad"}), 400

        _raw_dl = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        _allowed_roots = []
        if _raw_dl:
            try:
                _allowed_roots.append(_Path(_raw_dl).expanduser().resolve())
            except Exception:
                pass
        else:
            _allowed_roots.append((_Path.home() / "Downloads").resolve())

        for _cp in _get_custom_paths():
            try:
                _allowed_roots.append(_Path(_cp["path"]).expanduser().resolve())
            except Exception:
                pass

        _path_ok = False
        for _root in _allowed_roots:
            try:
                resolved.relative_to(_root)
                _path_ok = True
                break
            except ValueError:
                pass

        if not _path_ok or not resolved.is_file():
            return jsonify({"error": "Datei nicht gefunden"}), 404

        # Probe first so we can return media info
        info = probe_file(str(resolved)) or {}

        # Start a bit before saved position for buffer
        actual_start = max(0.0, start_pos - 5.0)

        # SyncPlay: everyone in a room watches the same file at the same spot, so
        # share ONE transcode session (and its segments) instead of one ffmpeg
        # per viewer. The share key is derived from the room server-side.
        from .transcoder import start_or_join_session
        share_key = None
        _sp_tok = (data.get("syncplay_token") or "").strip()
        if _sp_tok:
            try:
                from . import syncplay_rooms as _sp
                _room = _sp.room_for_token(_sp_tok)
                if _room:
                    share_key = "sp:" + _room.name
            except Exception:
                share_key = None

        try:
            token, session = start_or_join_session(str(resolved), actual_start, share_key=share_key)
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        return jsonify({
            "token":      token,
            "encoder":    session.encoder,
            "start_pos":  session.start_pos,
            "duration":   info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "width":      info.get("width", 0),
            "height":     info.get("height", 0),
            "format":     info.get("format", ""),
        })

    @app.route("/api/stream/<token>/index.m3u8")
    def api_stream_playlist(token):
        """Serve the HLS master playlist for a session."""
        from .transcoder import get_session
        import time as _t
        sess = get_session(token)
        if not sess:
            return "Session not found", 404

        # Wait for the background thread to signal playlist readiness
        sess._playlist_ready.wait(timeout=30)
        if not (sess.playlist_path and os.path.exists(sess.playlist_path)):
            err = sess.error or "Timeout: kein Segment innerhalb von 30 s"
            logger.warning("[Stream] playlist not ready for %s: %s", token[:8], err)
            return jsonify({"error": err}), 503
        # Verify at least one .ts reference is present
        try:
            with open(sess.playlist_path) as _pf:
                if ".ts" not in _pf.read():
                    err = sess.error or "Playlist ohne Segmente"
                    return jsonify({"error": err}), 503
        except Exception:
            return jsonify({"error": "Playlist nicht lesbar"}), 503

        from flask import send_file
        resp = send_file(sess.playlist_path, mimetype="application/vnd.apple.mpegurl")
        resp.headers["Cache-Control"] = "no-cache, no-store"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        return resp

    @app.route("/api/stream/<token>/<path:segment>")
    def api_stream_segment(token, segment):
        """Serve a .ts segment for a session."""
        from .transcoder import get_session
        from pathlib import Path as _Path
        import re as _re
        import time as _t

        sess = get_session(token)
        if not sess or not sess.tmp_dir:
            return "Session not found", 404

        # Accept only safe bare filenames — no path separators, no traversal
        bare = _Path(segment).name
        if not _re.fullmatch(r"seg\d+\.ts", bare):
            return "Segment not found", 404

        tmp_dir = _Path(sess.tmp_dir).resolve()
        seg_path = (tmp_dir / bare).resolve()

        # Ensure the resolved path is still inside the session tmp dir
        try:
            seg_path.relative_to(tmp_dir)
        except ValueError:
            return "Segment not found", 404

        # Wait up to 5 s for the segment to be written; return 503 so hls.js retries
        deadline = _t.time() + 5
        while _t.time() < deadline:
            if seg_path.exists() and seg_path.stat().st_size > 0:
                break
            _t.sleep(0.1)

        if not (seg_path.exists() and seg_path.stat().st_size > 0):
            from flask import Response as _Resp
            return _Resp("Segment not yet available", status=503,
                         headers={"Retry-After": "1"})

        from flask import send_file
        resp = send_file(str(seg_path), mimetype="video/mp2t")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        return resp

    @app.route("/api/stream/stop", methods=["POST"])
    def api_stream_stop():
        """Stop a transcode session. Body: {token}"""
        from .transcoder import stop_session
        data  = request.get_json(force=True, silent=True) or {}
        token = data.get("token", "")
        if token:
            stop_session(token)
        return jsonify({"ok": True})

    @app.route("/api/stream/active")
    def api_stream_active():
        """Return active stream count (for sidebar badge)."""
        from .transcoder import active_count
        return jsonify({"count": active_count()})

    @app.route("/api/stream/<token>/status")
    def api_stream_status(token):
        """Poll session readiness: {ready, error, alive, stderr_tail}"""
        from .transcoder import get_session
        sess = get_session(token)
        if not sess:
            return jsonify({"ready": False, "error": "Session nicht gefunden", "alive": False})
        alive = sess.is_alive()
        # Check if playlist has segments
        ready = False
        if sess.playlist_path and os.path.exists(sess.playlist_path):
            try:
                with open(sess.playlist_path) as _pf:
                    ready = ".ts" in _pf.read()
            except Exception:
                pass
        # Try to read stderr tail (non-blocking peek)
        stderr_tail = ""
        if sess.process and sess.process.stderr:
            import select, os as _os
            try:
                # Non-blocking read on Windows via os.read with a small chunk
                fd = sess.process.stderr.fileno()
                # Drain up to 4 KB without blocking
                chunk = b""
                try:
                    import msvcrt
                    # Windows: check if data available
                    while msvcrt.kbhit() if False else True:
                        c = _os.read(fd, 4096)
                        if c:
                            chunk += c
                        break
                except Exception:
                    pass
                if chunk:
                    stderr_tail = chunk.decode(errors="replace")[-300:]
                    # Cache it on the session for death diagnosis
                    sess._stderr_buf = getattr(sess, "_stderr_buf", "") + stderr_tail
            except Exception:
                pass
        # If process died without segments, collect stderr
        if not alive and not ready:
            err = sess.error or "ffmpeg beendet ohne Ausgabe"
            if sess.process:
                try:
                    out = sess.process.stderr.read()
                    buf = getattr(sess, "_stderr_buf", "")
                    full = (buf + out.decode(errors="replace"))[-600:] if out else buf[-600:]
                    if full:
                        err = err + ": " + full
                        sess.error = err
                except Exception:
                    pass
            return jsonify({"ready": False, "error": err, "alive": False})
        return jsonify({"ready": ready, "error": sess.error, "alive": alive,
                        "stderr_tail": stderr_tail})

    # ================================================================
    # Watch Progress
    # ================================================================

    @app.route("/api/progress/save", methods=["POST"])
    def api_progress_save():
        data     = request.get_json(force=True, silent=True) or {}
        path     = data.get("path", "")
        position = float(data.get("position", 0) or 0)
        duration = float(data.get("duration", 0) or 0)
        if not path:
            return jsonify({"error": "path required"}), 400
        _user, _ = _get_current_user_info()
        save_watch_progress(path, position, duration, username=_user)
        return jsonify({"ok": True})

    @app.route("/api/progress/get")
    def api_progress_get():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        _user, _ = _get_current_user_info()
        return jsonify(get_watch_progress(path, username=_user))

    @app.route("/api/progress/bulk", methods=["POST"])
    def api_progress_bulk():
        data  = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return jsonify({"error": "paths must be list"}), 400
        _user, _ = _get_current_user_info()
        return jsonify(get_watch_progress_bulk(paths, username=_user))

    # ================================================================
    # SyncPlay — native in-app synchronised playback (own room service)
    # ================================================================
    # All clients are browsers on THIS instance (phone / tablet / PC). The
    # server is authoritative; guests may join via an invite without a login.

    def _syncplay_enabled() -> bool:
        return get_setting("syncplay_enabled", "0") == "1"

    def _syncplay_device() -> str:
        ua = (request.headers.get("User-Agent") or "").lower()
        if any(x in ua for x in ("iphone", "android", "mobile")):
            return "Phone"
        if any(x in ua for x in ("ipad", "tablet")):
            return "Tablet"
        return "PC"

    def _sp_persist():
        try:
            import json as _json
            from . import syncplay_rooms as _sp
            set_setting("syncplay_rooms", _json.dumps(_sp.all_room_names()))
        except Exception:
            pass

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
    _SYNCPLAY_STREAM_OK = {
        "api_stream_check", "api_stream_start", "api_stream_playlist",
        "api_stream_segment", "api_stream_status", "api_stream_stop",
        "api_stream_active",
    }

    @app.before_request
    def _syncplay_guest_stream_guard():
        if not auth_enabled:
            return None
        if request.endpoint not in _SYNCPLAY_STREAM_OK:
            return None
        from flask import session as _sess
        if _sess.get("user_id") is not None:
            return None  # logged-in user
        from . import syncplay_rooms as sp
        if _syncplay_enabled() and sp.valid_token(_sess.get("sp_guest", "")):
            return None  # valid SyncPlay guest
        return jsonify({"error": "authentication required"}), 401

    @app.route("/api/syncplay/config", methods=["GET"])
    def api_syncplay_config():
        """Whether SyncPlay is enabled + the logged-in name to prefill the lobby."""
        user, _ = _get_current_user_info()
        return jsonify({
            "enabled": _syncplay_enabled(),
            "username": user or "",
            "can_manage": bool(user) or not auth_enabled,
        })

    @app.route("/api/syncplay/join", methods=["POST"])
    def api_syncplay_join():
        from . import syncplay_rooms as sp
        if not _syncplay_enabled():
            return jsonify({"error": "SyncPlay ist deaktiviert"}), 403
        data = request.get_json(silent=True) or {}
        room = (data.get("room") or "").strip()
        if not room:
            return jsonify({"error": "room fehlt"}), 400
        # Logged-in users keep their name; guests pass one or get a Guest tag.
        user, _ = _get_current_user_info()
        is_guest = not user
        name = (data.get("username") or user or "").strip()
        try:
            token, _r, snap = sp.join(room, name, is_guest, _syncplay_device(),
                                      ip=request.remote_addr or "",
                                      password=(data.get("password") or None))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except sp.RoomError as exc:
            return jsonify({"error": str(exc)}), 403
        if is_guest:
            from flask import session as _sess
            _sess["sp_guest"] = token
        _sp_persist()
        return jsonify({"token": token, "snapshot": snap})

    @app.route("/api/syncplay/stream")
    def api_syncplay_stream():
        """Server-Sent Events stream of room events for one member."""
        from flask import Response, stream_with_context
        from . import syncplay_rooms as sp
        import json as _json, queue as _queue
        token = (request.args.get("token") or "").strip()
        q = sp.subscribe(token)
        if q is None:
            return jsonify({"error": "invalid token"}), 404

        @stream_with_context
        def gen():
            yield "retry: 2000\n\n"
            while sp.valid_token(token):
                try:
                    ev = q.get(timeout=15)
                    sp.ack_drained(token, 1)
                    yield "data: " + _json.dumps(ev) + "\n\n"
                except _queue.Empty:
                    sp.heartbeat(token)
                    yield ": ping\n\n"
        resp = Response(gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.route("/api/syncplay/control", methods=["POST"])
    def api_syncplay_control():
        from . import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip()
        if action not in ("play", "pause", "seek"):
            return jsonify({"error": "invalid action"}), 400
        pos = data.get("position")
        ok = sp.control((data.get("token") or "").strip(), action,
                        float(pos) if pos is not None else None)
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))

    @app.route("/api/syncplay/report", methods=["POST"])
    def api_syncplay_report():
        from . import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.report((data.get("token") or "").strip(),
                       float(data.get("position", 0) or 0),
                       bool(data.get("paused", True)),
                       bool(data.get("buffering", False)),
                       file=data.get("file"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))

    @app.route("/api/syncplay/ready", methods=["POST"])
    def api_syncplay_ready():
        from . import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.set_ready((data.get("token") or "").strip(), bool(data.get("ready", True)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))

    @app.route("/api/syncplay/chat", methods=["POST"])
    def api_syncplay_chat():
        from . import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.chat((data.get("token") or "").strip(), str(data.get("text", "")))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))

    @app.route("/api/syncplay/episode", methods=["POST"])
    def api_syncplay_episode():
        """Host announces the currently selected media / episode."""
        from . import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
        cd = data.get("countdown")
        if cd:
            ok = sp.start_countdown(token, data.get("media"), int(cd))
        else:
            ok = sp.set_media(token, data.get("media"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host or no session"}), 403))

    @app.route("/api/syncplay/snapshot")
    def api_syncplay_snapshot():
        """Resume an existing membership after a page reload."""
        from . import syncplay_rooms as sp
        token = (request.args.get("token") or "").strip()
        snap = sp.get_snapshot(token)
        if snap is None:
            return jsonify({"error": "invalid"}), 404
        return jsonify({"token": token, "snapshot": snap})

    @app.route("/api/syncplay/leave", methods=["POST"])
    def api_syncplay_leave():
        from . import syncplay_rooms as sp
        from flask import session as _sess
        data = request.get_json(silent=True) or {}
        sp.leave((data.get("token") or "").strip())
        _sess.pop("sp_guest", None)
        return jsonify({"ok": True})

    def _sp_tok(data):
        return (data.get("token") or "").strip()

    @app.route("/api/syncplay/rooms", methods=["GET"])
    def api_syncplay_rooms():
        from . import syncplay_rooms as sp
        if not _syncplay_enabled():
            return jsonify({"rooms": []})
        return jsonify({"rooms": sp.list_rooms()})

    @app.route("/api/syncplay/close-room", methods=["POST"])
    def api_syncplay_close_room():
        # Owner-only: this endpoint stays behind login_required (not exempt),
        # so guests cannot close rooms — only the instance owner can.
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.close_by_name((d.get("name") or "").strip())
        _sp_persist()
        return jsonify({"ok": ok})

    @app.route("/api/syncplay/kick", methods=["POST"])
    def api_syncplay_kick():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.kick(_sp_tok(d), (d.get("name") or "").strip())
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/ban", methods=["POST"])
    def api_syncplay_ban():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.ban(_sp_tok(d), (d.get("name") or "").strip(), bool(d.get("by_ip", True)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/transfer-host", methods=["POST"])
    def api_syncplay_transfer_host():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.transfer_host(_sp_tok(d), (d.get("name") or "").strip())
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/close", methods=["POST"])
    def api_syncplay_close():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.close_room(_sp_tok(d))
        _sp_persist()
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/host-lock", methods=["POST"])
    def api_syncplay_host_lock():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_host_lock(_sp_tok(d), bool(d.get("locked", False)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/max", methods=["POST"])
    def api_syncplay_max():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_max(_sp_tok(d), d.get("max"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/password", methods=["POST"])
    def api_syncplay_password():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_password(_sp_tok(d), d.get("password"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    @app.route("/api/syncplay/away", methods=["POST"])
    def api_syncplay_away():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.set_away(_sp_tok(d), bool(d.get("away", False)))
        return jsonify({"ok": True})

    @app.route("/api/syncplay/typing", methods=["POST"])
    def api_syncplay_typing():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.typing(_sp_tok(d), bool(d.get("typing", False)))
        return jsonify({"ok": True})

    @app.route("/api/syncplay/reaction", methods=["POST"])
    def api_syncplay_reaction():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.reaction(_sp_tok(d), str(d.get("emoji", "")))
        return jsonify({"ok": True})

    @app.route("/api/syncplay/track", methods=["POST"])
    def api_syncplay_track():
        from . import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_track(_sp_tok(d), (d.get("kind") or "").strip(), d.get("value"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))

    if auth_enabled:
        from .auth import admin_required

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
        for endpoint, view_func in list(app.view_functions.items()):
            if endpoint not in _exempt:
                if endpoint in _admin_only:
                    app.view_functions[endpoint] = admin_required(view_func)
                else:
                    app.view_functions[endpoint] = login_required(view_func)

        # Exempt JSON API routes from CSRF (they use Content-Type: application/json
        # which provides implicit cross-origin protection via CORS preflight)
        for endpoint in list(app.view_functions):
            if endpoint.startswith("api_") or endpoint.startswith("auth.admin_"):
                csrf.exempt(app.view_functions[endpoint])

    # -----------------------------------------------------------------------
    # Background prefetch worker — warms browse lists, poster images and
    # TMDB data so the home page loads instantly instead of fetching lazily.
    # First run ~20 s after startup, then every 15 minutes.
    # -----------------------------------------------------------------------
    _PREFETCH_INTERVAL = 15 * 60   # seconds between cycles
    _PREFETCH_STARTUP  = 3         # initial delay to let server fully start
    _PREFETCH_RATE     = 0.4       # seconds between per-entry TMDB calls

    def _prefetch_cycle():
        """One full pass: warm browse lists → pre-cache posters → fetch TMDB data."""
        api_key = get_setting("cineinfo_tmdb_api_key", "")
        country = get_setting("cineinfo_country", "DE")
        tmdb_on = bool(api_key)

        # Collect all cards from every browse category (uses in-process cache)
        browse_sources = [
            ("new_animes",     fetch_new_animes),
            ("popular_animes", fetch_popular_animes),
            ("new_series",     fetch_new_series),
            ("popular_series", fetch_popular_series),
            ("new_movies",     _fetch_new_movies),
        ]
        all_entries = []
        for bkey, fn in browse_sources:
            try:
                results = _cached_browse(bkey, fn)
                if results:
                    all_entries.extend(results)
            except Exception as exc:
                logger.debug("[Prefetch] Browse %r failed: %s", bkey, exc)

        # Deduplicate by URL
        seen, unique = set(), []
        for e in all_entries:
            url = e.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(e)

        logger.info("[Prefetch] Warming cache for %d cards (TMDB: %s)", len(unique), tmdb_on)

        for entry in unique:
            url        = entry.get("url", "")
            title      = entry.get("title", "")
            poster_url = entry.get("poster_url", "")

            # Fire-and-forget poster pre-cache
            if poster_url:
                _img_pool.submit(_precache_image_bg, poster_url)

            if not (tmdb_on and title):
                continue

            # Skip if TMDB data already cached (title key, default de) and up to date
            cached = get_tmdb_cache(title + "|||" + country + "|||de")
            if cached is not None:
                if not cached.get("found", True) or ("trailer_key" in cached and "recommendations" in cached):
                    continue

            # Try to get IMDB ID from the series page for accurate matching
            imdb_id = None
            if not _is_filmpalast_url(url):
                try:
                    prov   = resolve_provider(url)
                    series = prov.series_cls(url=url)
                    imdb_id = getattr(series, "imdb", None) or None
                    # Also check the imdb_id-keyed cache entry
                    if imdb_id and get_tmdb_cache(imdb_id + "|||" + country + "|||de") is not None:
                        time.sleep(_PREFETCH_RATE)
                        continue
                except Exception:
                    pass  # fall through to title-only lookup

            _tmdb_lookup_cached(title, imdb_id, api_key, country)
            time.sleep(_PREFETCH_RATE)

        logger.info("[Prefetch] Cycle complete.")

    def _prefetch_worker():
        time.sleep(_PREFETCH_STARTUP)
        while True:
            try:
                _prefetch_cycle()
            except Exception as exc:
                logger.warning("[Prefetch] Worker cycle error: %s", exc)
            time.sleep(_PREFETCH_INTERVAL)

    _pt = threading.Thread(target=_prefetch_worker, daemon=True, name="browse-prefetch")
    _pt.start()
    logger.info("[Prefetch] Background worker started (interval=%d min)", _PREFETCH_INTERVAL // 60)

    # Update checker — runs once at startup (after 10 s) then every 24 h
    def _update_check_worker():
        import time as _time
        _time.sleep(10)
        while True:
            try:
                _do_update_check()
                logger.info(
                    "[UpdateCheck] Latest: %s | update_available: %s",
                    _update_cache["latest_version"],
                    _update_cache["update_available"],
                )
            except Exception:
                logger.exception("[UpdateCheck] Unexpected error")
            _time.sleep(_UPDATE_CHECK_INTERVAL)

    # Resolve any update state left behind by the self-update helper.
    try:
        selfupdate.finalize_after_restart()
    except Exception:
        logger.exception("[SelfUpdate] finalize_after_restart failed")

    _uct = threading.Thread(target=_update_check_worker, daemon=True, name="update-checker")
    _uct.start()

    # Auto-update scheduler — installs updates at a user-defined weekday/time.
    def _auto_update_worker():
        import time as _t
        from datetime import datetime as _dt
        _t.sleep(20)
        while True:
            try:
                if get_setting("auto_update_enabled", "0") == "1":
                    inst = selfupdate.detect_install()
                    if inst["can_self_update"]:
                        now = _dt.now()
                        days_raw = get_setting("auto_update_days", "0,1,2,3,4,5,6") or ""
                        day_ok = str(now.weekday()) in [d.strip() for d in days_raw.split(",") if d.strip() != ""]
                        target_time = (get_setting("auto_update_time", "03:00") or "03:00").strip()
                        now_hhmm = now.strftime("%H:%M")
                        today = now.strftime("%Y-%m-%d")
                        already = get_setting("auto_update_last_run", "") == today
                        if day_ok and now_hhmm == target_time and not already:
                            set_setting("auto_update_last_run", today)
                            if (_t.time() - _update_cache["checked_at"]) > 300:
                                _do_update_check()
                            if _update_cache["update_available"]:
                                logger.info("[AutoUpdate] scheduled update starting")
                                try:
                                    selfupdate.start_update()
                                    from .db import get_db as _gd
                                    _c = _gd()
                                    try:
                                        _c.execute("UPDATE download_queue SET status='queued' WHERE status='running'")
                                        _c.commit()
                                    finally:
                                        _c.close()
                                    _t.sleep(1.5)
                                    os._exit(0)
                                except selfupdate.UpdateError as _ue:
                                    logger.warning("[AutoUpdate] cannot update: %s", _ue)
                            else:
                                logger.info("[AutoUpdate] no update available, skipping")
            except Exception:
                logger.exception("[AutoUpdate] worker error")
            _t.sleep(30)

    _aut = threading.Thread(target=_auto_update_worker, daemon=True, name="auto-update")
    _aut.start()

    @app.context_processor
    def override_url_for():
        def dated_url_for(endpoint, **values):
            if endpoint == 'static':
                filename = values.get('filename', None)
                if filename:
                    file_path = os.path.join(app.static_folder, filename)
                    if os.path.exists(file_path):
                        values['v'] = int(os.stat(file_path).st_mtime)
            return url_for(endpoint, **values)
        return dict(url_for=dated_url_for)



    # ---------------------------------------------------------------------------
    # Calendar Watcher Service
    # ---------------------------------------------------------------------------
    def _resolve_cr_titles(api_key, country, ui_lang):
        """Resolve Crunchyroll simulcast/watchlist/list titles to TMDB tv ids.

        Returns ``(ids, meta)`` where ``meta[tid] = {title, in_wl, in_list,
        lists}``. Pure title->id resolution (TMDB-cached); no episode/DB work.
        Runs in the background watcher so the request path stays instant.
        """
        from . import crunchyroll_service as _crs

        def _norm(x):
            return "".join(c for c in (x or "").lower() if c.isalnum())

        want_sim = get_setting("crunchyroll_calendar_simulcast", "0") == "1"
        want_wl = get_setting("crunchyroll_calendar_watchlist", "0") == "1"
        want_lists = get_setting("crunchyroll_calendar_lists", "0") == "1"
        if not (want_sim or want_wl or want_lists):
            return [], {}, True, {}

        wl_titles = _crs.get_watchlist_titles() if want_wl else []
        wl_norm = {_norm(t) for t in wl_titles}
        list_entries = _crs.get_custom_list_entries() if want_lists else []
        list_titles = [e["title"] for e in list_entries]
        list_names_by_norm = {}
        for _e in list_entries:
            list_names_by_norm.setdefault(_norm(_e["title"]), set()).add(
                _e.get("list_name") or "Crunchylist")
        list_norm = set(list_names_by_norm.keys())

        sim_titles = list(_crs.get_simulcast_titles()) if want_sim else []
        # A category whose toggle is on but which returned nothing is a
        # transient failure (e.g. a re-login hiccup). Signal "incomplete" so
        # the caller keeps the previous good set instead of a partial one.
        complete = not ((want_sim and not sim_titles) or
                        (want_wl and not wl_titles) or
                        (want_lists and not list_entries))
        titles = sim_titles + list(wl_titles) + list(list_titles)
        if not titles:
            return [], {}, False, {}

        # Prefer a TMDB id already in the calendar cache for this title (synced by
        # ANY source, e.g. Seerr's authoritative id) over the CR title search,
        # which sometimes picks a wrong/duplicate TMDB entry. Per title we take the
        # cached id with the most recent episode (the active entry).
        db_id_by_title, db_best = {}, {}
        try:
            from .db import get_calendar_media_titles
            for _tid, _ttl, _ttl_en, _max_air in get_calendar_media_titles():
                for _nm in (_norm(_ttl), _norm(_ttl_en)):
                    if not _nm:
                        continue
                    if _nm not in db_best or (_max_air or "") > db_best[_nm]:
                        db_id_by_title[_nm] = int(_tid)
                        db_best[_nm] = _max_air or ""
        except Exception as _exc:
            logger.debug("[Calendar] CR id reconcile map failed: %s", _exc)
            db_id_by_title = {}

        ids, meta = [], {}
        for t in titles:
            nt = _norm(t)
            disp = t
            tid = db_id_by_title.get(nt)
            if tid is None:
                try:
                    info = _tmdb_lookup_cached(t, None, api_key, country, ui_lang)
                except Exception:
                    continue
                if not (info and info.get("found") and info.get("media_type") == "tv"):
                    continue
                _t = info.get("tmdb_id")
                if not _t:
                    continue
                tid = int(_t)
                disp = info.get("title") or t
            in_wl = nt in wl_norm
            in_list = nt in list_norm
            names = list_names_by_norm.get(nt, set())
            if tid not in meta:
                ids.append(tid)
                meta[tid] = {"title": disp,
                             "in_wl": in_wl, "in_list": in_list,
                             "lists": set(names)}
            else:
                if in_wl:
                    meta[tid]["in_wl"] = True
                if in_list:
                    meta[tid]["in_list"] = True
                meta[tid]["lists"].update(names)
        # Title-keyed membership (for matching events whose source gave a
        # different TMDB id than the CR title resolves to, e.g. Seerr).
        tmeta = {}
        for _t in wl_titles:
            tmeta.setdefault(_norm(_t), {"in_wl": False, "in_list": False,
                                         "lists": set()})["in_wl"] = True
        for _k, _names in list_names_by_norm.items():
            _d = tmeta.setdefault(_k, {"in_wl": False, "in_list": False, "lists": set()})
            _d["in_list"] = True
            _d["lists"].update(_names)
        for _t in sim_titles:
            tmeta.setdefault(_norm(_t), {"in_wl": False, "in_list": False, "lists": set()})
        return ids, meta, complete, tmeta

    def _cr_calendar_targets(api_key, country, ui_lang, now):
        """Throttled CR target resolver. Caches ids+meta in module globals so the
        request path can read them without any TMDB/Crunchyroll calls. Returns the
        list of CR tmdb ids to feed into the watcher's sync pool."""
        global _cr_calendar_ids, _cr_calendar_meta, _cr_targets_built_at, _cr_calendar_titles
        from . import crunchyroll_service as _crs
        want_any = any(get_setting(k, "0") == "1" for k in (
            "crunchyroll_calendar_simulcast",
            "crunchyroll_calendar_watchlist",
            "crunchyroll_calendar_lists"))
        if not (want_any and (_crs.is_enabled() or _crs.has_account())):
            _cr_calendar_ids, _cr_calendar_meta, _cr_targets_built_at = [], {}, now
            return []
        if _cr_calendar_ids and (now - _cr_targets_built_at) < _CR_TARGETS_TTL:
            return list(_cr_calendar_ids)
        try:
            ids, meta, complete, tmeta = _resolve_cr_titles(api_key, country, ui_lang)
        except Exception as exc:
            logger.debug("[Calendar Watcher] CR resolve failed: %s", exc)
            return list(_cr_calendar_ids)
        # Replace the cached set only on a non-empty result, and never let a
        # partial (incomplete) resolve overwrite a previously good full set.
        if ids and (complete or not _cr_calendar_ids):
            _cr_calendar_ids, _cr_calendar_meta = ids, meta
        if tmeta and (complete or not _cr_calendar_titles):
            _cr_calendar_titles = tmeta
        # On an incomplete resolve, retry soon instead of waiting the full TTL.
        _cr_targets_built_at = now if complete else (now - _CR_TARGETS_TTL + 90)
        return list(_cr_calendar_ids)

    def _sync_calendar_item(tmdb_id, media_type, api_key):
        # The cache is language-agnostic: we fetch both German and English from
        # TMDB and store both, so the calendar can be displayed in either UI
        # language without re-fetching. (TMDB lookups are cached per language.)
        try:
            if media_type == "tv":
                cal = _tmdb_calendar_episodes(tmdb_id, api_key, "de")
                if not cal or not cal.get("title"):
                    # Save a dummy media to db so we don't query it infinitely
                    save_calendar_media(tmdb_id, f"TMDB TV #{tmdb_id}", f"TMDB TV #{tmdb_id}", "")
                    return
                cal_en = {}
                try:
                    cal_en = _tmdb_calendar_episodes(tmdb_id, api_key, "en") or {}
                except Exception:
                    pass
                title_en = cal_en.get("title") or cal["title"]
                en_names = {
                    (e.get("season"), e.get("episode")): (e.get("name") or "")
                    for e in cal_en.get("episodes", [])
                }

                media_id = save_calendar_media(tmdb_id, cal["title"], title_en, cal.get("poster") or "")
                keep_episodes = []
                for ep in cal.get("episodes", []):
                    season = ep.get("season")
                    episode = ep.get("episode")
                    name = ep.get("name") or ""
                    name_en = en_names.get((season, episode)) or name
                    air_date = ep.get("air_date")
                    still_path = ep.get("still") or ""
                    if season is not None and episode is not None and air_date:
                        save_calendar_episode(media_id, season, episode, name, name_en, air_date, still_path)
                        keep_episodes.append((season, episode))

                # Delete any other episodes no longer in the TMDB schedule
                delete_calendar_episodes_except(media_id, keep_episodes)
                logger.debug("[Calendar Watcher] Synced TV show tmdb_id=%d: %s (%d episodes)", tmdb_id, cal["title"], len(keep_episodes))

            elif media_type == "movie":
                mov = _tmdb_movie_release(tmdb_id, api_key, "de")
                if not mov or not mov.get("title") or not mov.get("release_date"):
                    save_calendar_media(tmdb_id, f"TMDB Movie #{tmdb_id}", f"TMDB Movie #{tmdb_id}", "")
                    return
                title_en = mov["title"]
                try:
                    mov_en = _tmdb_movie_release(tmdb_id, api_key, "en") or {}
                    title_en = mov_en.get("title") or mov["title"]
                except Exception:
                    pass

                media_id = save_calendar_media(tmdb_id, mov["title"], title_en, mov.get("poster") or "")
                save_calendar_episode(media_id, None, None, "", "", mov["release_date"], "")
                delete_calendar_episodes_except(media_id, [(None, None)])
                logger.debug("[Calendar Watcher] Synced Movie tmdb_id=%d: %s (release: %s)", tmdb_id, mov["title"], mov["release_date"])
        except Exception as exc:
            logger.error("[Calendar Watcher] Failed to sync tmdb_id=%d type=%s: %s", tmdb_id, media_type, exc, exc_info=True)

    def _calendar_watcher_loop():
        import time as _t
        global _calendar_watcher_active
        last_list_a_empty_time = None
        _busy = False  # fast cadence while actively populating List A
        while True:
            _t.sleep(0.5 if _busy else 10)
            try:
                # 1. Check if calendar and TMDB API key are configured/enabled.
                #    Reflect the real enabled state in the status flag.
                if get_setting("cineinfo_calendar", "0") != "1":
                    _calendar_watcher_active = False
                    continue
                api_key = (get_setting("cineinfo_tmdb_api_key") or "").strip()
                if not api_key:
                    _calendar_watcher_active = False
                    continue
                _calendar_watcher_active = True
                country = get_setting("cineinfo_country", "DE")
                # Language used only to resolve title -> TMDB id (the id is the same
                # regardless); the cached display data itself is stored bilingually.
                ui_lang = "de"

                # 2. Gather active targets
                # Priority 2.1: AutoSync jobs
                autosync_jobs = get_autosync_jobs()
                priority_titles = []
                for job in autosync_jobs:
                    if job.get("enabled") == 1:
                        t = (job.get("title") or "").strip()
                        if t:
                            priority_titles.append(t)

                priority_tv_ids = set()
                for title in priority_titles:
                    try:
                        info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                        if info and info.get("found") and info.get("media_type") == "tv":
                            tid = info.get("tmdb_id")
                            if tid:
                                priority_tv_ids.add(int(tid))
                    except Exception as exc:
                        logger.debug("[Calendar Watcher] TMDB lookup failed for title %s: %s", title, exc)

                # Priority 2.2: Seerr requests
                seerr_items = []
                if get_setting("cineinfo_calendar_seerr", "0") == "1":
                    seerr_items = _seerr_requested_media()

                priority_media_targets = []
                for tid in priority_tv_ids:
                    priority_media_targets.append((tid, "tv"))
                for item in seerr_items:
                    tid = item.get("tmdb_id")
                    mt = item.get("media_type")
                    if tid and mt in ("tv", "movie"):
                        priority_media_targets.append((int(tid), mt))

                # Non-priority 2.3: Media Library series
                mediathek_media_targets = []
                if get_setting("cineinfo_calendar_mediathek", "0") == "1":
                    mediathek_series = get_mediascan_series()
                    for item in mediathek_series:
                        tid = item.get("tmdb_id")
                        if tid:
                            try:
                                mediathek_media_targets.append((int(tid), "tv"))
                            except ValueError:
                                pass
                        elif item.get("title"):
                            title = item.get("title").strip()
                            try:
                                info = _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
                                if info and info.get("found") and info.get("media_type") == "tv":
                                    tid = info.get("tmdb_id")
                                    if tid:
                                        mediathek_media_targets.append((int(tid), "tv"))
                            except Exception as exc:
                                logger.debug("[Calendar Watcher] TMDB lookup failed for library series %s: %s", title, exc)

                # Deduplicate Mediathek targets against priority targets
                priority_set = {tid for tid, mt in priority_media_targets}
                mediathek_media_targets = [item for item in mediathek_media_targets if item[0] not in priority_set]

                # Non-priority 2.4: Crunchyroll simulcast / watchlist / lists.
                #    Resolve CR titles -> TMDB ids (throttled; cached in globals
                #    for the request path) and sync their episodes like Mediathek
                #    so the calendar fills in progressively in the background.
                try:
                    cr_ids = _cr_calendar_targets(api_key, country, ui_lang, _t.time())
                except Exception as exc:
                    logger.debug("[Calendar Watcher] CR targets failed: %s", exc)
                    cr_ids = []
                _seen_np = {tid for tid, _mt in mediathek_media_targets}
                for _cid in cr_ids:
                    if _cid not in priority_set and _cid not in _seen_np:
                        mediathek_media_targets.append((_cid, "tv"))
                        _seen_np.add(_cid)

                # 3. Retrieve currently cached media status from DB
                all_target_ids = list({tid for tid, mt in (priority_media_targets + mediathek_media_targets)})
                cached_times = get_cached_calendar_media(all_target_ids)

                # Build List A (missing from DB)
                list_a_priority = []
                list_a_mediathek = []
                for tid, mt in priority_media_targets:
                    if tid not in cached_times:
                        list_a_priority.append((tid, mt))
                for tid, mt in mediathek_media_targets:
                    if tid not in cached_times:
                        list_a_mediathek.append((tid, mt))

                # Build List B (existing in DB, needs refresh)
                refresh_hours = int(get_setting("cineinfo_calendar_refresh_interval", "24"))
                refresh_seconds = refresh_hours * 3600

                list_b = []
                now = _t.time()
                for tid, mt in (priority_media_targets + mediathek_media_targets):
                    if tid in cached_times:
                        last_updated = cached_times[tid]
                        if now - last_updated >= refresh_seconds:
                            list_b.append((tid, mt))

                # 4. Processing logic
                global _calendar_watcher_scanning
                global _calendar_watcher_last_sync
                if list_a_priority or list_a_mediathek:
                    _busy = True  # keep the fast cadence
                    # List A has items: drain a batch this cycle (priority first).
                    # The shared TMDB rate limiter (40 req/s) throttles the calls,
                    # so the calendar fills in fast instead of one item per cycle.
                    last_list_a_empty_time = None
                    batch = (list_a_priority + list_a_mediathek)[:_CAL_A_BATCH]
                    _calendar_watcher_scanning = True
                    try:
                        for target_id, target_type in batch:
                            _sync_calendar_item(target_id, target_type, api_key)
                    finally:
                        _calendar_watcher_scanning = False
                        _calendar_watcher_last_sync = _t.time()
                else:
                    _busy = False  # back to the calm 10s cadence
                    # List A is empty
                    if last_list_a_empty_time is None:
                        last_list_a_empty_time = _t.time()
                    
                    # If List A has been empty for at least 15 minutes (900 seconds), process List B
                    if _t.time() - last_list_a_empty_time >= 900:
                        if list_b:
                            target_id, target_type = list_b[0]
                            _calendar_watcher_scanning = True
                            try:
                                _sync_calendar_item(target_id, target_type, api_key)
                            finally:
                                _calendar_watcher_scanning = False
                                _calendar_watcher_last_sync = _t.time()
                            _t.sleep(1.5)
            except Exception as e:
                logger.error("[Calendar Watcher] Error in watcher loop: %s", e, exc_info=True)

    # Start the watcher exactly once, even if create_app is invoked more than
    # once in the same process (mirrors the queue/autosync worker guards).
    global _calendar_watcher_started
    if not _calendar_watcher_started:
        _calendar_watcher_started = True
        threading.Thread(target=_calendar_watcher_loop, daemon=True, name="calendar-watcher").start()

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
            _graceful_shutdo