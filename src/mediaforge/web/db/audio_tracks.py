"""Which audio and video tracks a file on disk actually carries.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up.

Auto-sync answers "is this episode already there?" by looking at folders: an
episode counts as present in German when a file for it sits in ``german-dub/``.
That works as long as one language means one file, and stops working the moment
a multi-language job merges several audio tracks into ONE file -- the English
track then lives inside the German file, the ``english-dub/`` folder stays
empty, and every sync cycle would queue the whole series again. The downloads
themselves would be skipped one by one (``check_downloaded()`` sees the track),
but the queue fills up and the notifications announce new episodes that are not
new.

Reading the tracks needs ``ffprobe``, which is far too expensive to run over a
caught-up 200-episode series every cycle. So the probe result is cached per
file and invalidated by (mtime, size): a file nobody touched is a single index
lookup, a file that was written to is probed again. That also covers the case
this exists for -- a track merged into an existing file changes its mtime and
size, so the next cycle sees the new track without anyone having to invalidate
anything by hand.
"""

import json
import os
import time

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)

# Rows are only ever read back for files that still exist, so nothing goes
# stale in a harmful way -- but a library that is reorganised often would grow
# the table forever. Pruned against the paths a scan actually saw.
_PRUNE_BATCH = 500


def init_audio_track_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audio_track_cache (
                path        TEXT PRIMARY KEY,
                mtime       REAL NOT NULL,
                size        INTEGER NOT NULL,
                audio_langs TEXT NOT NULL DEFAULT '[]',
                video_langs TEXT NOT NULL DEFAULT '[]',
                probed_at   REAL NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime, st.st_size


def get_cached_tracks(path):
    """Cached ``{"audio_langs": set, "video_langs": set}``, or None.

    None means "ask ffprobe": either nothing was ever stored for this path, or
    the file changed since it was. A file that has disappeared also returns
    None rather than the last known answer -- reporting tracks for a file that
    is gone would make auto-sync skip an episode it no longer has.
    """
    current = _stat(path)
    if current is None:
        return None
    mtime, size = current

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT mtime, size, audio_langs, video_langs FROM audio_track_cache WHERE path = ?",
            (str(path),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # Both halves on purpose: a same-size rewrite keeps the size, and a copy
    # that preserves timestamps keeps the mtime. Together they are wrong only
    # for an edit that changes neither, which no muxer produces.
    if row["mtime"] != mtime or row["size"] != size:
        return None
    try:
        return {
            "audio_langs": set(json.loads(row["audio_langs"])),
            "video_langs": set(json.loads(row["video_langs"])),
        }
    except (TypeError, ValueError):
        return None


def set_cached_tracks(path, audio_langs, video_langs):
    """Store a probe result for `path` against its current mtime and size."""
    current = _stat(path)
    if current is None:
        return
    mtime, size = current
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO audio_track_cache (path, mtime, size, audio_langs, video_langs, probed_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size, "
            "audio_langs = excluded.audio_langs, video_langs = excluded.video_langs, "
            "probed_at = excluded.probed_at",
            (
                str(path),
                mtime,
                size,
                json.dumps(sorted(audio_langs or [])),
                json.dumps(sorted(video_langs or [])),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def prune_audio_track_cache(max_age_days=90):
    """Drop entries for files that are gone, oldest first.

    Checked against the filesystem rather than against a caller-supplied path
    list: the cache is written from several places (auto-sync, the library
    scan) and none of them knows the whole set. Only the oldest rows are
    stat()ed per call so this cannot turn into a full library walk.
    """
    cutoff = time.time() - max_age_days * 86400
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT path FROM audio_track_cache WHERE probed_at < ? "
            "ORDER BY probed_at LIMIT ?",
            (cutoff, _PRUNE_BATCH),
        ).fetchall()
        gone = [r["path"] for r in rows if not os.path.exists(r["path"])]
        for i in range(0, len(gone), 100):
            chunk = gone[i:i + 100]
            conn.execute(
                "DELETE FROM audio_track_cache WHERE path IN (%s)"
                % ",".join("?" * len(chunk)),
                tuple(chunk),
            )
        conn.commit()
        return len(gone)
    finally:
        conn.close()
