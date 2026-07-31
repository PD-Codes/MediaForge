"""ComicVine (comicvine.gamespot.com) enrichment for the comic library.

The comic library identifies a file from what is already on disk: ComicInfo.xml
inside the archive, and failing that the file name. That is the source of
truth and it never leaves the machine. This module is the OPTIONAL layer on
top: when the user has entered a ComicVine API key, a series/issue can be
looked up online to fill in the fields the local data simply does not contain
(publisher, cover art, plot, character list).

Three properties decide everything below:

* **It only ever adds.** :func:`enrich` returns the fields ComicVine knows and
  nothing else; merging is the caller's job and local data wins every conflict
  (see the contract on :func:`enrich`).
* **It is allowed to do nothing.** No key, integration off, no network, a
  broken response, the hourly budget spent -- every one of those is a normal
  state that returns ``{}`` quietly. Nothing here raises into a scan, and
  nothing here logs at ERROR level: an ERROR on the shared "mediaforge" logger
  is picked up by telemetry/hooks.py and turned into a crash report, and "the
  user has no ComicVine key" is not a crash.
* **It must not get the user's key banned.** ComicVine allows 200 requests per
  resource per hour and locks the key out on violation. A library scan is
  exactly the workload that blows through that (3000 issues = 3000 lookups),
  so the throttle below is a hard client-side cap, not a courtesy delay -- see
  :class:`_RateLimiter`.

Caching follows web/tmdb_cache.py: SQLite-backed via the shared
``provider_cache`` table (namespace ``comicvine``), a version prefix in every
key so a format change invalidates old rows instead of mis-reading them, and
**negative results are cached too**. That last part is not an optimisation
detail: a library full of obscure scanlations produces mostly misses, and
without a cached miss every scan would re-ask ComicVine the same unanswerable
question until the budget is gone.
"""

from __future__ import annotations

import html as _html
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .db import clear_provider_cache
from .db import get_provider_cache
from .db import get_setting
from .db import set_provider_cache
from .db import set_setting
from ..logger import get_logger

logger = get_logger(__name__)


API_BASE = "https://comicvine.gamespot.com/api"

# Settings keys. The API key is listed in db.SENSITIVE_KEYS, so set_setting()
# encrypts it and it is never handed back to the browser (routes/integrations.py
# only reports has_api_key).
SETTING_API_KEY = "comicvine_api_key"
SETTING_ENABLED = "comicvine_enabled"
# Throttle bookkeeping (see _RateLimiter). Plain settings rows rather than a
# table of their own: it is two short JSON values written at most a few hundred
# times an hour.
SETTING_RATE_WINDOW = "comicvine_rate_window"
SETTING_COOLDOWN = "comicvine_cooldown_until"

# Cache namespace + key version. Bump _CACHE_VERSION whenever the shape of a
# cached dict changes; old rows then simply never match again.
_CACHE_NS = "comicvine"
_CACHE_VERSION = "v1"

# 24 h. Comic metadata is close to immutable, so a longer TTL would be nice --
# but the shared evictor (db.evict_provider_cache(), called hourly from app.py)
# drops provider_cache rows older than 24 h, so anything above that would be a
# TTL the storage does not actually honour.
CACHE_TTL = 24 * 3600.0

# --- Throttle ---------------------------------------------------------------
# ComicVine documents "200 requests per resource per hour" and answers a
# violation with a lockout, so the cap is enforced per resource ("volumes",
# "issues") and left with headroom: a second MediaForge instance, a retry or a
# clock skew must not be what pushes the key over the documented line.
HOURLY_LIMIT = 180
RATE_WINDOW = 3600.0
# How long everything stops after ComicVine itself said "too many requests".
# Deliberately a full window: at that point the local counter and the server's
# disagree, and the only safe assumption is that the server is right.
COOLDOWN_SECONDS = 3600.0

_TIMEOUT = 12.0

# Result caps. Foreign data, so nothing is passed through unbounded.
MAX_SUMMARY_CHARS = 1500
MAX_CHARACTERS = 12

