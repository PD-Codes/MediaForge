"""MediaScan — library scanning from Plex / Jellyfin and mediaplayer refresh.

Two independent jobs live here:
  * A cache of the media server's full library (title + TMDB/IMDB/TVDB ids),
    refreshed on a 24-h timer or shortly after a download completes, so
    "already in your library" checks elsewhere in the app are cheap local
    lookups instead of a live API call.
  * Triggering a Jellyfin/Plex library refresh after a download finishes, so
    the new file shows up without the user manually rescanning.

Used by: ``web/app.py`` starts the 24-h scheduler at boot; ``web/queue_worker.py``
calls the post-download refresh + delayed rescan after a completed download;
``web/routes/integrations.py`` exposes the manual-trigger and status-polling
endpoints backed by ``_run_mediascan`` / ``_mediascan_status``.

TODO(telemetry): wire up flag.library_scan / detail.library_scan (scan
duration, new-finds count, errors) at the scan-finished point in this
module -- see telemetry/registry.py. Registry-only for now.
"""

import threading

from ..logger import get_logger
from .db import (
    get_setting,
    get_tmdb_cache,
    replace_mediascan_cache,
)

logger = get_logger(__name__)


def _normalize_media_url(raw: str) -> str:
    """Ensure a media-server URL has an http(s):// scheme."""
    # Imported lazily: queue_worker imports this module at module level, so a
    # top-level import here would create a circular import.
    from .queue_worker import _normalize_media_url as _impl
    return _impl(raw)


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
    """Start a daemon thread that triggers a mediascan every 24 h.

    Used by: called once at startup from ``web/app.py``."""
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
    """Trigger a library refresh on Jellyfin or Plex after a successful download.

    Used by: ``web/queue_worker.py`` after a completed download, and
    ``web/routes/integrations.py`` for a manual refresh action."""
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
