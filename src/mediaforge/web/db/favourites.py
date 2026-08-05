"""Favourites.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import _sql_chunks, get_db

logger = get_logger(__name__)


_CREATE_FAVOURITES_TABLE = """\
CREATE TABLE IF NOT EXISTS favourites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_url TEXT NOT NULL,
    title TEXT NOT NULL,
    poster_url TEXT,
    added_by TEXT,
    media_type TEXT,
    provider TEXT,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(series_url, added_by)
);
"""


def init_favourites_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_FAVOURITES_TABLE)
        # Migrations: add the metadata columns for existing DBs. Same best-effort
        # ALTER TABLE pattern as init_queue_db()/init_autosync_db() -- each column
        # is added on its own and the "duplicate column" error is ignored when it
        # already exists. All three are nullable so legacy rows stay valid.
        for _col in (
            "ALTER TABLE favourites ADD COLUMN media_type TEXT",
            "ALTER TABLE favourites ADD COLUMN provider TEXT",
            "ALTER TABLE favourites ADD COLUMN language TEXT",
        ):
            try:
                conn.execute(_col)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def add_favourite(
    series_url: str,
    title: str,
    poster_url: str | None,
    added_by: str | None,
    media_type: str | None = None,
    provider: str | None = None,
    language: str | None = None,
):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO favourites "
            "(series_url, title, poster_url, added_by, media_type, provider, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (series_url, title, poster_url, added_by, media_type, provider, language),
        )
        conn.commit()
    finally:
        conn.close()


def remove_favourite(series_url: str, added_by: str | None):
    # "OR added_by IS NULL" also matches legacy/no-auth rows that have no
    # owner, since SQLite treats NULL as distinct for the UNIQUE(series_url,
    # added_by) constraint and a plain "=" comparison would never match NULL.
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM favourites WHERE series_url = ? AND (added_by = ? OR added_by IS NULL)",
            (series_url, added_by),
        )
        conn.commit()
    finally:
        conn.close()


def remove_favourites_bulk(series_urls: list[str], added_by: str | None):
    if not series_urls:
        return
    conn = get_db()
    try:
        for chunk, placeholders in _sql_chunks(series_urls):
            params = list(chunk)
            if added_by:
                query = f"DELETE FROM favourites WHERE series_url IN ({placeholders}) AND (added_by = ? OR added_by IS NULL)"
                params.append(added_by)
            else:
                query = f"DELETE FROM favourites WHERE series_url IN ({placeholders})"
            conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def get_favourites(added_by: str | None = None):
    conn = get_db()
    try:
        if added_by:
            rows = conn.execute(
                "SELECT * FROM favourites WHERE added_by = ? ORDER BY created_at DESC",
                (added_by,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM favourites ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_favourite(series_url: str, added_by: str | None) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM favourites WHERE series_url = ? AND (added_by = ? OR added_by IS NULL) LIMIT 1",
            (series_url, added_by),
        ).fetchone()
        return row is not None
    finally:
        conn.close()