# Where a cover URL may point. The URL comes out of a third-party JSON
# response and is handed to the browser (and possibly fetched server-side), so
# it is validated instead of trusted -- same reasoning as the download-host
# allowlist in models/common/opensubtitles.py.
_ALLOWED_IMAGE_HOSTS = (
    "comicvine.gamespot.com",
    "gamespot.com",
    "cbsistatic.com",
    "comicvine.com",
)

# ComicVine status codes we care about (payload["status_code"]).
_CV_OK = 1
_CV_INVALID_KEY = 100
_CV_NOT_FOUND = 101
_CV_RATE_LIMIT = 107

_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Logging safety
# ---------------------------------------------------------------------------

_API_KEY_IN_TEXT = re.compile(r"(api_key=)[^&\s\"\']+", re.IGNORECASE)


def _redact(text: Any, api_key: str = "") -> str:
    """`text` with the API key removed, ready to be logged.

    Exception strings from an HTTP client routinely contain the full request
    URL, and for this API the key IS a query parameter -- so a plain
    ``logger.debug("... %s", exc)`` would write the user's key into the log
    file. Everything logged from this module goes through here.
    """
    out = str(text if text is not None else "")
    if api_key:
        out = out.replace(api_key, "***")
    out = _API_KEY_IN_TEXT.sub(r"\1***", out)
    return out[:400]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _setting(key: str, default: str = "") -> str:
    """Read a setting without ever raising (no DB yet, no app context, ...)."""
    try:
        value = get_setting(key, default)
    except Exception:
        return default
    return (str(value) if value is not None else default).strip()


def get_api_key() -> str:
    """The configured ComicVine API key, or "" when none is stored.

    Never log or return this to a client -- see routes/integrations.py, which
    reports only whether one exists.
    """
    return _setting(SETTING_API_KEY, "")


def is_configured() -> bool:
    """True when an API key is stored, i.e. a lookup could work at all."""
    return bool(get_api_key())


def is_enabled() -> bool:
    """Master switch AND key present. Off by default, on purpose: this contacts
    an external service the user has to sign up for."""
    return _setting(SETTING_ENABLED, "0") == "1" and is_configured()


