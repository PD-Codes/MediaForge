"""MediaCalendar's own, fully self-contained SQLite database.

Deliberately separate from MediaForge's main mediaforge.db (web/db.py) --
this module never imports from or writes into that database's schema. It
lives in the same directory (MEDIAFORGE_CONFIG_DIR, read-only import of
that path constant) purely as a matter of convention -- one place to look
for "where does MediaForge keep its data" -- not because it shares
anything with the main DB.

Why a separate DB instead of the generic app_settings/provider_cache
tables every other thirdparty uses: calendars, lists and their items are
genuinely relational data (a calendar has many genres/keywords/providers/
manual refs, a list has many items, a calendar can reference several
lists in different roles, ...). Cramming that into JSON blobs under a
handful of settings keys would work for the settings tab, but not for the
actual CRUD + filtering this integration is built around. A real schema
with real tables and real WHERE clauses is the right tool here -- see the
README/APP_SPEC of the Android app this is modeled on
(C:\\Program Files\\ILF\\DKS\\_GIT\\MediaCalendar) for the feature set this
schema exists to support.

Schema notes:
  - season_number/episode_number use -1 (not NULL) to mean "not
    applicable / this is a movie" -- TMDB itself uses season 0 for
    specials, so 0 isn't a safe sentinel, but -1 never collides with a
    real TMDB season/episode number. This lets every table that needs
    them use a plain UNIQUE(...) constraint without NULL-handling caveats
    (SQLite treats NULL != NULL in unique indexes, which would silently
    allow duplicate "movie" rows).
  - cached_releases has no UNIQUE constraint: the resolution engine
    (service.py) always deletes a calendar's whole cache and reinserts a
    fresh batch on every refresh (mirroring the Android app's per-calendar
    cache invalidation model), so upsert semantics are never needed.
"""

import sqlite3
import threading
import time
from pathlib import Path

from ....config import MEDIAFORGE_CONFIG_DIR

DB_PATH = MEDIAFORGE_CONFIG_DIR / "mediacalendar.db"

_SCHEMA_VERSION = 3

# One process-wide lock around writes -- this module is small and low-
# traffic enough (a handful of users editing calendars/lists) that a
# single lock is simpler and safer than per-connection WAL tuning, and
# matches the "don't touch anything outside this folder" brief by not
# depending on any concurrency helper from web/db.py.
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    name                        TEXT    NOT NULL,
    color                       TEXT    NOT NULL DEFAULT '#7c3aed',
    media_types                 TEXT    NOT NULL DEFAULT 'movie,tv',
    source                      TEXT    NOT NULL DEFAULT 'discover',   -- discover | list | library
    combine_list_with_discover  INTEGER NOT NULL DEFAULT 0,
    provider_filter_mode        TEXT    NOT NULL DEFAULT 'include',    -- include | exclude
    library_filter               TEXT    NOT NULL DEFAULT 'any',        -- any | in_library | missing
    seerr_filter                  TEXT    NOT NULL DEFAULT 'any',        -- any | requested | not_requested
    sort_order                     INTEGER NOT NULL DEFAULT 0,
    created_at                      REAL    NOT NULL,
    updated_at                       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_genres (
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    genre_id    INTEGER NOT NULL,
    PRIMARY KEY (calendar_id, genre_id)
);

-- keyword_id is the actual TMDB keyword id (what discover's with_keywords
-- param needs); keyword is just the display name shown in the UI chip.
-- Rows saved before this column existed have keyword_id=0 (see the
-- best-effort ALTER TABLE migration in init_db()) and will keep matching
-- nothing until re-picked from the search dropdown -- TMDB has no
-- name-based discover filter, so there's no way to "repair" old rows
-- without another network round-trip; simplest to just have the user
-- re-add the keyword once.
CREATE TABLE IF NOT EXISTS calendar_keywords (
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    keyword     TEXT    NOT NULL,
    keyword_id  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (calendar_id, keyword)
);

CREATE TABLE IF NOT EXISTS calendar_providers (
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    provider_id INTEGER NOT NULL,
    PRIMARY KEY (calendar_id, provider_id)
);

