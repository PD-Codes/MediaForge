"""Crunchyroll integration service.

Thin host-side wrapper around the vendored ``crunchyroll_api`` single-file
library. Responsibilities:

* Build a :class:`CrunchyrollClient` from the CineInfo settings stored in the
  app database (credentials are kept encrypted at rest, see
  :data:`mediaforge.web.db.SENSITIVE_KEYS`).
* Cache the logged-in client process-wide (thread-safe, lazy, with a re-login
  TTL) so we never log in once per request.
* Persist the refresh-token across restarts in an **encrypted** session file
  (:class:`EncryptedFileSessionStore`) so we don't re-authenticate every boot.
* Provide small, defensive helpers the web layer consumes:
    - :func:`test_connection`        – validate credentials from the settings UI
    - :func:`is_available`           – "is this title on Crunchyroll?" (cached),
                                       used to add a provider pill even for fresh
                                       simulcasts TMDB doesn't list yet
    - :func:`get_simulcast_titles` / :func:`get_watchlist_titles` – Crunchyroll
                                       title lists the calendar resolves to dates
                                       via TMDB (CR has no future air dates)

Every public function is failure-tolerant: a missing dependency, a login error
or a Cloudflare block degrades to "no data" rather than raising into the request
handler. The vendored library only needs ``requests`` (already a project dep);
``cryptography`` (also a project dep) is used for the encrypted session store.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date as _date, datetime
from typing import Any, Dict, List, Optional

from ..config import MEDIAFORGE_CONFIG_DIR
from .db import (
    get_setting,
    set_setting,
    get_provider_cache,
    set_provider_cache,
    clear_provider_cache,
)

logger = logging.getLogger(__name__)

# Vendored library — imported lazily-safe so the rest of the app keeps working
# even if the single-file bundle is missing or fails to import.
try:  # pragma: no cover - import guard
    from ..vendor.crunchyroll_api import (  # type: ignore
        CrunchyrollClient,
        Config,
        EncryptedFileSessionStore,
        generate_key,
        CrunchyrollError,
        LoginError,
        CloudflareBlockError,
    )
    _IMPORT_OK = True
    _IMPORT_ERR: Optional[str] = None
except Exception as _exc:  # pragma: no cover - import guard
    CrunchyrollClient = None  # type: ignore
    Config = None  # type: ignore
    EncryptedFileSessionStore = None  # type: ignore
    generate_key = None  # type: ignore
    CrunchyrollError = Exception  # type: ignore
    LoginError = Exception  # type: ignore
    CloudflareBlockError = Exception  # type: ignore
    _IMPORT_OK = False
    _IMPORT_ERR = str(_exc)

# ── Session / client cache ────────────────────────────────────────────────────
_SESSION_FILE = MEDIAFORGE_CONFIG_DIR / ".crunchyroll.session.enc"
_RELOGIN_TTL = 6 * 3600          # re-login at most every 6 h
_MAX_SERIES = 80                 # cap simulcast series processed per refresh
_AVAIL_TTL = 24 * 3600           # cache "on Crunchyroll?" answers for 24 h

_client_lock = threading.Lock()
_client: Any = None
_client_signature: Optional[str] = None
_client_logged_in_at: float = 0.0

# "on Crunchyroll?" lookups are cached persistently (SQLite, same mechanism as
# the TMDB cache) so a restart doesn't lose 24h of work and negative results
# don't need to be re-fetched on every process start.
_PROVIDER_CACHE_NS = "crunchyroll_avail"

# ── Settings helpers ──────────────────────────────────────────────────────────
def _s(key: str, default: str = "") -> str:
    return (get_setting("crunchyroll_" + key, default) or "").strip()


def is_enabled() -> bool:
    """Master switch: Crunchyroll integration active and importable."""
    return _IMPORT_OK and _s("enabled", "0") == "1"


def has_account() -> bool:
    """True when a real (non-anonymous) Crunchyroll login is configured.

    Used so personal-account features (watchlist / custom lists in the calendar)
    can work even when the master display toggle (:func:`is_enabled`) is off, as
    long as email + password are stored. Anonymous mode has no watchlist.
    """
    return _IMPORT_OK and bool(_s("email")) and bool(_s("password"))


def _credentials_signature() -> str:
    """Fingerprint of the inputs that require a fresh client when they change."""
    return "|".join([
        _s("anon", "0"),
        _s("email"),
        _s("password"),
        _s("locale", "de-DE"),
        _s("profile_id"),
    ])


def _get_session_key() -> Optional[str]:
    """Return (creating once) the key used to encrypt the session-token cache."""
    if generate_key is None:
        return None
    key = get_setting("crunchyroll_session_key", "") or ""
    if not key:
        try:
            key = generate_key()
            set_setting("crunchyroll_session_key", key)
        except Exception:
            logger.warning("[Crunchyroll] could not create session key", exc_info=True)
            return None
    return key


# curl_cffi impersonation target. Plain ``requests`` exposes a Python/OpenSSL
# TLS fingerprint (JA3/JA4) that Cloudflare blocks on Windows builds even though
# the User-Agent claims Chrome. curl_cffi replays a real Chrome TLS handshake so
# the fingerprint matches the claimed browser. Mirrors mediaforge.config which
# already uses impersonate="chrome120" for the hosters.
_IMPERSONATE = "chrome120"


def _make_http_session() -> Any:
    """Return a curl_cffi session that impersonates Chrome, or ``None``.

    Falling back to ``None`` lets the vendored client create its own plain
    ``requests`` session (the previous behaviour) when curl_cffi is unavailable.
    """
    try:
        from curl_cffi import requests as _curl_requests  # type: ignore
        return _curl_requests.Session(impersonate=_IMPERSONATE)
    except Exception:
        return None


def _build_client(email: str, password: str, locale: str, anon: bool) -> Any:
    """Construct (but do not log in) a fresh client from explicit values."""
    cfg = Config(
        email=email or None,
        password=password or None,
        locale=locale or "de-DE",
        max_retries=3,
        backoff_factor=0.5,
        cache_ttl=300.0,
    )
    store = None
    key = _get_session_key()
    if key and EncryptedFileSessionStore is not None:
        try:
            store = EncryptedFileSessionStore(str(_SESSION_FILE), key)
        except Exception:
            store = None
    # Impersonate Chrome's TLS fingerprint to avoid Cloudflare bot blocks.
    http = _make_http_session()
    kwargs: Dict[str, Any] = {}
    if store is not None:
        kwargs["session_store"] = store
    if http is not None:
        kwargs["http_session"] = http
    return CrunchyrollClient(cfg, **kwargs)


def get_client() -> Any:
    """Return a cached, logged-in client, or ``None`` if unavailable.

    Thread-safe. Rebuilds the client when the stored credentials change and
    refreshes the login after :data:`_RELOGIN_TTL`. Anonymous mode is used when
    no email/password is configured or the ``anon`` toggle is set.
    """
    global _client, _client_signature, _client_logged_in_at
    if not (is_enabled() or has_account()):
        return None

    sig = _credentials_signature()
    now = time.time()
    with _client_lock:
        fresh = (
            _client is not None
            and _client_signature == sig
            and (now - _client_logged_in_at) < _RELOGIN_TTL
        )
        if fresh:
            return _client

        email = _s("email")
        password = _s("password")
        locale = _s("locale", "de-DE")
        anon = _s("anon", "0") == "1" or not (email and password)
        try:
            client = _build_client(email, password, locale, anon)
            if anon:
                client.login_anonymous()
            else:
                client.login()
                pid = _s("profile_id")
                if pid:
                    try:
                        client.switch_profile(pid)
                    except Exception:
                        logger.warning("[Crunchyroll] could not switch to profile %s", pid)
            _client = client
            _client_signature = sig
            _client_logged_in_at = now
            logger.info("[Crunchyroll] logged in (%s)", "anonymous" if anon else email)
            return _client
        except LoginError:
            logger.warning("[Crunchyroll] login failed — check credentials")
        except CloudflareBlockError:
            logger.warning("[Crunchyroll] blocked by Cloudflare")
        except Exception:
            logger.warning("[Crunchyroll] could not create client", exc_info=True)
        _client = None
        _client_signature = None
        return None


def invalidate_client() -> None:
    """Drop the cached client (call after credential/setting changes)."""
    global _client, _client_signature, _client_logged_in_at
    with _client_lock:
        _client = None
        _client_signature = None
        _client_logged_in_at = 0.0
    clear_provider_cache(_PROVIDER_CACHE_NS)
    _titles_cache.clear()


def invalidate_availability_cache() -> None:
    """Drop only the cached "on Crunchyroll?" pill results.

    Lighter-weight than :func:`invalidate_client` — used by the manual
    "clear cache" button so it doesn't also force a fresh login.
    """
    clear_provider_cache(_PROVIDER_CACHE_NS)


def list_account_profiles() -> List[Dict[str, Any]]:
    """Return the account's profiles (id, name, is_primary) for the UI selector."""
    client = get_client()
    if client is None:
        return []
    try:
        return [
            {"id": getattr(p, "profile_id", ""),
             "name": getattr(p, "profile_name", "") or "",
             "is_primary": bool(getattr(p, "is_primary", False))}
            for p in client.list_profiles()
        ]
    except Exception as exc:
        logger.debug("[Crunchyroll] list_account_profiles failed: %s", exc)
        return []