def invalidate_cache() -> None:
    """Drop every cached ComicVine lookup (called after a key change)."""
    try:
        clear_provider_cache(_CACHE_NS)
    except Exception:
        logger.debug("[ComicVine] cache invalidation failed", exc_info=False)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Hard per-resource cap of `limit` requests per rolling `window` seconds.

    Not a token bucket like the TMDB one: TMDB's limit is "requests per second"
    and a caller that arrives too early can simply sleep 30 ms. ComicVine's is
    "requests per hour", where the equivalent sleep is up to an hour -- so this
    limiter never blocks. :meth:`acquire` answers yes or no, and a "no" means
    the caller returns "no data" for now. A scan that enriches 180 issues this
    hour and continues next hour is a slow scan; a scan that gets the key
    locked out is a broken installation.

    The timestamp window is mirrored into app_settings, so restarting the
    process (or the user hitting "restart" in Settings twice) does not hand out
    a fresh budget -- which is exactly how a key gets banned despite a limiter
    being present.
    """

    def __init__(self, limit: int = HOURLY_LIMIT, window: float = RATE_WINDOW):
        self._limit = int(limit)
        self._window = float(window)
        self._hits: Dict[str, List[float]] = {}
        self._loaded = False

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = _setting(SETTING_RATE_WINDOW, "")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        for resource, stamps in data.items():
            if not isinstance(resource, str) or not isinstance(stamps, list):
                continue
            kept = []
            for ts in stamps:
                if isinstance(ts, (int, float)) and now - float(ts) < self._window:
                    # A timestamp from the future (clock moved backwards) is
                    # clamped to now instead of dropped: dropping it would
                    # hand out free budget, which is the one direction that
                    # must never happen here.
                    kept.append(min(float(ts), now))
            if kept:
                self._hits[resource] = kept[-self._limit:]

    def _persist(self) -> None:
        try:
            set_setting(SETTING_RATE_WINDOW,
                        json.dumps({k: v for k, v in self._hits.items() if v}))
        except Exception:
            # A failed write only costs persistence across restarts; the
            # in-memory window still caps this process.
            logger.debug("[ComicVine] could not persist rate window")

    # -- API ----------------------------------------------------------------
    def acquire(self, resource: str) -> bool:
        """Consume one request for `resource`. False = budget spent, do not call."""
        now = time.time()
        with _state_lock:
            self._load()
            stamps = [ts for ts in self._hits.get(resource, [])
                      if now - ts < self._window]
            if len(stamps) >= self._limit:
                self._hits[resource] = stamps
                return False
            stamps.append(now)
            self._hits[resource] = stamps
            self._persist()
            return True

    def remaining(self, resource: str) -> int:
        now = time.time()
        with _state_lock:
            self._load()
            stamps = [ts for ts in self._hits.get(resource, [])
                      if now - ts < self._window]
            return max(0, self._limit - len(stamps))

    def reset_at(self, resource: str) -> float:
        """Unix time at which the oldest hit leaves the window (0 = nothing to wait for)."""
        now = time.time()
        with _state_lock:
            self._load()
            stamps = [ts for ts in self._hits.get(resource, [])
                      if now - ts < self._window]
            if len(stamps) < self._limit:
                return 0.0
            return min(stamps) + self._window

    def reset(self) -> None:
        """Forget the window (tests, and a manual "clear" if one is ever added)."""
        with _state_lock:
            self._hits = {}
            self._loaded = True
            self._persist()


_rate = _RateLimiter()


def _cooldown_left() -> float:
    """Seconds left of a server-imposed cooldown, 0.0 when none is active."""
    raw = _setting(SETTING_COOLDOWN, "")
    if not raw:
        return 0.0
    try:
        until = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - time.time())


def _start_cooldown() -> None:
    """ComicVine said "too many requests" -- stop asking for a full window."""
    try:
        set_setting(SETTING_COOLDOWN, str(time.time() + COOLDOWN_SECONDS))
    except Exception:
        logger.debug("[ComicVine] could not persist cooldown")
    logger.warning("[ComicVine] rate limit hit — pausing all requests for %d min",
                   int(COOLDOWN_SECONDS // 60))


def throttle_status() -> Dict[str, Any]:
    """What the settings UI shows about the budget. No secrets involved."""
    return {
        "limit": HOURLY_LIMIT,
        "remaining_volumes": _rate.remaining("volumes"),
        "remaining_issues": _rate.remaining("issues"),
        "cooldown_seconds": int(_cooldown_left()),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    """ComicVine blocks generic/default user agents outright."""
    try:
        from importlib.metadata import version
        return f"MediaForge/{version('mediaforge')} (comic library)"
    except Exception:
        return "MediaForge (comic library)"


def _session():
    """The project's shared, DoH-aware niquests session.

    Same indirection as devinfos_monitor.py: imported lazily so this module
    stays importable without the HTTP stack, and monkeypatchable in tests so
    nothing here ever touches the real API.
    """
    from ..config import GLOBAL_SESSION
    return GLOBAL_SESSION


def _api_get(resource: str, params: Dict[str, str], api_key: str):
    """GET ``/{resource}/`` and return ``(payload, error)``; exactly one is set.

    `error` is a short classifier ("no_api_key", "throttled", "cooldown",
    "unreachable", "http_error", "bad_response", "invalid_key", "not_found",
    "rate_limited"), never a message that could carry the key.
    """
    if not api_key:
        return None, "no_api_key"
    if _cooldown_left() > 0:
        return None, "cooldown"
    if not _rate.acquire(resource):
        logger.debug("[ComicVine] hourly budget for %s spent — skipping lookup", resource)
        return None, "throttled"

    query = dict(params or {})
    query["api_key"] = api_key
    query["format"] = "json"
    url = f"{API_BASE}/{resource}/"
    try:
        resp = _session().get(
            url, params=query, timeout=_TIMEOUT,
            headers={"User-Agent": _user_agent(), "Accept": "application/json"},
        )
    except Exception as exc:
        # No traceback and no raw message: the message is an HTTP client's and
        # would contain the request URL, key included. See _redact().
        logger.debug("[ComicVine] %s request failed: %s",
                     resource, _redact(f"{type(exc).__name__}: {exc}", api_key))
        return None, "unreachable"

    status = getattr(resp, "status_code", 0)
    if status in (420, 429):
        _start_cooldown()
        return None, "rate_limited"
    if status == 401 or status == 403:
        return None, "invalid_key"
    if status >= 400:
        return None, "http_error"

    try:
        payload = resp.json()
    except Exception:
        return None, "bad_response"
    if not isinstance(payload, dict):
        return None, "bad_response"

    code = payload.get("status_code")
    if code == _CV_RATE_LIMIT:
        _start_cooldown()
        return None, "rate_limited"
    if code == _CV_INVALID_KEY:
        return None, "invalid_key"
    if code == _CV_NOT_FOUND:
        return None, "not_found"
    if code != _CV_OK:
        logger.debug("[ComicVine] %s answered status_code=%r", resource, code)
        return None, "http_error"
    return payload, None


# ---------------------------------------------------------------------------
# Foreign-data helpers
# ---------------------------------------------------------------------------
# Everything below treats the response as untrusted input: a field may be
# missing, null, a number where a string is expected, or a list of nulls.

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text(value: Any, limit: int = 300) -> str:
    """A trimmed plain string, or "" for anything that is not usable text."""
    if isinstance(value, str):
        out = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out = str(value)
    else:
        return ""
    return out.strip()[:limit]


def _plain(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """ComicVine descriptions are HTML. Strip it down to plain text."""
    raw = value if isinstance(value, str) else ""
    if not raw:
        return ""
    out = _TAG_RE.sub(" ", raw)
    out = _html.unescape(out)
    out = _WS_RE.sub(" ", out).strip()
    return out[:limit]


def _year(value: Any) -> str:
    """A 4-digit year from a year field or an ISO-ish date, else ""."""
    text = _text(value, 32)
    match = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", text)
    return match.group(1) if match else ""


def _image_url(image: Any) -> str:
    """A usable, allow-listed cover URL from ComicVine's `image` object."""
    if not isinstance(image, dict):
        return ""
    for key in ("super_url", "medium_url", "original_url", "small_url", "thumb_url"):
        url = _text(image.get(key), 500)
        if not url:
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        host = parsed.hostname.lower().rstrip(".")
        if any(host == allowed or host.endswith("." + allowed)
               for allowed in _ALLOWED_IMAGE_HOSTS):
            return url
    return ""


