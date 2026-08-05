"""Hidden Seerr requests.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


_CREATE_SEERR_HIDDEN_TABLE = """
CREATE TABLE IF NOT EXISTS seerr_hidden (
    user_id INTEGER NOT NULL,
    seerr_request_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    poster_url TEXT NOT NULL DEFAULT '',
    hidden_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, seerr_request_id)
);
"""


def init_seerr_hidden_db():
    conn = get_db()
    try:
        conn.execute(_CREATE_SEERR_HIDDEN_TABLE)
        conn.commit()
    finally:
        conn.close()


def hide_seerr_request(user_id: int, seerr_request_id: int, title: str = "", poster_url: str = "") -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO seerr_hidden (user_id, seerr_request_id, title, poster_url) VALUES (?, ?, ?, ?)",
            (user_id, seerr_request_id, title, poster_url),
        )
        conn.commit()
    finally:
        conn.close()


def unhide_seerr_request(user_id: int, seerr_request_id: int) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM seerr_hidden WHERE user_id = ? AND seerr_request_id = ?",
            (user_id, seerr_request_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_hidden_seerr_request_ids(user_id: int) -> set:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT seerr_request_id FROM seerr_hidden WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def get_hidden_seerr_requests(user_id: int) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT seerr_request_id, title, poster_url, hidden_at FROM seerr_hidden WHERE user_id = ? ORDER BY hidden_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