-- Individually added ("manual") or dropped ("excluded") titles, always
-- honoured regardless of the calendar's filter -- see CalendarFilter.manual
-- / .excluded in the Android app.
CREATE TABLE IF NOT EXISTS calendar_refs (
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    tmdb_id     INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,   -- movie | tv
    role        TEXT    NOT NULL,   -- manual | excluded
    title       TEXT,
    poster_path TEXT,
    PRIMARY KEY (calendar_id, tmdb_id, media_type, role)
);

-- A calendar can reference lists three ways: as its primary "source"
-- (source='list'), folded in on top of discover/library results
-- (role='positive'), or subtracted from them (role='negative'). Matches
-- CalendarSource.LIST / positiveListIds / negativeListIds in the Android
-- app, but normalized into one join table with a role column instead of
-- three separate comma-string columns.
CREATE TABLE IF NOT EXISTS calendar_list_links (
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,   -- source | positive | negative
    PRIMARY KEY (calendar_id, list_id, role)
);

CREATE TABLE IF NOT EXISTS lists (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    dynamic_enabled  INTEGER NOT NULL DEFAULT 0,
    media_types      TEXT    NOT NULL DEFAULT 'movie,tv',
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS list_genres (
    list_id  INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (list_id, genre_id)
);

-- Same keyword_id note as calendar_keywords above.
CREATE TABLE IF NOT EXISTS list_keywords (
    list_id    INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    keyword    TEXT    NOT NULL,
    keyword_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (list_id, keyword)
);

CREATE TABLE IF NOT EXISTS list_providers (
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    provider_id INTEGER NOT NULL,
    PRIMARY KEY (list_id, provider_id)
);

CREATE TABLE IF NOT EXISTS list_items (
    list_id      INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    tmdb_id      INTEGER NOT NULL,
    media_type   TEXT    NOT NULL,
    title        TEXT,
    poster_path  TEXT,
    release_date TEXT,
    added_at     REAL    NOT NULL,
    PRIMARY KEY (list_id, tmdb_id, media_type)
);

-- Per-calendar resolved-release cache. Composite id (not a real PK -- see
-- module docstring) so the same title can be cached independently under
-- several calendars. Always fully replaced per calendar on refresh.
CREATE TABLE IF NOT EXISTS cached_releases (
    calendar_id    INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    tmdb_id        INTEGER NOT NULL,
    media_type     TEXT    NOT NULL,
    title          TEXT,
    overview       TEXT,
    poster_path    TEXT,
    release_date   TEXT,
    season_number  INTEGER NOT NULL DEFAULT -1,
    episode_number INTEGER NOT NULL DEFAULT -1,
    episode_title  TEXT,
    genres_json     TEXT,
    providers_json   TEXT,
    in_library        INTEGER,   -- NULL = unknown/not checked, 0/1 = known
    requested          INTEGER,   -- NULL = unknown/not checked, 0/1 = known
    cached_at           REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cached_releases_calendar ON cached_releases(calendar_id, release_date);

-- Global (not per-calendar) watched/hidden state, keyed by title (+
-- episode for TV). Mirrors MediaProgressEntity.
CREATE TABLE IF NOT EXISTS media_progress (
    tmdb_id        INTEGER NOT NULL,
    media_type     TEXT    NOT NULL,
    season_number  INTEGER NOT NULL DEFAULT -1,
    episode_number INTEGER NOT NULL DEFAULT -1,
    watched        INTEGER NOT NULL DEFAULT 0,
    hidden         INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL    NOT NULL,
    PRIMARY KEY (tmdb_id, media_type, season_number, episode_number)
);

-- Releases the user has flagged "auto-download once available" (the
-- "Planned Download" pill/action) -- see service.py's planned-download
-- worker, which hourly re-searches AniWorld/S.TO/MegaKino (via
-- web/routes/autosync.py's find_site_candidates()) for every row here
-- whose release_date has passed and status is still 'pending', and
-- creates a real AutoSync job (web/db.py's add_autosync_job()) the moment
-- a good match is found, flipping status to 'queued'. 'failed' is set
-- after repeated no-match attempts so the worker eventually stops
-- hammering a title that just isn't going to show up on any site.
-- language/custom_path_id are the settings the user configures when
-- flagging (or later editing) a planned download -- see routes.py's
-- api_planned_download_add and static/mediacalendar.js's McPlanned module.
-- They're only consulted the moment a match is found and a *new* AutoSync
-- job needs creating (service.py's _check_planned_downloads); if a job for
-- that series URL already exists, its settings win instead, same as any
-- other "reuse the existing job" case in this codebase. poster_path is
-- purely cosmetic (thumbnail in the "Planned Downloads" management list).
CREATE TABLE IF NOT EXISTS planned_downloads (
    tmdb_id        INTEGER NOT NULL,
    media_type     TEXT    NOT NULL,
    season_number  INTEGER NOT NULL DEFAULT -1,
    episode_number INTEGER NOT NULL DEFAULT -1,
    title          TEXT,
    release_date   TEXT,
    poster_path    TEXT,
    language       TEXT    NOT NULL DEFAULT 'German Dub',
    custom_path_id INTEGER,
    status         TEXT    NOT NULL DEFAULT 'pending',  -- pending | queued | failed
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_checked   REAL,
    autosync_job_id INTEGER,
    created_at     REAL    NOT NULL,
    PRIMARY KEY (tmdb_id, media_type, season_number, episode_number)
);

-- Folder-name -> TMDB id resolution cache for the "My media library"
-- calendar source when the user has no Jellyfin/Plex connected (so
-- MediaScan's mediascan_cache stays empty forever) and instead relies on
-- MediaForge's own native, file-based library scan (web/routes/library.py's
-- library_cache, which only ever stores folder/file names -- no TMDB
-- linkage at all). See service.py's _native_library_tmdb_ids_by_type():
-- each distinct library folder name gets TMDB-searched once and the result
-- (or NULL if nothing matched) is cached here forever, so a calendar
-- refresh never re-searches a folder it already resolved. tmdb_id is
-- nullable on purpose -- a confirmed "no match" is still worth caching,
-- otherwise every refresh would keep retrying the same unmatchable folder.
CREATE TABLE IF NOT EXISTS library_title_matches (
    folder_key    TEXT    NOT NULL,
    media_type    TEXT    NOT NULL,   -- movie | tv
    tmdb_id       INTEGER,
    matched_title TEXT,
    resolved_at   REAL    NOT NULL,
    PRIMARY KEY (folder_key, media_type)
);
"""


def _connect() -> sqlite3.Connection:
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the schema if it doesn't exist yet. Safe to call on every
    app start (CREATE TABLE IF NOT EXISTS / plain integer PRAGMA
    user_version bump) -- called once from __init__.py's register(app)."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < _SCHEMA_VERSION:
            # Schema version 2: planned_downloads gained poster_path/language/
            # custom_path_id (the per-planned-release AutoSync config the user
            # can now set -- see McPlanned in static/mediacalendar.js). CREATE
            # TABLE IF NOT EXISTS above is a no-op on an already-existing table,
            # so existing installs need an explicit ALTER TABLE per column,
            # same best-effort "ignore if it already exists" pattern web/db.py's
            # init_autosync_db() uses.
            # Schema version 3: calendar_keywords/list_keywords gained
            # keyword_id (the actual TMDB keyword id discover's with_keywords
            # param needs -- rows saved before this existed only had the
            # display name, which silently matched nothing, see
            # service.py's _resolve_discover / _CLIENT_STRINGS's keyword
            # picker in static/mediacalendar.js).
            for _ddl in (
                "ALTER TABLE planned_downloads ADD COLUMN poster_path TEXT",
                "ALTER TABLE planned_downloads ADD COLUMN language TEXT NOT NULL DEFAULT 'German Dub'",
                "ALTER TABLE planned_downloads ADD COLUMN custom_path_id INTEGER",
                "ALTER TABLE calendar_keywords ADD COLUMN keyword_id INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE list_keywords ADD COLUMN keyword_id INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    conn.execute(_ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists (fresh install created it via _SCHEMA already)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

def list_calendars() -> list:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM calendars ORDER BY sort_order, id").fetchall()
        return [_calendar_row_to_dict(conn, r) for r in rows]
    finally:
        conn.close()


def get_calendar(calendar_id: int) -> "dict | None":
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM calendars WHERE id = ?", (calendar_id,)).fetchone()
        return _calendar_row_to_dict(conn, row) if row else None
    finally:
        conn.close()


def _calendar_row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    cid = row["id"]
    genres = [r["genre_id"] for r in conn.execute(
        "SELECT genre_id FROM calendar_genres WHERE calendar_id = ?", (cid,))]
    # {id, name} pairs -- id is the actual TMDB keyword id used for
    # filtering (see service.py's _resolve_discover), name is just for the
    # UI chip. See init_db()'s schema-version-3 migration note for why
    # older rows may have id=0.
    keywords = [{"id": r["keyword_id"], "name": r["keyword"]} for r in conn.execute(
        "SELECT keyword, keyword_id FROM calendar_keywords WHERE calendar_id = ?", (cid,))]
    providers = [r["provider_id"] for r in conn.execute(
        "SELECT provider_id FROM calendar_providers WHERE calendar_id = ?", (cid,))]
    manual = [dict(r) for r in conn.execute(
        "SELECT tmdb_id, media_type, title, poster_path FROM calendar_refs "
        "WHERE calendar_id = ? AND role = 'manual'", (cid,))]
    excluded = [dict(r) for r in conn.execute(
        "SELECT tmdb_id, media_type, title, poster_path FROM calendar_refs "
        "WHERE calendar_id = ? AND role = 'excluded'", (cid,))]
    list_ids = {"source": [], "positive": [], "negative": []}
    for r in conn.execute(
            "SELECT list_id, role FROM calendar_list_links WHERE calendar_id = ?", (cid,)):
        list_ids.setdefault(r["role"], []).append(r["list_id"])
    return {
        "id": cid,
        "name": row["name"],
        "color": row["color"],
        "media_types": (row["media_types"] or "").split(",") if row["media_types"] else [],
        "source": row["source"],
        "combine_list_with_discover": bool(row["combine_list_with_discover"]),
        "provider_filter_mode": row["provider_filter_mode"],
        "library_filter": row["library_filter"],
        "seerr_filter": row["seerr_filter"],
        "sort_order": row["sort_order"],
        "genres": genres,
        "keywords": keywords,
        "providers": providers,
        "manual": manual,
        "excluded": excluded,
        "list_ids": list_ids,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_calendar(data: dict, calendar_id: "int | None" = None) -> int:
    """Insert (calendar_id=None) or update (calendar_id=<id>) a calendar,
    replacing all of its child rows (genres/keywords/providers/refs/list
    links) with whatever's in `data`. Returns the calendar id."""
    now = _now()
    media_types = ",".join(data.get("media_types") or ["movie", "tv"])
    with _write_lock:
        conn = _connect()
        try:
            if calendar_id is None:
                cur = conn.execute(
                    "INSERT INTO calendars (name, color, media_types, source, "
                    "combine_list_with_discover, provider_filter_mode, library_filter, "
                    "seerr_filter, sort_order, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["name"], data.get("color", "#7c3aed"), media_types,
                     data.get("source", "discover"),
                     1 if data.get("combine_list_with_discover") else 0,
                     data.get("provider_filter_mode", "include"),
                     data.get("library_filter", "any"),
                     data.get("seerr_filter", "any"),
                     data.get("sort_order", 0), now, now),
                )
                calendar_id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE calendars SET name=?, color=?, media_types=?, source=?, "
                    "combine_list_with_discover=?, provider_filter_mode=?, library_filter=?, "
                    "seerr_filter=?, sort_order=?, updated_at=? WHERE id=?",
                    (data["name"], data.get("color", "#7c3aed"), media_types,
                     data.get("source", "discover"),
                     1 if data.get("combine_list_with_discover") else 0,
                     data.get("provider_filter_mode", "include"),
                     data.get("library_filter", "any"),
                     data.get("seerr_filter", "any"),
                     data.get("sort_order", 0), now, calendar_id),
                )
                conn.execute("DELETE FROM calendar_genres WHERE calendar_id=?", (calendar_id,))
                conn.execute("DELETE FROM calendar_keywords WHERE calendar_id=?", (calendar_id,))
                conn.execute("DELETE FROM calendar_providers WHERE calendar_id=?", (calendar_id,))
                conn.execute("DELETE FROM calendar_refs WHERE calendar_id=?", (calendar_id,))
                conn.execute("DELETE FROM calendar_list_links WHERE calendar_id=?", (calendar_id,))

            for genre_id in data.get("genres", []):
                conn.execute(
                    "INSERT OR IGNORE INTO calendar_genres (calendar_id, genre_id) VALUES (?, ?)",
                    (calendar_id, int(genre_id)))
            for keyword in data.get("keywords", []):
                # {id, name} dicts (see static/mediacalendar.js's keyword
                # picker) -- name alone (old shape, pre schema-version-3) is
                # still accepted so a stale client payload doesn't hard-fail,
                # it'll just save with keyword_id=0 (matches nothing, same
                # as before this fix) until re-picked.
                name = (keyword.get("name") if isinstance(keyword, dict) else keyword) or ""
                kw_id = keyword.get("id") if isinstance(keyword, dict) else None
                if name.strip():
                    conn.execute(
                        "INSERT OR IGNORE INTO calendar_keywords (calendar_id, keyword, keyword_id) "
                        "VALUES (?, ?, ?)",
                        (calendar_id, name.strip(), int(kw_id) if kw_id else 0))
            for provider_id in data.get("providers", []):
                conn.execute(
                    "INSERT OR IGNORE INTO calendar_providers (calendar_id, provider_id) VALUES (?, ?)",
                    (calendar_id, int(provider_id)))
            for ref in data.get("manual", []):
                conn.execute(
                    "INSERT OR IGNORE INTO calendar_refs "
                    "(calendar_id, tmdb_id, media_type, role, title, poster_path) "
                    "VALUES (?, ?, ?, 'manual', ?, ?)",
                    (calendar_id, int(ref["tmdb_id"]), ref["media_type"],
                     ref.get("title"), ref.get("poster_path")))
            for ref in data.get("excluded", []):
                conn.execute(
                    "INSERT OR IGNORE INTO calendar_refs "
                    "(calendar_id, tmdb_id, media_type, role, title, poster_path) "
                    "VALUES (?, ?, ?, 'excluded', ?, ?)",
                    (calendar_id, int(ref["tmdb_id"]), ref["media_type"],
                     ref.get("title"), ref.get("poster_path")))
            list_ids = data.get("list_ids", {})
            for role in ("source", "positive", "negative"):
                for list_id in list_ids.get(role, []):
                    conn.execute(
                        "INSERT OR IGNORE INTO calendar_list_links (calendar_id, list_id, role) "
                        "VALUES (?, ?, ?)",
                        (calendar_id, int(list_id), role))
            conn.commit()
            return calendar_id
        finally:
            conn.close()


def duplicate_calendar(calendar_id: int) -> "int | None":
    src = get_calendar(calendar_id)
    if not src:
        return None
    src["name"] = src["name"] + " (Kopie)"
    return save_calendar(src)


def delete_calendar(calendar_id: int) -> None:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM calendars WHERE id=?", (calendar_id,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

def list_lists() -> list:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM lists ORDER BY name").fetchall()
        return [_list_row_to_dict(conn, r) for r in rows]
    finally:
        conn.close()


def get_list(list_id: int) -> "dict | None":
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        return _list_row_to_dict(conn, row) if row else None
    finally:
        conn.close()


def _list_row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    lid = row["id"]
    genres = [r["genre_id"] for r in conn.execute(
        "SELECT genre_id FROM list_genres WHERE list_id = ?", (lid,))]
    # See _calendar_row_to_dict's comment -- same {id, name} shape, same
    # reason (TMDB discover needs the numeric keyword id, not its name).
    keywords = [{"id": r["keyword_id"], "name": r["keyword"]} for r in conn.execute(
        "SELECT keyword, keyword_id FROM list_keywords WHERE list_id = ?", (lid,))]
    providers = [r["provider_id"] for r in conn.execute(
        "SELECT provider_id FROM list_providers WHERE list_id = ?", (lid,))]
    items = [dict(r) for r in conn.execute(
        "SELECT tmdb_id, media_type, title, poster_path, release_date, added_at "
        "FROM list_items WHERE list_id = ? ORDER BY added_at DESC", (lid,))]
    return {
        "id": lid,
        "name": row["name"],
        "dynamic_enabled": bool(row["dynamic_enabled"]),
        "media_types": (row["media_types"] or "").split(",") if row["media_types"] else [],
        "genres": genres,
        "keywords": keywords,
        "providers": providers,
        "items": items,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_list(name: str) -> int:
    now = _now()
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO lists (name, created_at, updated_at) VALUES (?, ?, ?)",
                (name, now, now))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def save_list_settings(list_id: int, data: dict) -> None:
    now = _now()
    media_types = ",".join(data.get("media_types") or ["movie", "tv"])
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE lists SET name=?, dynamic_enabled=?, media_types=?, updated_at=? WHERE id=?",
                (data["name"], 1 if data.get("dynamic_enabled") else 0, media_types, now, list_id))
            conn.execute("DELETE FROM list_genres WHERE list_id=?", (list_id,))
            conn.execute("DELETE FROM list_keywords WHERE list_id=?", (list_id,))
            conn.execute("DELETE FROM list_providers WHERE list_id=?", (list_id,))
            for genre_id in data.get("genres", []):
                conn.execute("INSERT OR IGNORE INTO list_genres (list_id, genre_id) VALUES (?, ?)",
                             (list_id, int(genre_id)))
            for keyword in data.get("keywords", []):
                name = (keyword.get("name") if isinstance(keyword, dict) else keyword) or ""
                kw_id = keyword.get("id") if isinstance(keyword, dict) else None
                if name.strip():
                    conn.execute(
                        "INSERT OR IGNORE INTO list_keywords (list_id, keyword, keyword_id) VALUES (?, ?, ?)",
                        (list_id, name.strip(), int(kw_id) if kw_id else 0))
            for provider_id in data.get("providers", []):
                conn.execute("INSERT OR IGNORE INTO list_providers (list_id, provider_id) VALUES (?, ?)",
                             (list_id, int(provider_id)))
            conn.commit()
        finally:
            conn.close()


def delete_list(list_id: int) -> None:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM lists WHERE id=?", (list_id,))
            conn.commit()
        finally:
            conn.close()


def add_list_item(list_id: int, tmdb_id: int, media_type: str, title: str,
                   poster_path: "str | None", release_date: "str | None") -> None:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO list_items "
                "(list_id, tmdb_id, media_type, title, poster_path, release_date, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (list_id, tmdb_id, media_type, title, poster_path, release_date, _now()))
            conn.commit()
        finally:
            conn.close()


def remove_list_item(list_id: int, tmdb_id: int, media_type: str) -> None:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM list_items WHERE list_id=? AND tmdb_id=? AND media_type=?",
                (list_id, tmdb_id, media_type))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Cached releases
# ---------------------------------------------------------------------------

def get_cached_releases(calendar_id: int, max_age: float) -> "list | None":
    """Return the calendar's cached releases if the newest cache row is
    within max_age seconds, else None (caller should re-resolve)."""
    conn = _connect()
    try:
        newest = conn.execute(
            "SELECT MAX(cached_at) AS t FROM cached_releases WHERE calendar_id = ?",
            (calendar_id,)).fetchone()["t"]
        if newest is None or (_now() - newest) > max_age:
            return None
        rows = conn.execute(
            "SELECT * FROM cached_releases WHERE calendar_id = ? ORDER BY release_date",
            (calendar_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def replace_cached_releases(calendar_id: int, releases: list) -> None:
    now = _now()
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM cached_releases WHERE calendar_id=?", (calendar_id,))
            for r in releases:
                conn.execute(
                    "INSERT INTO cached_releases (calendar_id, tmdb_id, media_type, title, "
                    "overview, poster_path, release_date, season_number, episode_number, "
                    "episode_title, genres_json, providers_json, in_library, requested, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (calendar_id, r["tmdb_id"], r["media_type"], r.get("title"),
                     r.get("overview"), r.get("poster_path"), r.get("release_date"),
                     r.get("season_number", -1), r.get("episode_number", -1),
                     r.get("episode_title"), r.get("genres_json"), r.get("providers_json"),
                     r.get("in_library"), r.get("requested"), now))
            conn.commit()
        finally:
            conn.close()


def clear_cache(calendar_id: "int | None" = None) -> None:
    with _write_lock:
        conn = _connect()
        try:
            if calendar_id is None:
                conn.execute("DELETE FROM cached_releases")
            else:
                conn.execute("DELETE FROM cached_releases WHERE calendar_id=?", (calendar_id,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Media progress (watched/hidden)
# ---------------------------------------------------------------------------

def set_progress(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                  watched: "bool | None" = None, hidden: "bool | None" = None) -> None:
    with _write_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT watched, hidden FROM media_progress "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                (tmdb_id, media_type, season_number, episode_number)).fetchone()
            new_watched = int(watched) if watched is not None else (row["watched"] if row else 0)
            new_hidden = int(hidden) if hidden is not None else (row["hidden"] if row else 0)
            conn.execute(
                "INSERT INTO media_progress (tmdb_id, media_type, season_number, episode_number, "
                "watched, hidden, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tmdb_id, media_type, season_number, episode_number) DO UPDATE SET "
                "watched=excluded.watched, hidden=excluded.hidden, updated_at=excluded.updated_at",
                (tmdb_id, media_type, season_number, episode_number, new_watched, new_hidden, _now()))
            conn.commit()
        finally:
            conn.close()


def get_all_progress() -> dict:
    """Return {(tmdb_id, media_type, season_number, episode_number): {watched, hidden}}."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM media_progress").fetchall()
        return {
            (r["tmdb_id"], r["media_type"], r["season_number"], r["episode_number"]):
                {"watched": bool(r["watched"]), "hidden": bool(r["hidden"])}
            for r in rows
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Planned downloads ("auto-download once available" -- see service.py's
# planned-download worker)
# ---------------------------------------------------------------------------

def add_planned_download(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                          title: "str | None", release_date: "str | None",
                          poster_path: "str | None" = None,
                          language: str = "German Dub",
                          custom_path_id: "int | None" = None) -> None:
    """Flag a release for the planned-download worker, or update the
    language/path config of one that's already flagged -- this single
    function is both "add" and "edit" (see routes.py's
    api_planned_download_add, called from both the calendar's "Plan
    auto-download" action and the "Planned Downloads" management list's
    "Edit" button).

    INSERT OR IGNORE is a no-op if the row already exists (so re-flagging
    doesn't reset a 'queued'/'failed' status back to 'pending'), but the
    language/custom_path_id config is applied via an explicit UPDATE
    afterwards either way, so editing an existing row's config still takes
    effect even though the INSERT itself was skipped."""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO planned_downloads "
                "(tmdb_id, media_type, season_number, episode_number, title, release_date, "
                "poster_path, language, custom_path_id, status, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                (tmdb_id, media_type, season_number, episode_number, title, release_date,
                 poster_path, language, custom_path_id, _now()))
            conn.execute(
                "UPDATE planned_downloads SET language = ?, custom_path_id = ? "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                (language, custom_path_id, tmdb_id, media_type, season_number, episode_number))
            conn.commit()
        finally:
            conn.close()


def remove_planned_download(tmdb_id: int, media_type: str, season_number: int, episode_number: int) -> None:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM planned_downloads "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                (tmdb_id, media_type, season_number, episode_number))
            conn.commit()
        finally:
            conn.close()


def get_planned_download(tmdb_id: int, media_type: str, season_number: int, episode_number: int) -> "dict | None":
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM planned_downloads "
            "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
            (tmdb_id, media_type, season_number, episode_number)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_planned_downloads() -> dict:
    """Return {(tmdb_id, media_type, season_number, episode_number): row_dict}
    for every planned download -- shaped for a cheap per-release lookup
    when enriching a resolved calendar's releases (see service.py's
    _postprocess()), the same pattern get_all_progress() already uses."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM planned_downloads").fetchall()
        return {
            (r["tmdb_id"], r["media_type"], r["season_number"], r["episode_number"]): dict(r)
            for r in rows
        }
    finally:
        conn.close()


def list_all_planned_downloads() -> list:
    """Every planned-download row regardless of status, newest first --
    feeds the "Planned Downloads" management list/tab (routes.py's
    api_planned_downloads_list), where the user reviews status, edits the
    language/path config, or removes a flagged release. Contrast with
    list_pending_planned_downloads(), which is the worker's own narrower
    "still due for a check" query."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM planned_downloads ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_pending_planned_downloads() -> list:
    """Rows the worker should actively (re-)check: still 'pending' and due
    (release_date today or earlier -- checking before the release date is
    pointless, nothing could possibly have it yet)."""
    from datetime import date
    today = date.today().isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM planned_downloads WHERE status = 'pending' "
            "AND release_date IS NOT NULL AND release_date <= ?", (today,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_planned_download_result(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                                  status: "str | None" = None, autosync_job_id: "int | None" = None,
                                  increment_attempts: bool = False) -> None:
    with _write_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT attempts FROM planned_downloads "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                (tmdb_id, media_type, season_number, episode_number)).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + (1 if increment_attempts else 0)
            fields = ["attempts = ?", "last_checked = ?"]
            params = [attempts, _now()]
            if status is not None:
                fields.append("status = ?")
                params.append(status)
            if autosync_job_id is not None:
                fields.append("autosync_job_id = ?")
                params.append(autosync_job_id)
            params += [tmdb_id, media_type, season_number, episode_number]
            conn.execute(
                f"UPDATE planned_downloads SET {', '.join(fields)} "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                params)
            conn.commit()
        finally:
            conn.close()


def list_active_planned_downloads() -> list:
    """'pending' or 'failed' rows -- the ones a release_date re-sync is
    still worth doing for (see service.py's _resync_planned_release_dates()).
    'queued' rows already became a real AutoSync job and no longer need
    their stored release_date kept accurate here."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM planned_downloads WHERE status IN ('pending', 'failed')").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_planned_download_date(tmdb_id: int, media_type: str, season_number: int, episode_number: int,
                                  release_date: str, revive: bool = False) -> None:
    """Applies a re-fetched TMDB release_date to an already-flagged planned
    download (see service.py's _resync_planned_release_dates() for why this
    is needed -- TMDB reschedules episodes after the fact). `revive=True`
    (the row was 'failed') also resets it back to 'pending' with attempts
    cleared, since the stale date -- not a genuine no-match -- may well have
    been why it was given up on."""
    with _write_lock:
        conn = _connect()
        try:
            fields = ["release_date = ?"]
            params = [release_date]
            if revive:
                fields += ["status = 'pending'", "attempts = 0"]
            params += [tmdb_id, media_type, season_number, episode_number]
            conn.execute(
                f"UPDATE planned_downloads SET {', '.join(fields)} "
                "WHERE tmdb_id=? AND media_type=? AND season_number=? AND episode_number=?",
                params)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Native-library folder -> TMDB match cache (see service.py's
# _native_library_tmdb_ids_by_type())
# ---------------------------------------------------------------------------

def get_library_title_matches(media_type: str) -> dict:
    """{folder_key: {tmdb_id, matched_title, resolved_at}} for every folder
    already resolved (or confirmed unmatchable, tmdb_id=NULL) for this
    media_type -- one query up front so a refresh doesn't hit the DB once
    per folder."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT folder_key, tmdb_id, matched_title, resolved_at "
            "FROM library_title_matches WHERE media_type = ?",
            (media_type,),
        ).fetchall()
        return {r["folder_key"]: dict(r) for r in rows}
    finally:
        conn.close()


def set_library_title_match(folder_key: str, media_type: str, tmdb_id: "int | None",
                             matched_title: "str | None") -> None:
    """Persists a folder's resolved TMDB id (or None for a confirmed
    no-match) so future calendar refreshes never re-search it."""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO library_title_matches (folder_key, media_type, tmdb_id, matched_title, resolved_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(folder_key, media_type) DO UPDATE SET
                       tmdb_id       = excluded.tmdb_id,
                       matched_title = excluded.matched_title,
                       resolved_at   = excluded.resolved_at""",
                (folder_key, media_type, tmdb_id, matched_title, _now()),
            )
            conn.commit()
        finally:
            conn.close()