# ── Connection test (settings UI) ─────────────────────────────────────────────
def test_connection(email: str, password: str, locale: str, anon: bool,
                    profile_id: str = "") -> Dict[str, Any]:
    """Validate the given credentials without touching the cached client.

    Returns ``{ok: bool, ...}``. On success includes ``mode`` and, for an
    account login, the primary ``profile`` name and whether ``premium`` features
    look available.
    """
    if not _IMPORT_OK:
        return {"ok": False, "error": "library_unavailable", "detail": _IMPORT_ERR or ""}
    if not anon and not (email and password):
        return {"ok": False, "error": "missing_credentials"}
    try:
        cfg = Config(email=email or None, password=password or None,
                     locale=locale or "de-DE", max_retries=2)
        # Impersonate Chrome's TLS fingerprint to avoid Cloudflare bot blocks.
        http = _make_http_session()
        client = (CrunchyrollClient(cfg, http_session=http)
                  if http is not None else CrunchyrollClient(cfg))
        try:
            if anon:
                client.login_anonymous()
                return {"ok": True, "mode": "anonymous"}
            client.login()
            out: Dict[str, Any] = {"ok": True, "mode": "account"}
            profiles = []
            try:
                profiles = client.list_profiles()
                # Expose every profile so the UI can offer a selector.
                out["profiles"] = [
                    {"id": getattr(pr, "profile_id", ""),
                     "name": getattr(pr, "profile_name", "") or "",
                     "is_primary": bool(getattr(pr, "is_primary", False))}
                    for pr in profiles
                ]
            except Exception:
                pass
            # Switch to the requested profile (or fall back to primary/first) so
            # the reported name matches what the calendar/watchlist will use.
            chosen = None
            if profile_id:
                chosen = next((pr for pr in profiles
                               if getattr(pr, "profile_id", "") == profile_id), None)
                if chosen is not None:
                    try:
                        client.switch_profile(profile_id)
                    except Exception:
                        pass
            if chosen is None:
                chosen = next((pr for pr in profiles if getattr(pr, "is_primary", False)), None)
                if chosen is None and profiles:
                    chosen = profiles[0]
            if chosen is not None:
                out["profile"] = getattr(chosen, "profile_name", "") or ""
                out["profile_id"] = getattr(chosen, "profile_id", "")
            try:
                # A successful watchlist call implies an active (premium) account.
                client.get_watchlist(limit=1)
                out["premium"] = True
            except Exception:
                out["premium"] = False
            return out
        finally:
            try:
                client.http.close()
            except Exception:
                pass
    except LoginError:
        return {"ok": False, "error": "login_failed"}
    except CloudflareBlockError:
        return {"ok": False, "error": "cloudflare"}
    except Exception as exc:
        logger.debug("[Crunchyroll] test_connection error: %s", exc)
        return {"ok": False, "error": "unknown", "detail": str(exc)}


