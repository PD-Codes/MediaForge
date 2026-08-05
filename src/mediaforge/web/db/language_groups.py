"""Language fallback groups.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...config import MEDIAFORGE_CONFIG_DIR
from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


_CREATE_LANGUAGE_GROUPS_TABLE = """\
CREATE TABLE IF NOT EXISTS language_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    languages TEXT NOT NULL DEFAULT '[]',
    delete_replaced INTEGER NOT NULL DEFAULT 1
);
"""


def init_language_groups_db():
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute(_CREATE_LANGUAGE_GROUPS_TABLE)
        # Migration for groups created before the upgrade behaviour became
        # configurable. Default 1 keeps what those groups already did: replace
        # the old file instead of leaving both languages on disk.
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(language_groups)").fetchall()
        }
        if "delete_replaced" not in columns:
            conn.execute(
                "ALTER TABLE language_groups ADD COLUMN delete_replaced INTEGER NOT NULL DEFAULT 1"
            )
        conn.commit()
    finally:
        conn.close()


def _row_to_language_group(row):
    """Decode a language_groups row, tolerating a corrupt languages column."""
    import json

    try:
        languages = json.loads(row["languages"] or "[]")
    except (TypeError, ValueError):
        languages = []
    if not isinstance(languages, list):
        languages = []
    return {
        "id": row["id"],
        "name": row["name"],
        "languages": languages,
        "delete_replaced": bool(row["delete_replaced"]),
    }


def get_language_groups():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, languages, delete_replaced FROM language_groups ORDER BY id"
        ).fetchall()
        return [_row_to_language_group(r) for r in rows]
    finally:
        conn.close()


def get_language_group(group_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, languages, delete_replaced FROM language_groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        return _row_to_language_group(row) if row else None
    finally:
        conn.close()


def add_language_group(name, languages_json, delete_replaced=True):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO language_groups (name, languages, delete_replaced) VALUES (?, ?, ?)",
            (name, languages_json, 1 if delete_replaced else 0),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_language_group(group_id, name=None, languages_json=None, delete_replaced=None):
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if languages_json is not None:
        fields.append("languages = ?")
        values.append(languages_json)
    if delete_replaced is not None:
        fields.append("delete_replaced = ?")
        values.append(1 if delete_replaced else 0)
    if not fields:
        return

    values.append(group_id)
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE language_groups SET {', '.join(fields)} WHERE id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def count_language_group_users():
    """How many sync jobs + waiting/running downloads use any language group.

    Used to warn before language separation is switched off, which is what
    fallback groups need to work at all.
    """
    # ``..language_groups`` -- web/language_groups.py, the module that owns the
    # group VOCABULARY (prefix, ref formatting, chain resolution). This file
    # only owns the TABLE. The two share a name and are one dot apart, which
    # is a trap worth naming: written as a single dot this imports itself and
    # fails with ImportError the first time the function is called.
    from ..language_groups import GROUP_PREFIX

    like = GROUP_PREFIX + "%"
    conn = get_db()
    try:
        jobs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE language LIKE ?", (like,)
        ).fetchone()
        queued = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue "
            "WHERE language LIKE ? AND status IN ('queued', 'running')",
            (like,),
        ).fetchone()
        return (jobs["cnt"] if jobs else 0) + (queued["cnt"] if queued else 0)
    finally:
        conn.close()


def remove_language_group(group_id):
    """Delete a group. Returns (True, None) or (False, reason) if still in use.

    Mirrors remove_custom_path: a group referenced by a sync job or a waiting
    download must not vanish, because the reference is all those rows have —
    resolving it later would yield an empty chain and the job would silently
    stop syncing.
    """
    from ..language_groups import group_ref  # web/language_groups.py, not this file

    ref = group_ref(group_id)
    conn = get_db()
    try:
        row_sync = conn.execute(
            "SELECT COUNT(*) AS cnt FROM autosync_jobs WHERE language = ?", (ref,)
        ).fetchone()
        if row_sync and row_sync["cnt"] > 0:
            return False, "Diese Sprachgruppe wird von mindestens einem Auto-Sync-Job verwendet und kann nicht gelöscht werden."
        row_queue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM download_queue WHERE language = ? AND status IN ('queued', 'running')",
            (ref,),
        ).fetchone()
        if row_queue and row_queue["cnt"] > 0:
            return False, "Diese Sprachgruppe wird noch von aktiven oder wartenden Downloads in der Warteschlange verwendet."
        conn.execute("DELETE FROM language_groups WHERE id = ?", (group_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()
