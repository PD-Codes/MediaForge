"""AniList list import for MediaCalendar.

AniList's GraphQL API is public/read-only for a username's list (no auth
needed, no settings to add anywhere else -- fits the "everything stays in
this folder" brief without asking the user for another API key). AniList
identifies anime by its own id, not a TMDB id, so importing a list means:
fetch the user's entries -> resolve each title to a TMDB tv id via
tmdb_client.search_multi() (title match, picking the closest by simple
string-similarity + year proximity when AniList gives a start date) ->
add matched entries as items on a MediaCalendar List (db.add_list_item).

Titles AniList can't be confidently matched to a TMDB entry are skipped
and reported back to the caller (routes.py surfaces them in the import
result) rather than silently guessing -- mirrors the Android app's
AniListImportViewModel, which likewise flags unmatched titles instead of
picking blind.
"""

import difflib
import time

import requests

from . import db as mcdb
from . import tmdb_client as tmdb
from ....logger import get_logger

logger = get_logger(__name__)

_ANILIST_URL = "https://graphql.anilist.co"
_REQUEST_TIMEOUT = 15

# CURRENT = watching, PLANNING = plan to watch -- the two AniList statuses
# that make sense to keep mirrored into a MediaCalendar list; COMPLETED/
# DROPPED/PAUSED entries aren't "upcoming" or "to watch" in any useful
# sense for a release calendar, so the default import skips them.
DEFAULT_STATUSES = ("CURRENT", "PLANNING")

_QUERY = """
query ($userName: String, $statusIn: [MediaListStatus]) {
  MediaListCollection(userName: $userName, type: ANIME, status_in: $statusIn) {
    lists {
      entries {
        status
        media {
          id
          title { romaji english native }
          startDate { year }
          format
        }
      }
    }
  }
}
"""


class AniListError(Exception):
    """Wraps an AniList HTTP/GraphQL failure with a short, loggable message."""


def fetch_user_list(username: str, statuses: "tuple | list" = DEFAULT_STATUSES) -> list:
    """[{anilist_id, title, year, format, status}, ...] for `username`'s
    AniList entries in the given statuses."""
    username = (username or "").strip()
    if not username:
        raise AniListError("Kein AniList-Benutzername angegeben.")
    try:
        resp = requests.post(
            _ANILIST_URL,
            json={"query": _QUERY, "variables": {"userName": username, "statusIn": list(statuses)}},
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AniListError(f"AniList nicht erreichbar: {exc}") from exc
    if not resp.ok:
        raise AniListError(f"AniList {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("errors"):
        msg = "; ".join(e.get("message", "?") for e in data["errors"])
        raise AniListError(f"AniList: {msg}")

    out = []
    collection = (data.get("data") or {}).get("MediaListCollection") or {}
    for group in collection.get("lists", []):
        for entry in group.get("entries", []):
            media = entry.get("media") or {}
            title = ((media.get("title") or {}).get("english")
                     or (media.get("title") or {}).get("romaji")
                     or (media.get("title") or {}).get("native"))
            if not title:
                continue
            out.append({
                "anilist_id": media.get("id"),
                "title": title,
                "year": (media.get("startDate") or {}).get("year"),
                "format": media.get("format"),
                "status": entry.get("status"),
            })
    return out


def _best_match(title: str, year: "int | None", candidates: list) -> "dict | None":
    best, best_score = None, 0.0
    for c in candidates:
        if c.get("media_type") != "tv":
            continue
        name = c.get("name") or c.get("original_name") or ""
        score = difflib.SequenceMatcher(None, title.lower(), name.lower()).ratio()
        if year and c.get("first_air_date", "").startswith(str(year)):
            score += 0.15
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.55 else None


def import_to_list(list_id: int, username: str, statuses: "tuple | list" = DEFAULT_STATUSES) -> dict:
    """Fetch `username`'s AniList entries and add every confidently-matched
    one to MediaCalendar list `list_id`. Returns
    {"added": [...], "unmatched": [...]}."""
    if not tmdb.is_configured():
        raise tmdb.TmdbNotConfigured(
            "TMDB ist nicht konfiguriert -- unter Einstellungen -> Integrationen -> "
            "CineInfo einen TMDB API-Key hinterlegen."
        )
    entries = fetch_user_list(username, statuses)
    added, unmatched = [], []
    for entry in entries:
        try:
            candidates = tmdb.search_multi(entry["title"])
        except tmdb.TmdbError:
            unmatched.append(entry)
            continue
        match = _best_match(entry["title"], entry.get("year"), candidates)
        if not match:
            unmatched.append(entry)
            continue
        mcdb.add_list_item(
            list_id, match["id"], "tv",
            match.get("name") or match.get("original_name") or entry["title"],
            match.get("poster_path"), match.get("first_air_date"),
        )
        added.append({"anilist_title": entry["title"], "tmdb_id": match["id"],
                       "tmdb_title": match.get("name")})
        time.sleep(0.05)  # be polite to TMDB between search calls
    return {"added": added, "unmatched": unmatched}