# ── Provider availability (pills) ─────────────────────────────────────────────
def is_available(title: str) -> bool:
    """True if a series matching ``title`` exists in the Crunchyroll catalog.

    Cached persistently (SQLite) for :data:`_AVAIL_TTL`, same mechanism as the
    TMDB cache — survives restarts. Used to add a "Crunchyroll" provider pill
    in the detail modal — especially for new simulcasts TMDB doesn't list yet.
    """
    title = (title or "").strip()
    if not title or not is_enabled():
        return False
    key = title.lower()
    cached = get_provider_cache(_PROVIDER_CACHE_NS, key, _AVAIL_TTL)
    if cached is not None:
        return bool(cached.get("found"))

    client = get_client()
    if client is None:
        return False
    found = False
    try:
        # The client is bound to the account locale (crunchyroll_locale), so the
        # search catalog is already region-specific — a confident title match
        # therefore means the title is on Crunchyroll for that region. We match
        # strictly (exact or strong containment) to avoid false-positive pills.
        results = client.search_series(title, limit=8) or []
        norm = _norm(title)
        for s in results:
            # Compare against the result's title and slug so localized German /
            # English / romaji variants still line up.
            candidates = [
                _norm(getattr(s, "title", "") or ""),
                _norm((getattr(s, "slug", "") or "").replace("-", " ")),
            ]
            for st in candidates:
                if not st:
                    continue
                if st == norm:
                    found = True
                    break
                # Containment only counts when the shorter title is substantial,
                # so "One" can't match "One Piece" but full titles still align.
                shorter = min(len(st), len(norm))
                if shorter >= 6 and (norm in st or st in norm):
                    found = True
                    break
            if found:
                break
    except Exception as exc:
        logger.debug("[Crunchyroll] availability lookup failed for %r: %s", title, exc)
        found = False

    set_provider_cache(_PROVIDER_CACHE_NS, key, {"found": found})
    return found


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ── Calendar helpers (simulcast season slugs + episode probing for /debug) ────
def _season_slugs(d: _date) -> List[str]:
    """Crunchyroll season ids for the current and next season, e.g. 'summer-2026'.

    Seasons: winter (Jan–Mar), spring (Apr–Jun), summer (Jul–Sep), fall (Oct–Dec).
    The id format matches ``simulcast-seasons`` (verified against the live API).
    """
    order = ["winter", "spring", "summer", "fall"]
    idx = (d.month - 1) // 3
    cur = (order[idx], d.year)
    nxt = (order[(idx + 1) % 4], d.year + (1 if idx == 3 else 0))
    return [f"{name}-{year}" for name, year in (cur, nxt)]


