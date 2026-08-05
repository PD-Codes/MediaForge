"""Calendar watcher tables.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...logger import get_logger

from ._core import _sql_chunks, get_db

logger = get_logger(__name__)


def init_calendar_db() -> None:
    conn = get_db()
    try:
        # title    = primary/German display string
        # title_en = English display string (NULL until the watcher fills it)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_media (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id      INTEGER NOT NULL UNIQUE,
                title        TEXT    NOT NULL,
                title_en     TEXT,
                poster_path  TEXT,
                last_updated REAL    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_episodes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id  INTEGER NOT NULL REFERENCES calendar_media(id) ON DELETE CASCADE,
                season    INTEGER, -- NULL for movies
                episode   INTEGER, -- NULL for movies
                name      TEXT,
                name_en   TEXT,
                air_date  TEXT,    -- YYYY-MM-DD
                still_path TEXT,
                UNIQUE(media_id, season, episode)
            )
            """
        )
        # Migrations for existing DBs (add the English columns if missing)
        for stmt in (
            "ALTER TABLE calendar_media ADD COLUMN title_en TEXT",
            "ALTER TABLE calendar_episodes ADD COLUMN name_en TEXT",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def save_calendar_media(tmdb_id: int, title: str, title_en: str, poster_path: str) -> int:
    import time as _time
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO calendar_media (tmdb_id, title, title_en, poster_path, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET
                title = excluded.title,
                title_en = excluded.title_en,
                poster_path = excluded.poster_path,
                last_updated = excluded.last_updated
            """,
            (tmdb_id, title, title_en, poster_path, _time.time()),
        )
        row = conn.execute("SELECT id FROM calendar_media WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def save_calendar_episode(media_id: int, season: int, episode: int, name: str, name_en: str, air_date: str, still_path: str) -> None:
    conn = get_db()
    try:
        if season is None and episode is None:
            # Movies have NULL season/episode. SQLite treats NULLs as distinct in
            # UNIQUE constraints, so ON CONFLICT never fires here — replace the
            # existing movie row manually to avoid accumulating duplicates.
            conn.execute(
                "DELETE FROM calendar_episodes WHERE media_id = ? AND season IS NULL AND episode IS NULL",
                (media_id,),
            )
            conn.execute(
                """
                INSERT INTO calendar_episodes (media_id, season, episode, name, name_en, air_date, still_path)
                VALUES (?, NULL, NULL, ?, ?, ?, ?)
                """,
                (media_id, name, name_en, air_date, still_path),
            )
        else:
            conn.execute(
                """
                INSERT INTO calendar_episodes (media_id, season, episode, name, name_en, air_date, still_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id, season, episode) DO UPDATE SET
                    name = excluded.name,
                    name_en = excluded.name_en,
                    air_date = excluded.air_date,
                    still_path = excluded.still_path
                """,
                (media_id, season, episode, name, name_en, air_date, still_path),
            )
        conn.commit()
    finally:
        conn.close()


def save_calendar_episodes(media_id: int, rows: list) -> None:
    """Write many episodes of one media in ONE transaction.

    *rows* is a list of (season, episode, name, name_en, air_date, still_path).

    save_calendar_episode() opens a connection, writes a single row, commits
    and closes. The watcher called it once per episode, so a series with 120
    episodes meant 120 connections and 120 commits -- each of them an fsync.
    Everything here goes through one executemany and one commit instead.
    Movie rows (season/episode NULL) still go through the single-row function:
    they need a DELETE first because SQLite treats NULLs as distinct in a
    UNIQUE constraint, and there is at most one of them per media.
    """
    if not rows:
        return
    conn = get_db()
    try:
        conn.executemany(
            """
            INSERT INTO calendar_episodes (media_id, season, episode, name, name_en, air_date, still_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id, season, episode) DO UPDATE SET
                name = excluded.name,
                name_en = excluded.name_en,
                air_date = excluded.air_date,
                still_path = excluded.still_path
            """,
            [(media_id, s, e, n, ne, ad, sp) for (s, e, n, ne, ad, sp) in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _calendar_ep_key(season, episode) -> tuple:
    """Normalise one (season, episode) pair for comparison.

    The keep list comes from TMDB's JSON and the stored rows come back as
    SQLite INTEGERs. A str/int mismatch here does not raise -- it silently
    makes every episode look stale, which would empty the calendar for that
    series. Coerce both sides through the same function so that cannot happen.
    """
    def _one(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    return (_one(season), _one(episode))


def delete_calendar_episodes_except(media_id: int, keep_episodes: list) -> None:
    """Delete episodes for a media that are not in the keep list (tuples of (season, episode)).

    The set difference is computed in Python rather than in SQL. The previous
    version built `... AND NOT ((season=? AND episode=?) OR (…) OR (…) …)`, one
    OR term per kept episode. SQLite parses a chain of ORs into a left-deep
    binary tree, so the expression depth grows with the number of terms and a
    long-running series (One Piece, Detective Conan, a daily soap -- anything
    past ~1000 episodes) hit SQLITE_MAX_EXPR_DEPTH:

        OperationalError: Expression tree is too large (maximum depth 1000)

    That aborted the whole calendar sync for the show. Raising the limit is a
    compile-time option of SQLite, so it is not available here -- and a query
    whose size scales with the data is the actual problem, not the ceiling.

    Deleting by primary key keeps the statement flat, and the chunking keeps it
    under SQLite's bound-variable cap (999 by default), the same way
    get_reading_progress_bulk() does.
    """
    conn = get_db()
    try:
        if not keep_episodes:
            conn.execute("DELETE FROM calendar_episodes WHERE media_id = ?", (media_id,))
            conn.commit()
            return

        keep = {_calendar_ep_key(s, e) for s, e in keep_episodes}
        rows = conn.execute(
            "SELECT id, season, episode FROM calendar_episodes WHERE media_id = ?",
            (media_id,),
        ).fetchall()
        stale = [
            row["id"] for row in rows
            if _calendar_ep_key(row["season"], row["episode"]) not in keep
        ]
        if not stale:
            return

        for chunk, placeholders in _sql_chunks(stale):
            conn.execute(
                f"DELETE FROM calendar_episodes WHERE id IN ({placeholders})", chunk
            )
        conn.commit()
    finally:
        conn.close()


def get_cached_calendar_media(tmdb_ids: list) -> dict:
    """Return dict mapping tmdb_id -> last_updated time from database."""
    if not tmdb_ids:
        return {}
    conn = get_db()
    try:
        out = {}
        for chunk, placeholders in _sql_chunks(tmdb_ids):
            rows = conn.execute(
                f"SELECT tmdb_id, last_updated FROM calendar_media WHERE tmdb_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                out[row["tmdb_id"]] = row["last_updated"]
        return out
    finally:
        conn.close()


def get_calendar_episodes_from_db(tmdb_ids: list) -> list:
    """Fetch stored calendar episodes for a list of TMDB IDs."""
    if not tmdb_ids:
        return []
    conn = get_db()
    try:
        out = []
        for chunk, placeholders in _sql_chunks(tmdb_ids):
            rows = conn.execute(
                f"""
                SELECT m.tmdb_id, m.title, m.title_en, m.poster_path,
                       e.season, e.episode, e.name, e.name_en, e.air_date, e.still_path
                FROM calendar_media m
                JOIN calendar_episodes e ON m.id = e.media_id
                WHERE m.tmdb_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            out.extend(dict(r) for r in rows)
        return out
    finally:
        conn.close()


def get_calendar_media_titles() -> list:
    """Return ``[(tmdb_id, title, title_en, max_air_date)]`` for all stored
    calendar media, where ``max_air_date`` is the latest episode date (or None).

    Lets a caller map a known show title to the TMDB id it's already synced under
    (by any source) so Crunchyroll title resolution can reuse the authoritative id
    instead of a wrong/duplicate one from a blind title search.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT m.tmdb_id, m.title, m.title_en, MAX(e.air_date) AS max_air
            FROM calendar_media m
            LEFT JOIN calendar_episodes e ON m.id = e.media_id
            GROUP BY m.id
            """
        ).fetchall()
        return [(r["tmdb_id"], r["title"], r["title_en"], r["max_air"]) for r in rows]
    finally:
        conn.close()
