"""Read-only Jellyfin / Plex client for the personal home rows.

routes/integrations.py owns the *connection* (server type, URL, admin token)
and uses it for one thing: telling the media server to rescan its library.
This module is the other direction -- reading a **person's** playback data
back out of that server, for two home page features:

    continue_watching()  -> the "Continue watching" row, when the account has
                            linked itself to a media-server user
    watch_stats()        -> the "Wrapped" recap card

Why this is a separate module and not more routes in integrations.py:

* It is read-only and per user. integrations.py is admin-only settings; every
  function here runs for a normal account and must never expose another
  person's history. The mapping (MediaForge account -> media-server user) is
  a per-user UI preference, ``mediaplayer_user`` -- see db.USER_UI_PREF_KEYS.
* Both servers are asked with the *admin* token that is already stored. That
  is a deliberate trade-off the user picked over a second login per account:
  it means the id in ``mediaplayer_user`` decides whose history is read, so
  every entry point below validates that id against list_users() before it
  is put into a URL. An unvalidated id here would be an IDOR *and* a path
  injection into the media server's API.

Nothing in here raises: a home page must not break because Jellyfin is down.
Every public function returns an empty list/dict on any failure and logs at
debug level.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from ..logger import get_logger
from .db import get_setting
from .queue_worker import _normalize_media_url
from .queue_worker import _validate_server_url

logger = get_logger(__name__)

# Both servers are on the LAN in the normal case, so these are short. A home
# page that waits eight seconds for an off-line Jellyfin is a broken home page.
_TIMEOUT = 6
_USERS_TTL = 300          # user list changes about never
_DATA_TTL = 60            # resume list / stats

# key -> (expires_at, value)
_cache: dict = {}


def _cached(key, ttl, producer):
    """Tiny TTL memo. Shared by every call below so a page that renders the
    Continue-watching row *and* the Wrapped card asks the server once."""
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = producer()
    _cache[key] = (now + ttl, value)
    return value


def invalidate_cache() -> None:
    """Drop everything. Called when the media-player settings are saved, so a
    changed URL/token is not shadowed by a minute of stale answers."""
    _cache.clear()


# ---------------------------------------------------------------- connection

def config() -> dict:
    """The configured server, or ``{}`` when it is not usable.

    ``url`` is already normalised and SSRF-validated here rather than at each
    call site: every request below interpolates it into a URL string, and a
    single missed check is the whole point of a check.
    """
    kind = (get_setting("mediaplayer_type", "") or "").strip().lower()
    token = (get_setting("mediaplayer_apikey", "") or "").strip()
    if kind not in ("jellyfin", "plex") or not token:
        return {}
    raw = get_setting("mediaplayer_url" if kind == "jellyfin" else "mediaplayer_plex_url", "")
    url = _normalize_media_url(raw or "")
    if not url:
        return {}
    try:
        _validate_server_url(url)
    except ValueError:
        logger.debug("[MediaPlayer] stored URL rejected by the SSRF check")
        return {}
    return {"kind": kind, "url": url.rstrip("/"), "token": token}


def is_configured() -> bool:
    return bool(config())


def _get_json(url, headers=None):
    """GET *url* and parse JSON, or return None. Never raises."""
    try:
        req = urllib.request.Request(url, headers=dict(headers or {}, **{
            "Accept": "application/json",
        }))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception as exc:
        logger.debug("[MediaPlayer] GET failed (%s): %s", type(exc).__name__, exc)
        return None


def _jf_headers(cfg):
    return {"X-Emby-Token": cfg["token"]}


def _plex_url(cfg, path, **params):
    params["X-Plex-Token"] = cfg["token"]
    return "%s%s?%s" % (cfg["url"], path, urllib.parse.urlencode(params))


# ---------------------------------------------------------------------- users

def list_users() -> list:
    """Every user account on the configured server: ``[{"id", "name"}]``.

    This is the list the profile dropdown offers *and* the allow-list every
    other function validates against, which is why it is one function and not
    two.
    """
    cfg = config()
    if not cfg:
        return []
    return _cached("users:%s:%s" % (cfg["kind"], cfg["url"]), _USERS_TTL,
                   lambda: _list_users(cfg))


def _list_users(cfg) -> list:
    out = []
    if cfg["kind"] == "jellyfin":
        data = _get_json(cfg["url"] + "/Users", _jf_headers(cfg))
        for user in (data or []):
            uid, name = user.get("Id"), user.get("Name")
            if uid and name:
                out.append({"id": str(uid), "name": str(name)})
    else:
        # /accounts lists the owner plus every managed/shared user that has
        # ever played something on this server. Account id "1" is the owner.
        data = _get_json(_plex_url(cfg, "/accounts"))
        container = (data or {}).get("MediaContainer", {})
        for acc in container.get("Account", []) or []:
            uid, name = acc.get("id"), acc.get("name")
            if uid is None:
                continue
            out.append({"id": str(uid), "name": str(name or "Plex")})
    out.sort(key=lambda u: u["name"].lower())
    return out


def resolve_user(user_id) -> dict:
    """Return the ``{"id","name"}`` entry for *user_id*, or ``{}``.

    The gate for everything below. A caller must never pass a stored id
    straight into a request URL: the value comes from a per-user preference,
    so an account could otherwise point it at any id it likes (another
    person's history) or at path-traversal junk.
    """
    wanted = str(user_id or "").strip()
    if not wanted:
        return {}
    for user in list_users():
        if user["id"] == wanted:
            return user
    return {}


# ----------------------------------------------------------- continue watching

def continue_watching(user_id, limit=15) -> list:
    """Resume points for *user_id*, newest first.

    Normalised to the shape static/home_feed.js already renders for the local
    row, plus ``remote``/``server``/``open_url`` so the card can say where it
    came from and hand playback back to the media server (MediaForge has no
    file path for a title it did not download).
    """
    cfg = config()
    user = resolve_user(user_id)
    if not cfg or not user:
        return []
    limit = max(1, min(int(limit or 15), 40))
    items = _cached("resume:%s:%s:%d" % (cfg["kind"], user["id"], limit), _DATA_TTL,
                    lambda: (_jf_resume(cfg, user, limit) if cfg["kind"] == "jellyfin"
                             else _plex_on_deck(cfg, user, limit)))
    return items or []


def _jf_resume(cfg, user, limit) -> list:
    url = cfg["url"] + "/Users/" + urllib.parse.quote(user["id"]) + "/Items/Resume?" + \
        urllib.parse.urlencode({
            "Limit": limit,
            "Recursive": "true",
            "MediaTypes": "Video",
            "Fields": "UserData,RunTimeTicks,SeriesName,ParentIndexNumber,IndexNumber",
            "EnableImages": "true",
        })
    data = _get_json(url, _jf_headers(cfg))
    out = []
    for item in ((data or {}).get("Items") or [])[:limit]:
        ticks = float(item.get("RunTimeTicks") or 0)
        udata = item.get("UserData") or {}
        pos_ticks = float(udata.get("PlaybackPositionTicks") or 0)
        duration = ticks / 10_000_000.0        # 100 ns ticks -> seconds
        position = pos_ticks / 10_000_000.0
        item_id = str(item.get("Id") or "")
        is_movie = (item.get("Type") == "Movie")
        out.append({
            "title": item.get("SeriesName") or item.get("Name") or "",
            "path": "",
            "file": "",
            "season": None if is_movie else item.get("ParentIndexNumber"),
            "episode": None if is_movie else item.get("IndexNumber"),
            "is_movie": is_movie,
            "position": round(position, 1),
            "duration": round(duration, 1),
            "percent": round((position / duration * 100) if duration > 0 else 0, 1),
            "remote": True,
            "server": "jellyfin",
            # Relative, and fetched through /api/mediaplayer/image: handing the
            # browser an absolute media-server URL both leaks the internal
            # address into every page and breaks for anyone who reaches
            # MediaForge from outside that network.
            "poster_path": ("/Items/" + urllib.parse.quote(item_id) +
                            "/Images/Primary?maxHeight=340") if item_id else "",
            "open_url": (cfg["url"] + "/web/index.html#!/details?id=" +
                         urllib.parse.quote(item_id)) if item_id else cfg["url"],
        })
    return out


def _plex_on_deck(cfg, user, limit) -> list:
    """Plex's per-user On Deck.

    Plex scopes On Deck to the *token*, not to a query parameter, so a managed
    user's list needs that user's own token. plex.tv hands one out for a home
    user the owner administers (``/api/v2/home/users/<id>/switch``); when that
    is not possible (shared-but-not-home users, or plex.tv unreachable) the
    owner's list is returned only if the linked user IS the owner -- showing
    the owner's On Deck to somebody else would be exactly the leak this whole
    module is careful about.
    """
    # Account id "1" is the server owner in every Plex build; that is the
    # identity the stored admin token already speaks for.
    token = cfg["token"] if user["id"] == "1" else _plex_user_token(cfg, user["id"])
    if not token:
        return []
    sub = dict(cfg, token=token)
    data = _get_json(_plex_url(sub, "/library/onDeck", **{"X-Plex-Container-Size": limit}))
    out = []
    for item in ((data or {}).get("MediaContainer", {}).get("Metadata") or [])[:limit]:
        duration = float(item.get("duration") or 0) / 1000.0
        position = float(item.get("viewOffset") or 0) / 1000.0
        is_movie = item.get("type") == "movie"
        key = str(item.get("key") or "")
        thumb = str(item.get("thumb") or item.get("grandparentThumb") or "")
        out.append({
            "title": item.get("grandparentTitle") or item.get("title") or "",
            "path": "",
            "file": "",
            "season": None if is_movie else item.get("parentIndex"),
            "episode": None if is_movie else item.get("index"),
            "is_movie": is_movie,
            "position": round(position, 1),
            "duration": round(duration, 1),
            "percent": round((position / duration * 100) if duration > 0 else 0, 1),
            "remote": True,
            "server": "plex",
            # The thumb is served by the PMS and needs the token, so it is
            # proxied by the caller rather than handed to the browser raw.
            "poster_path": thumb,
            "open_url": cfg["url"] + "/web/index.html#!/server/-/details?key=" +
                        urllib.parse.quote(key),
        })
    return out


def _plex_user_token(cfg, user_id) -> str:
    """A home user's own token, via plex.tv. Cached for the users TTL.

    Returns "" when the switch is refused -- that is the normal answer for a
    friend the owner does not administer, and it means "no On Deck for you",
    not an error.
    """
    def _fetch():
        url = ("https://plex.tv/api/v2/home/users/%s/switch"
               % urllib.parse.quote(str(user_id)))
        try:
            req = urllib.request.Request(url, data=b"", method="POST", headers={
                "Accept": "application/json",
                "X-Plex-Token": cfg["token"],
                "X-Plex-Client-Identifier": "mediaforge-downloader",
                "X-Plex-Product": "MediaForge",
            })
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read() or b"{}")
            return str(data.get("authToken") or "")
        except Exception as exc:
            logger.debug("[MediaPlayer] plex user switch failed: %s", type(exc).__name__)
            return ""

    return _cached("plextoken:%s" % user_id, _USERS_TTL, _fetch)


def image_bytes(path) -> tuple:
    """Fetch an artwork *path* from the configured server, with the token.

    Returns ``(bytes, content_type)`` or ``(None, None)``. Only server-relative
    paths are accepted: the value originates from the media server but reaches
    this function through a query string, so ``//evil.example/x`` (a
    protocol-relative URL a fetcher would follow off-site) and any absolute URL
    are refused rather than normalised.
    """
    cfg = config()
    path = str(path or "")
    if not cfg or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None, None
    try:
        if cfg["kind"] == "plex":
            base, _, query = path.partition("?")
            params = dict(urllib.parse.parse_qsl(query))
            url = _plex_url(cfg, base, **params)
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(cfg["url"] + path, headers=_jf_headers(cfg))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            if not ctype.startswith("image/"):
                return None, None
            return resp.read(4 * 1024 * 1024), ctype
    except Exception:
        return None, None


# ------------------------------------------------------------------ watch stats

def watch_stats(user_id, since_ts, until_ts=None) -> dict:
    """What *user_id* watched in the window, for the Wrapped card.

    Returns ``{"available": bool, "server": str, "plays": int, "seconds": int,
    "top_titles": [...], "top_genres": [...], "movies": int, "episodes": int}``.
    ``available`` is False when nothing could be read -- the card then falls
    back to MediaForge's own download numbers instead of showing zeroes and
    implying the user watched nothing.
    """
    cfg = config()
    user = resolve_user(user_id)
    if not cfg or not user:
        return {"available": False}
    since = int(since_ts or 0)
    until = int(until_ts or time.time())
    key = "stats:%s:%s:%d:%d" % (cfg["kind"], user["id"], since, until // 3600)
    return _cached(key, _DATA_TTL, lambda: (
        _jf_stats(cfg, user, since, until) if cfg["kind"] == "jellyfin"
        else _plex_stats(cfg, user, since, until)))


def _rank(counter, limit=5):
    return [{"name": name, "count": count}
            for name, count in sorted(counter.items(), key=lambda kv: -kv[1])[:limit]]


def _jf_stats(cfg, user, since, until) -> dict:
    """Jellyfin has no history API without the Playback Reporting plugin, so
    this reads *played items* and their LastPlayedDate instead. That loses
    rewatches (PlayCount is counted, but not when each play happened) and is
    said as much through ``approximate``."""
    url = cfg["url"] + "/Users/" + urllib.parse.quote(user["id"]) + "/Items?" + \
        urllib.parse.urlencode({
            "Recursive": "true",
            "IsPlayed": "true",
            "IncludeItemTypes": "Movie,Episode",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Limit": 500,
            "Fields": "UserData,RunTimeTicks,Genres,SeriesName",
        })
    data = _get_json(url, _jf_headers(cfg))
    if data is None:
        return {"available": False}
    titles, genres = {}, {}
    seconds = plays = movies = episodes = 0
    for item in (data.get("Items") or []):
        udata = item.get("UserData") or {}
        played_at = _parse_iso(udata.get("LastPlayedDate"))
        if not played_at or played_at < since or played_at > until:
            continue
        plays += 1
        seconds += int(float(item.get("RunTimeTicks") or 0) / 10_000_000.0)
        name = item.get("SeriesName") or item.get("Name") or ""
        if name:
            titles[name] = titles.get(name, 0) + 1
        for genre in (item.get("Genres") or []):
            genres[genre] = genres.get(genre, 0) + 1
        if item.get("Type") == "Movie":
            movies += 1
        else:
            episodes += 1
    return {"available": True, "server": "jellyfin", "approximate": True,
            "plays": plays, "seconds": seconds, "movies": movies,
            "episodes": episodes, "top_titles": _rank(titles),
            "top_genres": _rank(genres)}


def _plex_stats(cfg, user, since, until) -> dict:
    """Plex keeps a real history, so this is exact -- but it carries no genre,
    which is why ``top_genres`` comes back empty for Plex and the card hides
    that tile rather than inventing one."""
    data = _get_json(_plex_url(cfg, "/status/sessions/history/all", **{
        "accountID": user["id"],
        "viewedAt>": since,
        "sort": "viewedAt:desc",
        "X-Plex-Container-Size": 1000,
    }))
    if data is None:
        return {"available": False}
    titles = {}
    seconds = plays = movies = episodes = 0
    for item in ((data or {}).get("MediaContainer", {}).get("Metadata") or []):
        viewed = int(item.get("viewedAt") or 0)
        if viewed < since or viewed > until:
            continue
        plays += 1
        seconds += int(float(item.get("duration") or 0) / 1000.0)
        name = item.get("grandparentTitle") or item.get("title") or ""
        if name:
            titles[name] = titles.get(name, 0) + 1
        if item.get("type") == "movie":
            movies += 1
        else:
            episodes += 1
    return {"available": True, "server": "plex", "approximate": False,
            "plays": plays, "seconds": seconds, "movies": movies,
            "episodes": episodes, "top_titles": _rank(titles),
            "top_genres": []}


def _parse_iso(value) -> int:
    """Jellyfin's ISO timestamps -> epoch seconds, or 0."""
    text = str(value or "").strip()
    if not text:
        return 0
    text = text.replace("Z", "+00:00")
    # Jellyfin emits SEVEN fractional digits; datetime.fromisoformat accepts at
    # most six and raises on the rest, so the fraction is truncated here. The
    # timezone offset (if any) is kept -- dropping it would silently reinterpret
    # a UTC timestamp as local time, which is a whole hour of "watched in the
    # wrong month" at the edges.
    if "." in text:
        head, _, tail = text.partition(".")
        offset = ""
        for pos, char in enumerate(tail):
            if char in "+-":
                offset, tail = tail[pos:], tail[:pos]
                break
        text = "%s.%s%s" % (head, (tail or "0")[:6], offset)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return 0