# ── Title providers for the TMDB-backed calendar ─────────────────────────────
# Crunchyroll can't date future episodes, but it knows WHICH anime are in the
# current/next simulcast season and on the user's watchlist. The calendar takes
# these titles and resolves the air dates via TMDB (which has the episode data).
_titles_cache: Dict[str, "tuple[List[str], float]"] = {}
_entries_cache: Dict[str, "tuple[List[Dict[str, Any]], float]"] = {}
_TITLES_TTL = 30 * 60


def _cached_titles(key: str, builder) -> List[str]:
    now = time.time()
    hit = _titles_cache.get(key)
    if hit and (now - hit[1]) < _TITLES_TTL:
        return list(hit[0])
    try:
        out = builder() or []
    except Exception as exc:
        logger.debug("[Crunchyroll] title builder %s failed: %s", key, exc)
        out = []
    # Don't cache an empty result (likely a transient login/API hiccup).
    if out:
        _titles_cache[key] = (list(out), now)
    return list(out)


def get_simulcast_titles() -> List[str]:
    """Titles of the current + next simulcast season lineup (deduped, cached)."""
    if not is_enabled():
        return []

    def _build():
        client = get_client()
        if client is None:
            return []
        seen, out = set(), []
        for slug in _season_slugs(_date.today()):
            try:
                lineup = client.get_season_anime(slug, limit=_MAX_SERIES) or []
            except Exception as exc:
                logger.debug("[Crunchyroll] lineup %s failed: %s", slug, exc)
                continue
            for sr in lineup:
                t = (getattr(sr, "title", "") or "").strip()
                k = _norm(t)
                if t and k not in seen:
                    seen.add(k)
                    out.append(t)
        return out

    return _cached_titles("simulcast", _build)


