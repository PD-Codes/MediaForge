"""Scraping helpers for megakino.to (React SPA backed by a JSON API).

megakino.to is NOT a server-rendered DLE site — it exposes a clean JSON API:

    /data/browse/?lang=2&keyword=&type=<movie|tvseries|>&order_by=<releases|trending|views|rating>&page=1&limit=N
        -> {"pager": {...}, "movies": [ {_id,title,year,rating,genres,poster_path,
                                         poster_path_season?, last_updated_epi?}, ... ]}

    /data/watch/?_id=<hexid>
        -> { _id, tv (0/1), title, slug, year, runtime, rating, storyline, genres,
             directors, cast, imdb_id, overview, poster_path, poster_path_season?,
             s (season no.), totalSeasons, totalEpisodes,
             streams: [ {_id, stream (hoster embed url), e? (episode no.),
                         release, url, source}, ... ] }

Posters are TMDB paths (image.tmdb.org). Streams carry direct hoster embed URLs
(VOE, Vidara, Firestream, Vidsonic, …); we use VOE by default.
"""
import json
import re
import threading
from html import unescape
from urllib.parse import urlencode

try:
    from ...config import MEGAKINO_BASE_URL, logger, GLOBAL_SESSION
except ImportError:  # pragma: no cover
    from mediaforge.config import MEGAKINO_BASE_URL, logger, GLOBAL_SESSION

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

# German content language id used by the site's API.
LANG_DE = "2"

# TMDB image base (posters/backdrops are TMDB paths).
_TMDB_IMG = "https://image.tmdb.org/t/p/w342"

# Map an embed-domain substring to the canonical extractor/provider name.
_HOSTER_DOMAINS = [
    ("voe", "VOE"),
    ("vidmoly", "Vidmoly"),
    ("vidoza", "Vidoza"),
    ("vidavaca", "Vidavaca"),
    ("vidara", "Vidara"),
    ("vidaar", "Vidara"),  # vidaarax.com/.net and other Vidara-family mirrors
    ("veev", "VeeV"),
    ("filemoon", "Filemoon"),
    ("dood", "Doodstream"),
    ("streamtape", "Streamtape"),
    ("luluvdo", "Luluvdo"),
    ("loadx", "LoadX"),
    ("firestream", "Firestream"),
    ("vidsonic", "Vidsonic"),
    ("upbolt", "Upbolt"),
]

_HEX24 = re.compile(r"[a-f0-9]{24}", re.IGNORECASE)


def base_url():
    return MEGAKINO_BASE_URL.rstrip("/")


def _text(value, _depth=0):
    """Coerce an API field that is DOCUMENTED as text but may arrive as a list.

    megakino.to's /data/* endpoints are not schema-stable: fields the module
    docstring above lists as plain strings (title, genres, storyline, overview,
    poster_path) come back as a JSON array for some entries. Every re.* and
    html.unescape() call in this module expects str, so one such entry raised
    "TypeError: expected string or bytes-like object, got 'list'" out of
    parse_meta() -- and because MegakinoMovie.title_de reads the parsed meta
    lazily, that TypeError surfaced inside the /api/series route and took down
    the whole response for a single malformed record.

    Lists are joined rather than dropped so the real content survives: a
    ["Action", "Drama"] genres field then splits back into two genres on the
    "[/,]" separator in parse_meta(). Anything else is stringified, and
    None/empty becomes "" so the existing `or ""` idioms keep working.
    """
    if isinstance(value, (list, tuple, set)):
        # Depth-bounded on purpose. This payload arrives over the network from a
        # third party, and json.loads() happily accepts nesting right up to the
        # interpreter's recursion limit -- an unbounded walk here would turn a
        # broken (or hostile) response into a RecursionError instead of a
        # harmless empty string. No field on this API is legitimately nested
        # deeper than one level, so two is already generous.
        if _depth >= 2:
            return ""
        return ", ".join(t for t in (_text(v, _depth + 1).strip() for v in value) if t)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def poster_url(path):
    """TMDB poster path -> absolute image URL (served via the app image proxy)."""
    path = _text(path)
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return _TMDB_IMG + (path if path.startswith("/") else "/" + path)


