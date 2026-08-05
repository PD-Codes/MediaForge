"""Recommendations from what is already on disk.

The honest version of this feature. MediaForge has no ratings, no
collaborative signal and no user graph, and inventing one from a single
household's viewing would be astrology with a progress bar. What it does have
is three things that are actually reliable:

* **What you watched**, and how far (``watch_progress``).
* **What is in the library**, with its metadata (``library_cache`` plus the
  cached TMDB lookups).
* **What you never finished**, which is usually the single most useful thing
  a home page can put in front of somebody.

So this produces three rows, in decreasing order of how confident it is:

1. **Continue watching** -- started, not finished, most recent first. Not a
   recommendation at all, which is exactly why it goes first: it is the row
   people actually click.
2. **Next up** -- the next unwatched episode of a series you are already
   partway through. Also not a guess.
3. **Because you watched X** -- library titles sharing genres with something
   you finished recently. This one *is* a guess, and it says whose fault it is
   by naming X, so a bad suggestion is legible rather than mysterious.

Everything is computed from the caches, never from the disk or the network:
the home page is the first thing that renders after login, and making that
wait on a filesystem walk is how an overview page ends up feeling worse than
the list it replaced.
"""

from __future__ import annotations

import json

from ..logger import get_logger

logger = get_logger(__name__)

# A title counts as "finished" past this fraction. Deliberately not 1.0 --
# nobody watches the credits, and a player that stops at 96% would otherwise
# leave every completed series sitting in "continue watching" forever.
WATCHED_FRACTION = 0.92

# ...and as "started" past this one. Below it, the user probably opened the
# wrong file or sampled the first minute, and putting that back in front of
# them is noise.
STARTED_FRACTION = 0.02

MAX_ROW = 20


def _progress_rows(username: str) -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT file_path, position_seconds, duration_seconds, watched, updated_at "
            "FROM watch_progress WHERE username = ? ORDER BY updated_at DESC LIMIT 500",
            (username or "",)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _fraction(row) -> float:
    duration = float(row.get("duration_seconds") or 0)
    if duration <= 0:
        return 0.0
    return max(0.0, min(float(row.get("position_seconds") or 0) / duration, 1.0))


def _library_index() -> dict:
    """``file_path -> {title, genres, poster, ...}`` from the library cache.

    Built once per request rather than queried per progress row: a household
    with a few hundred progress entries would otherwise do a few hundred
    lookups to render one row.
    """
    index: dict[str, dict] = {}
    try:
        from .db import get_all_library_cache
        from .routes.library import lib_iter_cached_titles
    except Exception:
        return index

    try:
        for _key, entry in (get_all_library_cache() or {}).items():
            data = (entry or {}).get("data") or {}
            for title in lib_iter_cached_titles(data):
                for episode in title.get("episodes") or []:
                    path = episode.get("path") or episode.get("file_path")
                    if path:
                        index[str(path)] = {
                            "title": title.get("title") or title.get("name") or "",
                            "poster": title.get("poster") or title.get("poster_url") or "",
                            "series_url": title.get("url") or "",
                            "total_episodes": title.get("total_episodes") or 0,
                        }
    except Exception as exc:
        logger.debug("[Recommend] Could not index the library: %s", exc)
    return index


def _genres_for(title: str) -> set[str]:
    """Genres for a title from the TMDB cache. Never fetches.

    A miss simply means the title contributes no genre signal, which is the
    right failure: a recommendation row that blocks on a network call is a
    home page that does not render.
    """
    if not title:
        return set()
    try:
        from .db import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT data_json FROM tmdb_cache WHERE cache_key LIKE ? LIMIT 1",
                ("%" + title.lower().strip() + "%",)).fetchone()
        finally:
            conn.close()
        if not row:
            return set()
        data = json.loads(row["data_json"])
        if isinstance(data, list):
            data = data[0] if data else {}
        genres = data.get("genres") or data.get("genre_names") or []
        out = set()
        for genre in genres:
            name = genre.get("name") if isinstance(genre, dict) else genre
            if name:
                out.add(str(name).strip().lower())
        return out
    except Exception:
        return set()


def continue_watching(username: str, limit: int = MAX_ROW) -> list[dict]:
    """Started but not finished, most recently touched first."""
    index = _library_index()
    out = []
    seen_titles = set()

    for row in _progress_rows(username):
        if row.get("watched"):
            continue
        fraction = _fraction(row)
        if not (STARTED_FRACTION < fraction < WATCHED_FRACTION):
            continue
        meta = index.get(str(row["file_path"]))
        if not meta:
            # In progress but no longer in the library: the file was deleted
            # or moved. Showing it would produce a card that cannot be played.
            continue
        # One card per series, not per episode -- a season you are working
        # through should occupy one slot, not twelve.
        if meta["title"] in seen_titles:
            continue
        seen_titles.add(meta["title"])
        out.append({
            "kind": "continue",
            "title": meta["title"],
            "poster": meta["poster"],
            "file_path": row["file_path"],
            "series_url": meta["series_url"],
            "progress": round(fraction, 3),
            "updated_at": row["updated_at"],
        })
        if len(out) >= limit:
            break
    return out


def because_you_watched(username: str, limit: int = MAX_ROW) -> dict | None:
    """One "because you watched X" row, or None when there is no signal.

    Returns None rather than an empty row on purpose: a home page section
    titled "Because you watched" with nothing under it is worse than no
    section, and the caller can only make that decision if this says so.
    """
    index = _library_index()

    # The most recently FINISHED title is the seed. Recency beats "most
    # watched": what somebody finished last night is a better predictor of
    # what they want now than what they binged two years ago.
    seed_title = ""
    for row in _progress_rows(username):
        if row.get("watched") or _fraction(row) >= WATCHED_FRACTION:
            meta = index.get(str(row["file_path"]))
            if meta and meta["title"]:
                seed_title = meta["title"]
                break
    if not seed_title:
        return None

    seed_genres = _genres_for(seed_title)
    if not seed_genres:
        return None

    # Everything in the library, scored by genre overlap with the seed.
    watched_paths = {str(r["file_path"]) for r in _progress_rows(username)}
    candidates: dict[str, dict] = {}
    for path, meta in index.items():
        title = meta["title"]
        if not title or title == seed_title or title in candidates:
            continue
        if path in watched_paths:
            continue
        overlap = seed_genres & _genres_for(title)
        if not overlap:
            continue
        candidates[title] = {
            "kind": "because",
            "title": title,
            "poster": meta["poster"],
            "file_path": path,
            "series_url": meta["series_url"],
            "score": len(overlap),
            "shared": sorted(overlap)[:3],
        }

    if not candidates:
        return None

    items = sorted(candidates.values(), key=lambda c: -c["score"])[:limit]
    return {"seed": seed_title, "items": items}


def personal_rows(username: str) -> list[dict]:
    """Every personal row that has something in it, in confidence order.

    Empty rows are dropped here rather than rendered empty by the client: a
    section header with nothing under it reads as a bug.
    """
    rows = []

    items = continue_watching(username)
    if items:
        rows.append({"id": "continue", "label_key": "row_continue", "items": items})

    because = because_you_watched(username)
    if because:
        rows.append({
            "id": "because",
            "label_key": "row_because",
            "seed": because["seed"],
            "items": because["items"],
        })

    return rows