def _names(entries: Any, limit: int = MAX_CHARACTERS) -> List[str]:
    """`[{"name": ...}, ...]` -> a bounded list of clean names."""
    if not isinstance(entries, list):
        return []
    out: List[str] = []
    for entry in entries:
        name = _text(entry.get("name"), 120) if isinstance(entry, dict) else ""
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _results(payload: Dict[str, Any]) -> List[dict]:
    """The `results` list of a ComicVine response, filtered to real dicts.

    A single-object endpoint answers with a dict instead of a list, so both
    shapes are accepted.
    """
    results = payload.get("results")
    if isinstance(results, dict):
        return [results]
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)]


def _norm_key(value: Any) -> str:
    """Normalised cache-key fragment (case/whitespace/punctuation-insensitive)."""
    text = _text(value, 160).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _norm_number(value: Any) -> str:
    """"007" and "7" are the same issue; "1.5" and "Annual 2" stay as typed."""
    text = _text(value, 24).strip()
    if not text:
        return ""
    match = re.fullmatch(r"0*(\d+)", text)
    return match.group(1) if match else text


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_get(key: str):
    try:
        return get_provider_cache(_CACHE_NS, key, ttl=CACHE_TTL)
    except Exception:
        return None


def _cache_put(key: str, data: dict) -> None:
    try:
        set_provider_cache(_CACHE_NS, key, data)
    except Exception:
        logger.debug("[ComicVine] could not write cache entry")


# A miss is stored exactly like a hit. Without this, a library of obscure
# releases would ask ComicVine the same unanswerable question on every scan
# and spend the whole hourly budget on questions that already have an answer.
_MISS = {"found": False}


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------

