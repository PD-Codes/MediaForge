"""Custom download paths.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...config import MEDIAFORGE_CONFIG_DIR
from ..media_kinds import DEFAULT_KINDS_CSV as _DEFAULT_MEDIA_KINDS
from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


_CREATE_CUSTOM_PATHS_TABLE = """\
CREATE TABLE IF NOT EXISTS custom_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    default_sites TEXT NOT NULL DEFAULT '',
    media_kinds TEXT NOT NULL DEFAULT 'video'
);
"""


def init_custom_paths_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_CUSTOM_PATHS_TABLE)
        # Migration for existing installations. An empty value preserves the
        # old behaviour: the global download path remains selected.
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(custom_paths)").fetchall()
        }
        if "default_sites" not in columns:
            conn.execute(
                "ALTER TABLE custom_paths ADD COLUMN default_sites TEXT NOT NULL DEFAULT ''"
            )
        # Which libraries a path feeds (see web/media_kinds.py). SQLite fills
        # every existing row with the column default, so an instance updating
        # into this release finds all of its paths assigned to "video" -- a
        # path that also holds eBooks has to be ticked once in Settings. The
        # book library's empty state links there for exactly this reason.
        if "media_kinds" not in columns:
            conn.execute(
                "ALTER TABLE custom_paths ADD COLUMN media_kinds TEXT NOT NULL "
                f"DEFAULT '{_DEFAULT_MEDIA_KINDS}'"
            )
        conn.commit()
    finally:
        conn.close()


def get_custom_paths():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, path, default_sites, media_kinds FROM custom_paths ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def default_custom_path_for_url(url):
    """The id of the custom path configured as default for the site behind
    *url*, or None.

    The ``default_sites`` CSV is matched here rather than in the browser.
    static/app.js and static/settings.js each had their own copy of the split-
    trim-compare, which meant every non-browser client (a module connector,
    Auto-Sync, Seerr) had to grow a third one to reach the same answer. First
    match in id order wins, which is the order get_custom_paths() returns and
    the order the frontend already resolved in.
    """
    from ...mirrors import site_for_url

    site = site_for_url(url or "")
    if not site:
        return None
    for row in get_custom_paths():
        sites = [s.strip() for s in (row.get("default_sites") or "").split(",")]
        if site in sites:
            return row["id"]
    return None


def add_custom_path(name, path, default_sites="", media_kinds=None):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO custom_paths (name, path, default_sites, media_kinds) "
            "VALUES (?, ?, ?, ?)",
            (name, path, default_sites, media_kinds or _DEFAULT_MEDIA_KINDS),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_custom_path(path_id, name=None, path=None, default_sites=None,
                       media_kinds=None):
    """Update the supplied fields of a custom download path."""
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if path is not None:
        fields.append("path = ?")
        values.append(path)
    if default_sites is not None:
        fields.append("default_sites = ?")
        values.append(default_sites)
    if media_kinds is not None:
        fields.append("media_kinds = ?")
        values.append(media_kinds)
    if not fields:
        return

    values.append(path_id)
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE custom_paths SET {', '.join(fields)} WHERE id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def is_custom_path_in_use(path_id):
    """Return True if any autosync job or active queue item currently references this custom path."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE custom_path_id = ? OR movie_custom_path_id = ?",
            (path_id, path_id),
        ).fetchone()
        if row and row["cnt"] > 0:
            return True
        row_queue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE custom_path_id = ? AND status IN ('queued', 'running')",
            (path_id,),
        ).fetchone()
        return bool(row_queue and row_queue["cnt"] > 0)
    finally:
        conn.close()


def remove_custom_path(path_id):
    """Delete a custom path. Returns (True, None) on success or (False, reason) if blocked."""
    conn = get_db()
    try:
        row_sync = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE custom_path_id = ? OR movie_custom_path_id = ?",
            (path_id, path_id),
        ).fetchone()
        if row_sync and row_sync["cnt"] > 0:
            return False, "Dieser Pfad wird von mindestens einem Auto-Sync-Job verwendet und kann nicht gelöscht werden."
        row_queue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE custom_path_id = ? AND status IN ('queued', 'running')",
            (path_id,),
        ).fetchone()
        if row_queue and row_queue["cnt"] > 0:
            return False, "Dieser Pfad wird noch von aktiven oder wartenden Downloads in der Warteschlange verwendet."
        conn.execute("DELETE FROM custom_paths WHERE id = ?", (path_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def get_custom_path_by_id(path_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, path, default_sites, media_kinds FROM custom_paths WHERE id = ?",
            (path_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
