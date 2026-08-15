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
import random

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


# How many tmdb_cache rows one "could be for you" pass is willing to read.
# ponytail: a single capped table scan, not N LIKE queries -- a library with
# 800 titles would otherwise fire 800 statements to build one row. Ceiling:
# beyond this many cached lookups the newest rows win and older ones stop
# contributing seeds. Upgrade path if that ever bites: index tmdb_cache by
# normalised title and join instead of scanning.
FORYOU_SCAN_LIMIT = 4000


def _norm(title) -> str:
    return str(title or "").strip().lower()


def _owned_titles() -> set[str]:
    """Every title the library already holds, normalised.

    This is the only thing standing between "could be for you" and
    recommending the show sitting one folder over, so it is deliberately
    generous: the library title plus, when the TMDB cache happens to know
    them, its aliases. A false positive here costs one missing suggestion, a
    false negative costs all credibility.
    """
    owned: set[str] = set()
    try:
        from .db import get_all_library_cache
        from .routes.library import lib_iter_cached_titles
    except Exception:
        return owned
    try:
        for _key, entry in (get_all_library_cache() or {}).items():
            data = (entry or {}).get("data") or {}
            for title in lib_iter_cached_titles(data):
                name = _norm(title.get("title") or title.get("name"))
                if name:
                    owned.add(name)
    except Exception as exc:
        logger.debug("[Recommend] Could not read owned titles: %s", exc)
    return owned


def _seed_titles() -> set[str]:
    """Titles to look up cached TMDB recommendations FROM, in priority order:
    a linked Jellyfin/Plex profile's own watch history, then the local
    library. A profile is preferred when one is linked and reachable because
    it knows what was actually watched on that server, which is not always
    the same set as what MediaForge's own library scan sees -- exactly the
    reasoning routes/browse.py already uses to let a linked profile REPLACE
    (not merge into) the local "continue watching" signal. This is a
    fallback chain for the same reason: a profile linked yesterday mixed with
    years of local library history would just be the noisier of the two
    signals, not a better one.

    Falls back to :func:`_owned_titles` whenever nothing is linked, the
    server cannot be reached, or it has no watch history yet -- so an
    instance with no media-server integration configured behaves exactly as
    it did before this existed.
    """
    try:
        from flask import session as _session
        from .db import get_user_ui_prefs
        from . import mediaplayer
        uid = _session.get("user_id")
        linked = ""
        if uid is not None:
            linked = str((get_user_ui_prefs(uid) or {}).get("mediaplayer_user") or "").strip()
        if linked and mediaplayer.is_configured():
            # since_ts=0: the whole history, same breadth as the local
            # library scan below, which has no time window either.
            stats = mediaplayer.watch_stats(linked, 0)
            if stats.get("available"):
                names = {_norm(t["name"]) for t in stats.get("top_titles") or []
                          if t.get("name")}
                if names:
                    return names
    except Exception as exc:
        logger.debug("[Recommend] Could not read mediaplayer watch stats: %s", exc)
    return _owned_titles()