def search_volume(series: str, volume_year: Optional[str] = None, *,
                  api_key: Optional[str] = None) -> Optional[dict]:
    """Find the ComicVine *volume* (= series) for `series`, or None.

    `volume_year` is the start year when it is known (from ComicInfo.xml or a
    "(2011)" in the folder name); it only ever breaks ties, never filters the
    search, because a wrong year in the file name must not turn a hit into a
    miss.

    Returns ``{"found": True, "id", "name", "year", "publisher", "cover_url",
    "summary"}`` on a hit, None when there is nothing (or nothing may be
    fetched right now). Cached for 24 h, misses included.
    """
    name = _text(series, 160)
    key = api_key if api_key is not None else get_api_key()
    if not name or not key:
        return None

    cache_key = f"{_CACHE_VERSION}|volume|{_norm_key(name)}|{_year(volume_year)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached.get("found") else None

    payload, error = _api_get("volumes", {
        "filter": f"name:{name}",
        "limit": "20",
        "field_list": "id,name,start_year,publisher,image,deck,description",
    }, key)
    if error:
        # Only a definitive "there is no such volume" is worth remembering. A
        # throttled/offline lookup must stay uncached, or a temporary outage
        # would be frozen into a miss for the next 24 h.
        if error == "not_found":
            _cache_put(cache_key, _MISS)
        return None

    best = _pick_volume(_results(payload), name, _year(volume_year))
    if best is None:
        _cache_put(cache_key, _MISS)
        return None

    publisher = best.get("publisher")
    out = {
        "found": True,
        "id": best.get("id") if isinstance(best.get("id"), int) else None,
        "name": _text(best.get("name"), 200),
        "year": _year(best.get("start_year")),
        "publisher": _text(publisher.get("name"), 120) if isinstance(publisher, dict) else "",
        "cover_url": _image_url(best.get("image")),
        "summary": _plain(best.get("deck") or best.get("description")),
    }
    if out["id"] is None:
        # Without an id the volume is useless downstream (issues are filtered
        # by it), so this counts as a miss rather than a half-filled hit.
        _cache_put(cache_key, _MISS)
        return None
    _cache_put(cache_key, out)
    return out


def _pick_volume(results: List[dict], wanted_name: str, wanted_year: str):
    """The best volume among `results`: exact name first, then the year."""
    if not results:
        return None
    target = _norm_key(wanted_name)

    def score(entry: dict) -> tuple:
        name = _norm_key(entry.get("name"))
        exact = 1 if name == target else 0
        starts = 1 if name.startswith(target) or target.startswith(name) else 0
        year_hit = 1 if wanted_year and _year(entry.get("start_year")) == wanted_year else 0
        return (exact + year_hit * 2, exact, starts, year_hit)

    best = max(results, key=score)
    # Nothing even resembling the requested name is a miss, not a "closest
    # match" -- a wrong series is worse than no series.
    name = _norm_key(best.get("name"))
    if not (name == target or name.startswith(target) or target.startswith(name)):
        return None
    return best


def search_issue(volume_id: int, issue_number: Any, *,
                 api_key: Optional[str] = None) -> Optional[dict]:
    """Find one issue of `volume_id` by its number, or None.

    Returns ``{"found": True, "id", "story_title", "number", "year",
    "cover_url", "summary", "characters"}``. Cached for 24 h, misses included.
    """
    key = api_key if api_key is not None else get_api_key()
    try:
        vol = int(volume_id)
    except (TypeError, ValueError):
        return None
    number = _norm_number(issue_number)
    if not key or vol <= 0 or not number:
        return None

    cache_key = f"{_CACHE_VERSION}|issue|{vol}|{_norm_key(number)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached.get("found") else None

    payload, error = _api_get("issues", {
        "filter": f"volume:{vol},issue_number:{number}",
        "limit": "5",
        "field_list": ("id,name,issue_number,cover_date,store_date,deck,"
                       "description,image,character_credits"),
    }, key)
    if error:
        if error == "not_found":
            _cache_put(cache_key, _MISS)
        return None

    results = _results(payload)
    if not results:
        _cache_put(cache_key, _MISS)
        return None
    hit = results[0]
    for entry in results:
        if _norm_number(entry.get("issue_number")) == number:
            hit = entry
            break

    out = {
        "found": True,
        "id": hit.get("id") if isinstance(hit.get("id"), int) else None,
        "story_title": _text(hit.get("name"), 200),
        "number": _text(hit.get("issue_number"), 24),
        "year": _year(hit.get("cover_date") or hit.get("store_date")),
        "cover_url": _image_url(hit.get("image")),
        "summary": _plain(hit.get("deck") or hit.get("description")),
        "characters": _names(hit.get("character_credits")),
    }
    _cache_put(cache_key, out)
    return out