def get_watchlist_titles() -> List[str]:
    """Titles of the active profile's watchlist (cached). Needs an account.

    The v2 watchlist endpoint returns only content ids (no panel/title), so the
    ids are resolved to objects via ``get_objects`` in batches to read titles.
    Gated on :func:`has_account` so it works with the master toggle off.
    """
    if not has_account():
        return []

    def _build():
        client = get_client()
        if client is None:
            return []
        # 1. Collect content ids (the slim watchlist carries no titles).
        ids, seen_id = [], set()
        try:
            for item in client.get_watchlist(limit=500) or []:
                cid = (getattr(item, "content_id", "") or "").strip()
                if cid and cid not in seen_id:
                    seen_id.add(cid)
                    ids.append(cid)
        except Exception as exc:
            logger.debug("[Crunchyroll] watchlist fetch failed: %s", exc)
            return []
        # 2. Resolve ids -> objects (with titles) in chunks of 50.
        seen, out = set(), []
        for i in range(0, len(ids), 50):
            try:
                objs = client.get_objects(ids[i:i + 50]) or []
            except Exception as exc:
                logger.debug("[Crunchyroll] watchlist resolve failed: %s", exc)
                continue
            for obj in objs:
                # Episode objects expose series_title; series/movies expose title.
                t = (getattr(obj, "series_title", "") or
                     getattr(obj, "title", "") or "").strip()
                k = _norm(t)
                if t and k not in seen:
                    seen.add(k)
                    out.append(t)
        return out

    return _cached_titles("watchlist", _build)


def get_custom_list_entries() -> List[Dict[str, Any]]:
    """Series from the user's custom lists (Crunchylists), tagged per list.

    Returns a list of ``{"title", "list_id", "list_name"}`` so callers can keep
    the lists separate in the output instead of merging them. Cached; needs an
    account. Gated on :func:`has_account` so it works with the master
    toggle off.
    """
    if not has_account():
        return []

    now = time.time()
    hit = _entries_cache.get("custom_lists")
    if hit and (now - hit[1]) < _TITLES_TTL:
        return [dict(e) for e in hit[0]]

    out: List[Dict[str, Any]] = []
    client = get_client()
    if client is None:
        return out
    try:
        lists = client.list_custom_lists() or []
    except Exception as exc:
        logger.debug("[Crunchyroll] custom lists failed: %s", exc)
        return out

    for lst in lists:
        if not isinstance(lst, dict):
            continue
        list_id = lst.get("list_id") or lst.get("id")
        list_name = (lst.get("title") or lst.get("name") or "").strip() or "Crunchylist"
        if not list_id:
            continue
        try:
            items = client.get_custom_list(list_id) or []
        except Exception as exc:
            logger.debug("[Crunchyroll] custom list %s failed: %s", list_id, exc)
            continue
        seen_in_list = set()
        for item in items:
            t = (getattr(item, "title", "") or "")
            if not t:
                panel = getattr(item, "panel", None) or {}
                t = panel.get("title", "") if isinstance(panel, dict) else ""
            t = (t or "").strip()
            k = _norm(t)
            if t and k not in seen_in_list:
                seen_in_list.add(k)
                out.append({"title": t, "list_id": list_id, "list_name": list_name})

    if out:
        _entries_cache["custom_lists"] = ([dict(e) for e in out], now)
    return out


def get_custom_list_titles() -> List[str]:
    """Flat, deduped titles across all custom lists (compat wrapper)."""
    seen, out = set(), []
    for e in get_custom_list_entries():
        k = _norm(e.get("title", ""))
        if k and k not in seen:
            seen.add(k)
            out.append(e["title"])
    return out


def get_simulcast_seasons() -> List[Dict[str, Any]]:
    """List available simulcast seasons (id + title), best effort."""
    if not is_enabled():
        return []
    client = get_client()
    if client is None:
        return []
    try:
        return client.list_simulcast_seasons() or []
    except Exception as exc:
        logger.debug("[Crunchyroll] simulcast seasons failed: %s", exc)
        return []


# ── Small parsers ─────────────────────────────────────────────────────────────
def _iso_date(value: str) -> Optional[str]:
    """Extract YYYY-MM-DD from an ISO datetime string."""
    if not value:
        return None
    txt = str(value).strip()
    try:
        # Handles '2026-07-01T17:00:00Z' and '2026-07-01T17:00:00+00:00'
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        # Fall back to the leading date token if present.
        token = txt[:10]
        try:
            datetime.strptime(token, "%Y-%m-%d")
            return token
        except Exception:
            return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except Exception:
        return None