def _cached_tmdb_entries(owned: set[str]) -> list[tuple[str, dict]]:
    """``(library title, cached TMDB payload)`` for the titles we own.

    One capped scan of ``tmdb_cache`` instead of a lookup per title (see
    ``FORYOU_SCAN_LIMIT``). Cache keys are ``"<title|imdb>|||<country>|||<lang>"``,
    so the part before the first separator is what we match on; rows keyed by
    IMDB id simply never match and are skipped.

    The title returned keeps the key's ORIGINAL casing, because callers show it
    to the user; the normalised form is only ever a matching/dedup key.

    Matching is bidirectional: a row counts as owned if its OWN cache-key
    title is in ``owned`` (the original check) OR if any of the row's
    ALIASES is (new). The second half matters whenever two providers stored
    the same show under folders that don't textually match each other or the
    cache key at all -- a Japanese-script folder from one provider and a
    romaji folder from another, say, with the cache itself keyed by yet a
    third (often English) title TMDB returned for the lookup. Without it,
    none of the three strings is ever equal under ``_norm()``, so the show's
    aliases (which DO include all three, that being what "aliases" means)
    never get merged into ``owned`` and the exact title a recommendation
    displays keeps slipping through the exclude set even though the show is
    demonstrably already in the library under two different providers.
    """
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    try:
        from .db import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT cache_key, data_json FROM tmdb_cache "
                "ORDER BY rowid DESC LIMIT ?", (FORYOU_SCAN_LIMIT,)).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[Recommend] tmdb_cache scan failed: %s", exc)
        return out

    for row in rows:
        display = str(row["cache_key"]).split("|||")[0].strip()
        key = _norm(display)
        if key in seen:
            continue
        try:
            data = json.loads(row["data_json"])
        except Exception:
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            continue
        aliases = data.get("titles") or []
        alias_keys = {_norm(a) for a in aliases}
        if key not in owned and owned.isdisjoint(alias_keys):
            continue
        seen.add(key)
        # Every alias AND the cache key itself count as owned from here on,
        # so a later candidate matching any of the three (the English TMDB
        # title, the Japanese folder, the romaji folder) is excluded too --
        # not just whichever single string happened to trigger the match
        # above.
        owned.add(key)
        owned.update(alias_keys)
        out.append((display or key, data))
    return out


# How many library titles one "could be for you" pass may re-fetch to repair
# its own input. Cache rows written before TMDB `recommendations` were stored
# carry an empty list forever on an instance that never looks those titles up
# again, so the row starves. The arithmetic: the route memoises for 6 h per
# account, so one account costs at most 8 lookups / 6 h = 32 TMDB calls a day,
# and a household of four at most 128 -- nothing next to TMDB's rate limit.
# It also stops by itself: once the pool is healthy the top-up never fires.
FORYOU_TOPUP_MAX = 8


def _schedule_recommendation_topup(titles, api_key, country, ui_lang) -> None:
    """Re-fetch a few library titles whose cached payload has no recommendations.

    Deliberately fire-and-forget on a daemon thread: the home page is waiting
    on for_you(), and eight sequential TMDB round trips is seconds of blank
    tab. THIS call returns whatever it already has, the repaired rows land in
    the cache meanwhile, and the NEXT call (or the "shuffle" button) sees
    them. Progressive healing beats a slow first paint.
    """
    if not titles:
        return

    def _work():
        from .db import get_db
        from .tmdb_cache import _tmdb_lookup_cached
        for title in titles:
            # One title at a time, each guarded on its own: a title TMDB has
            # forgotten about must cost its own row, not the rest of the batch.
            try:
                conn = get_db()
                try:
                    # Exact key, no LIKE: titles legitimately contain % and _.
                    conn.execute("DELETE FROM tmdb_cache WHERE cache_key = ?",
                                 (f"{title}|||{country}|||{ui_lang}",))
                    conn.commit()
                finally:
                    conn.close()
                _tmdb_lookup_cached(title, None, api_key, country, ui_lang)
            except Exception as exc:
                logger.debug("[Recommend] top-up failed for %r: %s", title, exc)

    import threading
    threading.Thread(target=_work, daemon=True, name="foryou-topup").start()


def _genre_names(data: dict) -> list[str]:
    names = []
    for genre in data.get("genres") or data.get("genre_names") or []:
        name = genre.get("name") if isinstance(genre, dict) else genre
        if name:
            names.append(str(name).strip())
    return names