def slugify(title):
    s = unescape(_text(title)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "title"


def content_url(item):
    """Build a stable /watch/<slug>/<id> URL from a browse/watch item.

    Only the trailing hex ``_id`` is significant (the API keys off it); the slug
    is cosmetic.
    """
    _id = str(item.get("_id") or "")
    return f"{base_url()}/watch/{slugify(item.get('title'))}/{_id}"


def extract_id(url):
    """Pull the 24-hex object id out of a /watch/<slug>/<id>[?episode=N] URL."""
    if not url:
        return None
    ids = _HEX24.findall(url.split("?")[0])
    return ids[-1] if ids else None


def is_series_item(item):
    """A browse item is a series if it carries season/episode markers."""
    return ("last_updated_epi" in item) or ("poster_path_season" in item) or str(item.get("tv")) == "1"


def normalize_hoster_url(url):
    """VOE embeds arrive as ``voe.sx/<id>``; the extractor expects ``voe.sx/e/<id>``."""
    if not url:
        return url
    low = url.lower()
    if "voe" in low and "/e/" not in url:
        m = re.match(r"^(https?://[^/]+)/([A-Za-z0-9]+)(.*)$", url)
        if m:
            return f"{m.group(1)}/e/{m.group(2)}{m.group(3)}"
    return url


def classify_hoster(url):
    if not url:
        return None
    low = url.lower()
    for needle, name in _HOSTER_DOMAINS:
        if needle in low:
            return name
    return None


# ---------------------------------------------------------------------------
# HTTP (plain JSON API, no token gate)
# ---------------------------------------------------------------------------
_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                import requests as _req
                s = _req.Session()
                s.headers.update({
                    "User-Agent": _UA,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                    "Referer": base_url() + "/",
                    "X-Requested-With": "XMLHttpRequest",
                })
                _session = s
    return _session


def reset_session():
    global _session
    with _session_lock:
        _session = None


_MK_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}


def _doh_get(url, headers, timeout):
    return GLOBAL_SESSION.get(url, headers=headers, timeout=timeout)


def _plain_get(url, headers, timeout):
    return _get_session().get(url, headers=headers, timeout=timeout)


class MegakinoUnavailable(Exception):
    """megakino.to did not hand back a usable JSON payload.

    Covers every way this can happen, because to a caller they are the same
    thing: the host is unreachable, the request timed out, a Cloudflare
    challenge page came back instead of JSON, or -- as seen in the wild -- the
    connection dropped mid-body and left a truncated document that raises
    JSONDecodeError("Unterminated string").

    It exists as its own type so the web routes can tell "the source site had a
    bad day" apart from "MediaForge is broken" without matching on exception
    CLASS NAMES, which is what routes/search.py did before: it looked for the
    substrings "connection"/"timeout"/"protocol"/"ssl" and therefore missed
    JSONDecodeError, ChunkedEncodingError and IncompleteRead entirely. Those
    fell through to the generic handler, which answers 500 and logs at ERROR --
    and an ERROR log is exactly what telemetry/hooks.py turns into a crash
    report. Every hiccup on megakino.to's side was being filed as a defect in
    this app. See routes/browse.py's "upstream failures answer 502" note for
    the convention this follows.
    """


def _api_get_json(path, params=None, timeout=15):
    """Fetch a megakino JSON endpoint, trying the DoH session first (bypasses
    ISP DNS blocks) and the plain requests session as a fallback. The body is
    validated to actually be JSON — a Cloudflare/HTML interstitial from either
    transport is skipped rather than crashing the JSON parser.

    Raises MegakinoUnavailable when neither transport produced usable JSON.
    """
    url = base_url() + path
    if params:
        url += "?" + urlencode(params)
    headers = dict(_MK_HEADERS, Referer=base_url() + "/")
    reason = None
    for _get in (_doh_get, _plain_get):
        try:
            resp = _get(url, headers, timeout)
            resp.raise_for_status()
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            continue
        body = (getattr(resp, "text", "") or "").lstrip()
        if body[:1] in ("{", "["):
            try:
                # Parsed from the text we already decoded above rather than via
                # resp.json(), which would decode and scan the whole body a
                # second time. Same result, one pass less per API call.
                return json.loads(body)
            except ValueError as e:
                # A body that starts with "{" but does not parse means a
                # truncated/corrupted response, not a challenge page. Record the
                # size, never the content: it can carry the requested URL.
                reason = f"malformed JSON after {len(body)} bytes ({e})"
                continue
        reason = "non-JSON response (possible block/challenge page)"
    raise MegakinoUnavailable(f"megakino.to returned no usable response ({reason or 'unknown'})")


