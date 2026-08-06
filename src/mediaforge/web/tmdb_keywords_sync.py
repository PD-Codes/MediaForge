"""Background worker that syncs the daily TMDB keyword export.

Downloads TMDB's public daily keyword-ID export (a large gzip'd JSON file) so
that CineInfo's advanced search can resolve keyword names locally instead of
hitting the TMDB API for every search. Only runs when advanced search is
enabled in settings, and only re-downloads once per day.
"""

import threading
from datetime import timedelta

from ..config import MEDIAFORGE_CONFIG_DIR
from ..logger import get_logger

logger = get_logger(__name__)


_tmdb_keywords_worker_started = False

# How long the worker sleeps between passes. Named so the heartbeat's
# "next run" and the actual sleep cannot drift apart.
_SYNC_INTERVAL = 3600


def _tmdb_keywords_sync_worker():
    """Background loop: check hourly whether today's TMDB keyword export
    needs downloading, and fetch it if so. Runs for the lifetime of the process."""
    import time
    import gzip
    import urllib.request
    from datetime import datetime

    from . import worker_registry as _wr

    def _next_run_iso():
        """When this loop will look again -- it always sleeps _SYNC_INTERVAL
        between passes, including after a skipped one."""
        return (datetime.now() + timedelta(seconds=_SYNC_INTERVAL)).isoformat(timespec="seconds")

    while True:
        try:
            from .db import get_setting
            # Beat before the config check, not after: a worker that is
            # switched off is still alive, and reporting only when enabled is
            # what made it look permanently unknown in the Operations view.
            _wr.working("tmdb_keywords")
            # Only run if advanced search is enabled in config
            if get_setting("cineinfo_advanced_search", "0") != "1":
                # idle(), not done(): nothing ran, so "last run" must not move.
                _wr.idle("tmdb_keywords", detail="advanced search disabled",
                         next_run=_next_run_iso())
                time.sleep(_SYNC_INTERVAL)
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

            _wr.done("tmdb_keywords",
                     detail="downloaded" if download_needed else "already current",
                     next_run=_next_run_iso())

        except Exception as e:
            logger.error(f"Error in TMDB keywords sync worker: {e}")
            _wr.fail("tmdb_keywords", str(e))

        time.sleep(_SYNC_INTERVAL)  # Check every hour


def _ensure_tmdb_keywords_sync_worker():
    """Start the background sync thread once (idempotent). Safe to call on
    every request/startup path that needs the keyword export available.

    Used by: app.py, called during create_app() startup.
    """
    global _tmdb_keywords_worker_started
    if _tmdb_keywords_worker_started:
        return
    _tmdb_keywords_worker_started = True
    thread = threading.Thread(target=_tmdb_keywords_sync_worker, daemon=True)
    thread.start()