def for_you(username: str, limit: int = MAX_ROW, hero: int = 5, shuffle: bool = False) -> dict:
    """"Could be for you": TMDB recommendations minus everything you own.

    The signal is borrowed, not invented: every cached TMDB lookup for a
    library title carries TMDB's own ``recommendations`` list. Counting how
    many of the user's titles point at the same stranger is a perfectly good
    "you would probably like this", and it costs zero network calls -- the
    whole tally comes out of caches that were filled for other reasons.

    Two rules keep it honest. Anything already in the library is dropped (a
    discovery row that suggests what you own is a bug wearing a feature's
    hat), and each item names up to two of the titles that produced it, so a
    bad suggestion is legible instead of mysterious.

    Only the first *hero* entries are enriched with overview/backdrop via the
    24 h-cached TMDB lookup; the rest stay poster-only. The home page must
    render immediately, and that budget buys at most five lookups a day.

    ``username`` is accepted for symmetry with the other rows (and for the
    per-user memo the route keeps); the library is household-wide, so it does
    not change the result today.

    ``shuffle`` is the Discover tab's "Shuffle" button (``?refresh=1`` on the
    route). Candidates are otherwise always the same fixed top-N by score, so
    without this a re-roll recomputed the exact same list every time -- the
    button visibly did nothing. Sampling from a capped top-scoring pool
    instead keeps "still relevant" while actually varying the result.
    """
    from .db import get_setting
    import time as _time

    out = {"configured": False, "items": [], "hero": [],
           "generated_at": _time.time()}
    api_key = (get_setting("cineinfo_tmdb_api_key", "") or "").strip()
    if not api_key:
        # No key means no cached recommendations either -- and definitely no
        # network call to go find some.
        return out
    out["configured"] = True
    try:
        from .cineinfo.registry import get_sources
        out["has_sources"] = bool(get_sources(enabled_only=True))
    except Exception:
        out["has_sources"] = False

    seed = _seed_titles()
    if not seed:
        return out
    # Always excludes what the LOCAL library holds, regardless of which set
    # seeded the lookup above -- a Jellyfin/Plex profile deciding what to
    # look FROM must never change what counts as "you already have this".
    # Unioned with the seed set too: a title already watched on a linked
    # profile is exactly as unwelcome as a recommendation as one already
    # sitting in the library, even when it never touched local storage.
    #
    # Computed AFTER _cached_tmdb_entries() runs, not before: that call adds
    # every owned title's ALIASES into the set it is given (see its own
    # docstring), and `owned | seed` below only sees those aliases if it
    # unions the sets once they are full. Building `exclude` first (the
    # original order here) silently dropped every alias-only match -- a
    # library title known to TMDB under a different name than the folder on
    # disk kept getting suggested back as "for you" forever.
    owned = _owned_titles()
    entries = _cached_tmdb_entries(seed)
    exclude = owned | seed
    # A second, exact exclude alongside the text-based one above: every
    # owned/seed title whose OWN cached TMDB lookup is in `entries` carries
    # its own tmdb_id, and a recommended candidate with that same id is
    # unambiguously the same title regardless of which language, region or
    # alternate title TMDB used for either side. Text matching (exclude,
    # above) only catches a candidate whose displayed title happens to equal
    # one of owned's known strings; this catches it even when it does not.
    exclude_ids = {data.get("tmdb_id") for _title, data in entries if data.get("tmdb_id")}

    candidates: dict[int, dict] = {}
    # Titles whose cached payload carries no recommendations at all. A missing
    # key means the row predates the field and was never enriched; an empty
    # list means TMDB was asked and had nothing. The first kind is worth a
    # re-fetch, the second almost never is, so they are kept apart and the
    # never-enriched ones go first.
    never_enriched: list[str] = []
    empty_result: list[str] = []
    for seed_title, data in entries:
        recs = data.get("recommendations")
        if not recs:
            (empty_result if isinstance(recs, list) else never_enriched).append(seed_title)
        seed_genres = _genre_names(data)
        for rec in data.get("recommendations") or []:
            if not isinstance(rec, dict):
                continue
            tmdb_id = rec.get("id")
            title = rec.get("title") or ""
            if not tmdb_id or not title or tmdb_id in exclude_ids or _norm(title) in exclude:
                continue
            item = candidates.get(tmdb_id)
            if item is None:
                item = candidates[tmdb_id] = {
                    "tmdb_id": tmdb_id,
                    "title": title,
                    "poster_url": ("https://image.tmdb.org/t/p/w342"
                                   + rec["poster_path"]) if rec.get("poster_path") else "",
                    "vote_average": round(float(rec.get("vote_average") or 0), 1),
                    "score": 0.0,
                    "reason_seeds": [],
                    "genre": "",
                    "_genres": {},
                }
            # media_type is only set when the cached payload knows it -- the
            # detail modal falls back to "tv" on its own.
            media_type = rec.get("media_type") or data.get("media_type")
            if media_type in ("tv", "movie"):
                item["media_type"] = media_type
            item["score"] += 1
            if len(item["reason_seeds"]) < 2:
                # seed_title carries its original casing: it is shown verbatim.
                item["reason_seeds"].append(seed_title)
            for name in seed_genres:
                item["_genres"][name] = item["_genres"].get(name, 0) + 1

    # Self-healing: a thin pool usually means unenriched cache rows, not a
    # boring library. Repair a bounded handful in the background (see
    # FORYOU_TOPUP_MAX) so the next pass has more to work with.
    if len(candidates) < limit and (never_enriched or empty_result):
        from .tmdb_cache import _current_ui_lang
        _schedule_recommendation_topup(
            (never_enriched + empty_result)[:FORYOU_TOPUP_MAX],
            api_key, get_setting("cineinfo_country", "DE"), _current_ui_lang())

    if not candidates:
        return out

    for item in candidates.values():
        # Vote average as a tiebreak only: it must never outrank an extra
        # library title agreeing, hence the /10.
        item["score"] = round(item["score"] + item["vote_average"] / 10.0, 3)
        genres = item.pop("_genres")
        item["genre"] = max(genres, key=genres.get) if genres else ""

    pool = sorted(candidates.values(), key=lambda c: -c["score"])
    if shuffle and len(pool) > 1:
        # A capped pool (not every candidate ever found), so a re-roll still
        # reads as "similar picks, different order" instead of suddenly
        # surfacing a barely-relevant afterthought just because the button
        # was clicked. Deliberately NOT gated on "more candidates than the
        # row limit" -- most households have well under MAX_ROW candidates
        # to begin with, and that guard used to make Shuffle silently do
        # nothing for exactly that (the common) case: with pool_cap equal to
        # the whole (small) pool, random.sample over it is still a genuine
        # permutation, not a no-op.
        pool_cap = pool[:max(limit * 3, limit + 5)]
        items = random.sample(pool_cap, min(max(0, limit), len(pool_cap)))
    else:
        items = pool[:max(0, limit)]
    out["items"] = items
    # The hero is the head of the rail, same entries in the same order --
    # two rankings on one screen read as a bug.
    out["hero"] = _hero_items(items[:max(0, hero)], api_key)
    return out