def test_connection(api_key: str = "") -> Dict[str, Any]:
    """Validate a key for the settings page's "test connection" button.

    Falls back to the stored key so the user can re-test without retyping the
    one they already saved. Returns a plain dict; the key is never echoed back
    and never reaches a log.

    Used by: web/routes/integrations.py.
    """
    key = _text(api_key, 200) or get_api_key()
    if not key:
        return {"ok": False, "error": "no_api_key"}
    cooldown = _cooldown_left()
    if cooldown > 0:
        return {"ok": False, "error": "cooldown", "retry_in": int(cooldown)}

    # One cheap, deterministic request. It costs one unit of the hourly budget
    # like any other call -- the test button is not exempt from the throttle,
    # because a user clicking it repeatedly is exactly how a key gets banned.
    payload, error = _api_get("volumes", {
        "filter": "name:Batman",
        "limit": "1",
        "field_list": "id,name",
    }, key)
    if error:
        return {"ok": False, "error": error}
    total = payload.get("number_of_total_results")
    return {
        "ok": True,
        "results": int(total) if isinstance(total, int) else 0,
        "remaining": _rate.remaining("volumes"),
        "limit": HOURLY_LIMIT,
    }


# ---------------------------------------------------------------------------
# The scanner's entry point
# ---------------------------------------------------------------------------

def enrich(series: str, number: Any, volume_year: Optional[str] = None) -> Dict[str, Any]:
    """Fields ComicVine can ADD for one issue, or ``{}``.

    THIS ONLY SUPPLEMENTS. It never overwrites anything. ComicInfo.xml and the
    file name are the authority for every field they contain -- they describe
    the file the user actually has, while this describes what a website thinks
    that file is. The caller (web/comics/scanner.py) therefore merges with the
    local value winning every conflict, e.g.::

        for field, value in enrich(series, number, year).items():
            local.setdefault(field, value)      # never local[field] = value

    Possible keys (only present when actually known): ``publisher``,
    ``summary``, ``cover_url``, ``year``, ``story_title``, ``characters``.

    Returns ``{}`` -- immediately, without blocking and without raising -- when
    the integration is off, no key is stored, the hourly budget is spent, the
    machine is offline, or the API answers with anything unexpected. A scan
    must run to completion on a machine with no internet at all, so "no
    enrichment" is a normal outcome and never an error.
    """
    try:
        if not is_enabled():
            return {}
        key = get_api_key()
        if not key:
            return {}

        volume = search_volume(series, volume_year, api_key=key)
        if not volume:
            return {}

        out: Dict[str, Any] = {}
        if volume.get("publisher"):
            out["publisher"] = volume["publisher"]
        if volume.get("year"):
            out["year"] = volume["year"]
        # Series-level text/art is the fallback; the issue's own overrides it
        # below when the issue was found.
        if volume.get("summary"):
            out["summary"] = volume["summary"]
        if volume.get("cover_url"):
            out["cover_url"] = volume["cover_url"]

        issue = search_issue(volume.get("id"), number, api_key=key) if number else None
        if issue:
            if issue.get("story_title"):
                out["story_title"] = issue["story_title"]
            if issue.get("summary"):
                out["summary"] = issue["summary"]
            if issue.get("cover_url"):
                out["cover_url"] = issue["cover_url"]
            if issue.get("year"):
                out["year"] = issue["year"]
            if issue.get("characters"):
                out["characters"] = issue["characters"]
        return out
    except Exception as exc:
        # A scan of a few thousand files must not die on one odd response.
        # DEBUG, not ERROR: telemetry/hooks.py turns ERROR records into crash
        # reports, and a failed optional lookup is not a crash.
        logger.debug("[ComicVine] enrich failed: %s",
                     _redact(f"{type(exc).__name__}: {exc}", get_api_key()))
        return {}
