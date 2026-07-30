"""OpenSubtitles.com lookup for languages the source itself does not offer.

``subtitles.py`` is deliberately hoster-only: it takes whatever the stream
already carries because that costs nothing beyond the download that is running
anyway. This module is the opposite trade -- an external service with an API
key, a user login, a hard daily download quota and a matching step that can be
wrong -- so it is **off by default** and only ever runs as a *last* fallback,
after yt-dlp and after the hoster's own player config came up empty for a
language.

Matching order, strongest first:

1. ``moviehash`` -- OpenSubtitles' own file hash (size + first/last 64 KiB).
   It identifies the exact release, so the timing fits without guessing.
2. ``query`` + ``season_number``/``episode_number`` -- title based, used when
   the hash matches nothing (usual for a fresh web rip nobody has uploaded a
   sub for yet). Results are still ordered by download count, but a wrong
   timing is possible here, which is why hash matches are always preferred.

Quota: the free tier allows a handful of downloads per day. Every ``/download``
call returns ``remaining``; it is cached so a run that has burned its quota
stops asking instead of hammering the API for 429s. Nothing in here may raise:
a missing subtitle is a cosmetic loss, the video is the deliverable.
"""

import os
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from ...logger import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

try:
    from .subtitles import MAX_SUBTITLE_TRACKS, normalize_lang
except ImportError:  # pragma: no cover - CLI / flat import
    from mediaforge.models.common.subtitles import MAX_SUBTITLE_TRACKS, normalize_lang


API_BASE = "https://api.opensubtitles.com/api/v1"

# OpenSubtitles rejects requests with a generic or missing User-Agent and asks
# for "AppName vX.Y". The version is resolved lazily so this module keeps
# importing when the package metadata is unavailable (source checkout, PyInstaller).
_USER_AGENT_FALLBACK = "MediaForge v1.0"

# JWT lifetime is 24 h; refreshed a little early so a long queue run does not
# trip over the boundary mid-download.
_TOKEN_TTL = 23 * 60 * 60

# Subtitle files are kilobytes. Anything past this is not a subtitle.
_MAX_SUBTITLE_BYTES = 4 * 1024 * 1024

# Per-process login/quota cache. The queue worker downloads from several
# threads and a login per episode would be both slow and rude.
_state_lock = threading.Lock()
_token_cache = {"token": None, "base": None, "at": 0.0, "key": None}
_quota = {"remaining": None, "reset_at": 0.0}

_DEFAULT_LANGUAGES = "de,en"

# The /download response hands back a URL that this process then fetches. It
# comes from a third party, so it is not followed blindly: https only, and only
# to OpenSubtitles' own hosts. Without this, a compromised or spoofed API
# response could make the server fetch an arbitrary internal address (SSRF).
_ALLOWED_DOWNLOAD_HOSTS = (
    "opensubtitles.com",
    "opensubtitles.org",
)


def _is_allowed_download_url(url) -> bool:
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in _ALLOWED_DOWNLOAD_HOSTS)


def _get_setting(key, default=""):
    """Read an app setting without importing the web stack at module load.

    Same shape as ``subtitles._get_setting`` -- ``models.*`` also runs from the
    CLI, where ``web.db`` (and Flask with it) may not import at all.
    """
    try:
        from ...web.db import get_setting
    except Exception:
        try:
            from mediaforge.web.db import get_setting
        except Exception:
            return default
    try:
        value = get_setting(key, default)
    except Exception:
        return default
    return default if value is None else value


def _user_agent() -> str:
    """``MediaForge vX.Y.Z``. OpenSubtitles rejects a generic User-Agent.

    Read from the installed package metadata rather than the web stack so this
    also works from the CLI and from a PyInstaller build.
    """
    try:
        from importlib.metadata import version
        return f"MediaForge v{version('mediaforge')}"
    except Exception:
        return _USER_AGENT_FALLBACK


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def opensubtitles_enabled() -> bool:
    """True only when the user switched it on *and* supplied credentials.

    Off by default, unlike ``dl_subtitles``: this one talks to a third party
    with the user's account and spends a quota they own.
    """
    if str(_get_setting("opensubtitles_enabled", os.environ.get("MEDIAFORGE_OPENSUBTITLES", "0"))) != "1":
        return False
    return bool(_credentials()[0])


def _credentials():
    """``(api_key, username, password)`` as configured, blanks stripped."""
    return (
        str(_get_setting("opensubtitles_api_key", "") or "").strip(),
        str(_get_setting("opensubtitles_username", "") or "").strip(),
        str(_get_setting("opensubtitles_password", "") or "").strip(),
    )