def _hero_items(items: list[dict], api_key: str) -> list[dict]:
    """Overview/backdrop for the few items that get a big card.

    Each lookup is wrapped on its own: one title TMDB has forgotten about must
    cost that one hero slot its extra fields, not the whole row.
    """
    from .db import get_setting
    from .tmdb_cache import _tmdb_lookup_cached, _current_ui_lang
    country = get_setting("cineinfo_country", "DE")
    ui_lang = _current_ui_lang()

    out = []
    for item in items:
        entry = dict(item)
        entry.update({"overview": "", "backdrop_url": "", "genres": [],
                      "year": "", "reason": ", ".join(item.get("reason_seeds") or [])})
        try:
            info = _tmdb_lookup_cached(item["title"], None, api_key, country, ui_lang) or {}
            if info.get("found"):
                details = info.get("raw_details") or {}
                backdrop = details.get("backdrop_path") or ""
                entry["overview"] = info.get("overview") or ""
                entry["genres"] = _genre_names(info)
                entry["year"] = str(details.get("first_air_date")
                                    or details.get("release_date") or "")[:4]
                if backdrop:
                    entry["backdrop_url"] = "https://image.tmdb.org/t/p/w1280" + backdrop
        except Exception as exc:
            logger.debug("[Recommend] hero lookup failed for %r: %s", item["title"], exc)
        out.append(entry)
    return out
