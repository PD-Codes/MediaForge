"""Download-history persistence helper."""

import time

from ..logger import get_logger
from .db import add_download_history

logger = get_logger(__name__)


def _record_download_history(item, ep_url, start_time, ep_path, size_bytes, status, error=None, language=None):
    """Persist a single episode download to the history table. Best-effort
    (exceptions are logged and swallowed so a history-write failure never
    breaks the download itself).

    `language` overrides the item's language for this one episode — items using
    a fallback group resolve a real language per episode, and the history is
    about what was actually downloaded. Without it the item's own value is
    stored, which for a group is its "group:<id>" reference: retrying such an
    entry has to go through the same per-episode resolution again, so the
    reference is what must survive, not a display name (routes/history.py adds
    that for the UI).

    Used by: queue_worker.py, called once per episode after a download
    attempt finishes (success, failure, or skip).
    """
    try:
        from datetime import datetime, timezone
        from .queue_worker import _parse_season_episode
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
            language=language or item.get("language"),
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