# ---------------------------------------------------------------------------
# Browse / search
# ---------------------------------------------------------------------------
def _browse(type_="", order_by="releases", keyword="", page=1, limit=24):
    params = {
        "lang": LANG_DE, "keyword": keyword, "year": "", "rating": "",
        "votes": "", "genre": "", "country": "", "cast": "", "directors": "",
        "type": type_, "order_by": order_by, "page": page, "limit": limit,
    }
    try:
        data = _api_get_json("/data/browse/", params)
    except Exception as e:
        logger.debug("Megakino browse failed (type=%s order=%s kw=%r): %s", type_, order_by, keyword, e)
        return None
    items = data.get("movies") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [_card(it) for it in items]


def _card(item):
    poster = item.get("poster_path") or item.get("poster_path_season") or ""
    series = is_series_item(item)
    title = unescape(_text(item.get("title")))
    return {
        "title": title,
        "url": content_url(item),
        "poster_url": poster_url(poster),
        "genre": _text(item.get("genres")).strip(),
        "rating": str(item.get("rating") or ""),
        "year": str(item.get("year") or ""),
        "is_series": series,
    }


def search(keyword):
    """Search across movies and series."""
    return _browse(type_="", order_by="", keyword=keyword, limit=30) or []


def fetch_new_movies():
    return _browse(type_="movies", order_by="releases", limit=24)


def fetch_popular_movies():
    return _browse(type_="movies", order_by="trending", limit=24)


def fetch_new_series():
    return _browse(type_="tvseries", order_by="releases", limit=24)


def fetch_popular_series():
    return _browse(type_="tvseries", order_by="trending", limit=24)


# ---------------------------------------------------------------------------
# Watch detail
# ---------------------------------------------------------------------------
def fetch_watch(url_or_id):
    """Return the /data/watch payload for a /watch URL or a raw id."""
    _id = url_or_id if _HEX24.fullmatch(url_or_id or "") else extract_id(url_or_id)
    if not _id:
        raise ValueError(f"Cannot extract MegaKino id from: {url_or_id}")
    return _api_get_json("/data/watch/", {"_id": _id})


def parse_meta(data):
    """Shared metadata from a /data/watch payload (movie or season)."""
    genres = [g.strip() for g in re.split(r"[/,]", _text(data.get("genres"))) if g.strip()]
    year = ""
    ym = re.search(r"\b(19|20)\d{2}\b", _text(data.get("year")))
    if ym:
        year = ym.group(0)
    poster = data.get("poster_path") or data.get("poster_path_season") or ""
    return {
        "title": unescape(_text(data.get("title"))),
        "year": year,
        "genres": genres,
        "description": unescape(_text(data.get("storyline") or data.get("overview"))),
        "poster_url": poster_url(poster),
        "imdb_id": _text(data.get("imdb_id")) or None,
        "rating": _text(data.get("rating") or ""),
        "tv": _text(data.get("tv")) or "0",
    }


def strip_season_suffix(title):
    if not title:
        return title
    t = re.sub(r"\s*[-–]\s*\d+\.?\s*Staffel\b.*$", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*[-–]\s*Staffel\s*\d+\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*Staffel\s*\d+\b.*$", "", t, flags=re.IGNORECASE)
    return t.strip(" -–") or title.strip()


def season_number(data):
    for key in ("s", "season", "season_number"):
        v = data.get(key)
        if v not in (None, "", "0"):
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    m = re.search(r"Staffel\s*(\d+)", _text(data.get("title")), re.IGNORECASE)
    return int(m.group(1)) if m else 1


def movie_hosters(data):
    """{provider_name: embed_url} for a movie payload (streams without episode)."""
    hosters = {}
    for st in (data.get("streams") or []):
        url = normalize_hoster_url(st.get("stream") or "")
        name = classify_hoster(url)
        if name and name not in hosters:
            hosters[name] = url
    return hosters


def episode_numbers(data):
    nums = set()
    for st in (data.get("streams") or []):
        e = st.get("e")
        if e is not None:
            try:
                nums.add(int(e))
            except (TypeError, ValueError):
                pass
    return sorted(nums)


def episode_hosters(data, episode_number):
    """{provider_name: embed_url} for one episode of a season payload."""
    hosters = {}
    for st in (data.get("streams") or []):
        try:
            if int(st.get("e")) != int(episode_number):
                continue
        except (TypeError, ValueError):
            continue
        url = normalize_hoster_url(st.get("stream") or "")
        name = classify_hoster(url)
        if name and name not in hosters:
            hosters[name] = url
    return hosters