def wanted_languages():
    """Configured languages as OpenSubtitles' own (ISO 639-1) codes.

    Stored as a comma separated string so the settings UI can use the existing
    ``.mf-multiselect``. Order is preserved -- it is the user's preference
    order, and the search asks for all of them in one request.
    """
    raw = str(_get_setting("opensubtitles_languages", _DEFAULT_LANGUAGES) or "")
    if not raw.strip():
        raw = _DEFAULT_LANGUAGES
    seen = set()
    langs = []
    for part in raw.replace(";", ",").split(","):
        code = part.strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        langs.append(code)
    return langs[:MAX_SUBTITLE_TRACKS]


def hearing_impaired_pref() -> str:
    """``include`` / ``exclude`` / ``only`` for the ``hearing_impaired`` filter."""
    value = str(_get_setting("opensubtitles_hearing_impaired", "exclude") or "exclude").lower()
    return value if value in ("include", "exclude", "only") else "exclude"


# ---------------------------------------------------------------------------
# Moviehash
# ---------------------------------------------------------------------------

def compute_moviehash(path):
    """OpenSubtitles' file hash, or ``None`` when it cannot be computed.

    The published algorithm: 64-bit sum of the file size and of every 64-bit
    little-endian word in the first and last 64 KiB, truncated to 64 bits.
    Files below 128 KiB have no defined hash (the two chunks would overlap),
    which in practice only happens for a broken download.
    """
    chunk = 64 * 1024
    try:
        path = Path(path)
        size = path.stat().st_size
        if size < 2 * chunk:
            return None
        value = size
        with open(path, "rb") as handle:
            for offset in (0, size - chunk):
                handle.seek(offset)
                buffer = handle.read(chunk)
                if len(buffer) < chunk:
                    return None
                for word in struct.unpack(f"<{chunk // 8}Q", buffer):
                    value = (value + word) & 0xFFFFFFFFFFFFFFFF
        return f"{value:016x}"
    except (OSError, ValueError, struct.error) as exc:
        logger.debug("[OpenSubtitles] moviehash failed for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _requests():
    try:
        import requests
        return requests
    except ImportError:  # pragma: no cover
        logger.debug("[OpenSubtitles] requests unavailable — skipping")
        return None


def _headers(api_key, token=None):
    head = {
        "Api-Key": api_key,
        "User-Agent": _user_agent(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        head["Authorization"] = f"Bearer {token}"
    return head


def _login(api_key, username, password, force=False):
    """``(token, base_url)`` from the cache or a fresh ``POST /login``.

    ``base_url`` matters: VIP accounts are answered by ``vip-api.opensubtitles.com``
    and the login response says so. Using the wrong host still works but costs
    the account its VIP rate limit.
    """
    now = time.time()
    with _state_lock:
        cached = dict(_token_cache)
    if (
        not force
        and cached["token"]
        and cached["key"] == (api_key, username)
        and now - cached["at"] < _TOKEN_TTL
    ):
        return cached["token"], cached["base"]

    requests = _requests()
    if requests is None or not (api_key and username and password):
        return None, None

    try:
        resp = requests.post(
            f"{API_BASE}/login",
            json={"username": username, "password": password},
            headers=_headers(api_key),
            timeout=20,
        )
        if resp.status_code == 401:
            logger.warning("[OpenSubtitles] login rejected — check username/password")
            return None, None
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        logger.warning("[OpenSubtitles] login failed: %s", exc)
        return None, None

    token = payload.get("token")
    if not token:
        return None, None
    base = payload.get("base_url") or ""
    base = f"https://{base}/api/v1" if base and not base.startswith("http") else (base or API_BASE)
    with _state_lock:
        _token_cache.update({"token": token, "base": base, "at": now, "key": (api_key, username)})
    return token, base


def invalidate_session():
    """Forget the cached JWT and quota state.

    Called when the settings change: a token belongs to one account, and a
    quota reading from the old one would silently suppress downloads for the
    new one.
    """
    with _state_lock:
        _token_cache.update({"token": None, "base": None, "at": 0.0, "key": None})
        _quota.update({"remaining": None, "reset_at": 0.0})


def _quota_exhausted() -> bool:
    with _state_lock:
        remaining, reset_at = _quota["remaining"], _quota["reset_at"]
    if remaining is None or remaining > 0:
        return False
    if reset_at and time.time() >= reset_at:
        with _state_lock:
            _quota["remaining"] = None
        return False
    return True


def _note_quota(payload):
    remaining = payload.get("remaining")
    if remaining is None:
        return
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        return
    with _state_lock:
        _quota["remaining"] = remaining
        # ``reset_time_utc`` is an ISO timestamp, ``reset_time`` a human string
        # like "23 hours". Neither is worth parsing precisely — the next UTC
        # midnight is when the free quota actually rolls over.
        _quota["reset_at"] = time.time() + 6 * 60 * 60
    if remaining <= 0:
        logger.info("[OpenSubtitles] daily download quota is used up — skipping until it resets")


# ---------------------------------------------------------------------------
# Search / download
# ---------------------------------------------------------------------------

def search(api_key, token, base, params):
    """``GET /subtitles`` -> list of result dicts. ``[]`` on any failure."""
    requests = _requests()
    if requests is None:
        return []
    try:
        resp = requests.get(
            f"{base or API_BASE}/subtitles",
            params=params,
            headers=_headers(api_key, token),
            timeout=25,
        )
        if resp.status_code == 429:
            logger.info("[OpenSubtitles] rate limited on search — skipping this file")
            return []
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or []
    except Exception as exc:
        logger.debug("[OpenSubtitles] search failed (%s): %s", params, exc)
        return []


def _best_files(results, languages):
    """One ``(lang, file_id, name, hash_match)`` per language, best first.

    "Best" = a hash match if there is one, otherwise the most downloaded
    upload. Download count is the only quality signal the API offers, and a
    subtitle thousands of people kept is a safer bet than the newest one.
    """
    per_lang = {}
    for entry in results or []:
        attrs = (entry or {}).get("attributes") or {}
        lang = str(attrs.get("language") or "").lower()
        if not lang:
            continue
        files = attrs.get("files") or []
        if not files:
            continue
        file_id = (files[0] or {}).get("file_id")
        if not file_id:
            continue
        rank = (
            1 if attrs.get("moviehash_match") else 0,
            int(attrs.get("download_count") or 0),
        )
        current = per_lang.get(lang)
        if current is None or rank > current[0]:
            per_lang[lang] = (
                rank,
                (lang, file_id, (files[0] or {}).get("file_name") or "", bool(attrs.get("moviehash_match"))),
            )
    ordered = []
    for lang in languages:
        # The API answers "pt-br" for a "pt-BR" request and "zh-cn" for "zh-CN";
        # match on the prefix so a regional variant still satisfies the wish.
        for key, (_rank, item) in per_lang.items():
            if key == lang or key.split("-")[0] == lang.split("-")[0]:
                ordered.append(item)
                break
    return ordered


def download_file(api_key, token, base, file_id, dest):
    """Fetch one subtitle to *dest*. True on success, False otherwise."""
    requests = _requests()
    if requests is None:
        return False
    try:
        resp = requests.post(
            f"{base or API_BASE}/download",
            json={"file_id": int(file_id)},
            headers=_headers(api_key, token),
            timeout=25,
        )
        if resp.status_code == 406:
            # What the API returns when the daily quota is gone.
            _note_quota({"remaining": 0})
            return False
        if resp.status_code == 429:
            logger.info("[OpenSubtitles] rate limited on download — skipping")
            return False
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as exc:
        logger.warning("[OpenSubtitles] could not request download link: %s", exc)
        return False

    _note_quota(payload)
    link = payload.get("link")
    if not link:
        return False
    if not _is_allowed_download_url(link):
        logger.warning("[OpenSubtitles] refusing download link outside opensubtitles.com: %s", link)
        return False

    try:
        # The link points at a plain CDN file, not the API — no Api-Key here,
        # and a size cap so a redirect to something large cannot fill the
        # temp drive.
        content = requests.get(link, timeout=30, stream=True)
        content.raise_for_status()
        data = b""
        for chunk in content.iter_content(65536):
            data += chunk
            if len(data) > _MAX_SUBTITLE_BYTES:
                raise ValueError("subtitle exceeds 4 MB — refusing")
        if not data.strip():
            return False
        Path(dest).write_bytes(data)
        return True
    except Exception as exc:
        logger.warning("[OpenSubtitles] download failed: %s", exc)
        try:
            Path(dest).unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Entry point used by the download path
# ---------------------------------------------------------------------------

def fetch_missing_subtitles(video_path, have_langs=(), meta=None):
    """Download configured languages that *video_path* does not have yet.

    *have_langs* are ISO 639-2/B tags already collected from the stream, so a
    language the hoster served is never fetched (and never charged to the
    quota) a second time. *meta* carries what the caller knows about the title:
    ``{"query": str, "season": int|None, "episode": int|None, "imdb_id": str|None,
    "tmdb_id": str|None}``.

    Sidecars are written with yt-dlp's naming (``<stem>.<lang>.srt``) so the
    existing ``collect_subtitle_files`` / mux path picks them up without knowing
    where they came from. Returns the list of written paths; never raises.
    """
    try:
        if not opensubtitles_enabled():
            return []
        api_key, username, password = _credentials()
        wanted = [
            lang for lang in wanted_languages()
            if normalize_lang(lang) not in set(have_langs or ())
        ]
        if not wanted:
            logger.debug("[OpenSubtitles] every configured language is already present")
            return []
        if _quota_exhausted():
            return []

        token, base = _login(api_key, username, password)
        if not token:
            return []

        video_path = Path(video_path)
        meta = meta or {}
        common = {
            "languages": ",".join(wanted),
            "hearing_impaired": hearing_impaired_pref(),
            "order_by": "download_count",
            "order_direction": "desc",
        }

        # 1. Hash first — it identifies the exact release, so the timing fits.
        results = []
        movie_hash = compute_moviehash(video_path)
        if movie_hash:
            results = search(api_key, token, base, dict(common, moviehash=movie_hash))

        # 2. Fall back to a title search when the hash is unknown to the site,
        #    which is the normal case for a fresh rip.
        if not results:
            params = dict(common)
            if meta.get("imdb_id"):
                params["imdb_id"] = str(meta["imdb_id"]).lstrip("t") or None
            elif meta.get("tmdb_id"):
                params["tmdb_id"] = str(meta["tmdb_id"])
            elif meta.get("query"):
                params["query"] = str(meta["query"])
            else:
                return []
            if meta.get("season") is not None:
                params["season_number"] = int(meta["season"])
            if meta.get("episode") is not None:
                params["episode_number"] = int(meta["episode"])
                params["type"] = "episode"
            results = search(api_key, token, base, {k: v for k, v in params.items() if v is not None})

        if not results:
            logger.info("[OpenSubtitles] no match for %s", video_path.name)
            return []

        stem = video_path.with_suffix("").name
        written = []
        for lang, file_id, name, hash_match in _best_files(results, wanted):
            if _quota_exhausted():
                break
            dest = video_path.parent / f"{stem}.{lang}.srt"
            if dest.exists():
                continue
            if download_file(api_key, token, base, file_id, dest):
                written.append(dest)
                logger.info(
                    "[OpenSubtitles] %s subtitle for %s (%s match): %s",
                    lang, video_path.name, "hash" if hash_match else "title", name or file_id,
                )
        return written
    except Exception as exc:  # never fail a download over a subtitle
        logger.debug("[OpenSubtitles] fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Settings UI support
# ---------------------------------------------------------------------------

def test_connection(api_key="", username="", password=""):
    """Validate credentials for the settings page. Returns a plain dict.

    Only ``/login`` is called: it is free, it proves key *and* account, and it
    reports the account's remaining download allowance, which is the number
    the user actually wants to see.
    """
    api_key = str(api_key or "").strip()
    username = str(username or "").strip()
    password = str(password or "").strip()
    if not api_key:
        return {"ok": False, "error": "no_api_key"}
    if not (username and password):
        return {"ok": False, "error": "no_credentials"}

    requests = _requests()
    if requests is None:
        return {"ok": False, "error": "requests_missing"}
    try:
        resp = requests.post(
            f"{API_BASE}/login",
            json={"username": username, "password": password},
            headers=_headers(api_key),
            timeout=20,
        )
    except Exception as exc:
        return {"ok": False, "error": "unreachable", "detail": str(exc)}

    if resp.status_code == 401:
        return {"ok": False, "error": "bad_credentials"}
    if resp.status_code == 403:
        return {"ok": False, "error": "bad_api_key"}
    if resp.status_code == 429:
        return {"ok": False, "error": "rate_limited"}
    if resp.status_code >= 400:
        return {"ok": False, "error": "http_error", "detail": str(resp.status_code)}

    try:
        payload = resp.json() or {}
    except Exception:
        return {"ok": False, "error": "bad_response"}
    user = payload.get("user") or {}
    if not payload.get("token"):
        return {"ok": False, "error": "no_token"}

    # Cache the fresh token — a successful test doubles as the login the next
    # download would otherwise have to perform.
    with _state_lock:
        base = payload.get("base_url") or ""
        _token_cache.update({
            "token": payload["token"],
            "base": f"https://{base}/api/v1" if base and not base.startswith("http") else (base or API_BASE),
            "at": time.time(),
            "key": (api_key, username),
        })
    return {
        "ok": True,
        "allowed_downloads": user.get("allowed_downloads"),
        "level": user.get("level"),
        "vip": bool(user.get("vip")),
    }
