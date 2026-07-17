"""Server-side image proxy + poster caching.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...config import MEDIAFORGE_CONFIG_DIR
from ..db import get_setting
from ..db import get_tmdb_cache_bulk
from flask import request
from flask import session
import threading
from ...logger import get_logger


logger = get_logger(__name__)


_ALLOWED_IMAGE_HOSTS = {
    "aniworld.to", "www.aniworld.to",
    "s.to", "www.s.to", "serienstream.to",
    "filmpalast.to", "www.filmpalast.to",
    "image.tmdb.org", "cdn.myanimelist.net",
    "cdn.aniworld.to",
    # Crunchyroll image CDNs (calendar thumbnails / series art)
    "imgsrv.crunchyroll.com", "static.crunchyroll.com",
    "img1.ak.crunchyroll.com", "www.crunchyroll.com",
}

import hashlib as _hashlib
from pathlib import Path as _Path

_IMAGE_CACHE_DIR = MEDIAFORGE_CONFIG_DIR / "image_cache"
_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_IMG_FETCH_RETRIES = 3
_IMG_FETCH_TIMEOUT = 20

import concurrent.futures as _cf
import urllib.parse as _up_img
import atexit as _atexit
_img_pool = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-precache")
_atexit.register(_img_pool.shutdown, wait=False)


def ensure_image_cache_cleanup():
    """Run the image-cache cleanup once in the background at startup."""
    threading.Thread(target=cleanup_image_cache, daemon=True).start()


def _img_upstream_headers(raw_url: str) -> dict:
    """Referer + Accept so CDNs don't drop requests that look like off-site hotlinks."""
    from urllib.parse import urlparse as _urlp_img

    try:
        netloc = _urlp_img(raw_url).netloc.lower()
    except Exception:
        return {}
    host = netloc.removeprefix("www.")
    referer_by_host = {
        "filmpalast.to": "https://filmpalast.to/",
        "s.to": "https://serienstream.to/",
        "serienstream.to": "https://serienstream.to/",
        "aniworld.to": "https://aniworld.to/",
        "cdn.aniworld.to": "https://aniworld.to/",
    }
    ref = referer_by_host.get(host)
    if not ref:
        return {}
    return {
        "Referer": ref,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }


def _img_fetch_with_retries(raw_url: str):
    """
    GET with retries over classic HTTPS (TCP).

    GLOBAL_SESSION uses niquests, which may negotiate HTTP/3 (QUIC). Cloudflare
    often resets those connections from Python (logs: quic … Connection close
    0x128).  Plain ``requests`` stays on HTTP/1.1 or HTTP/2 over TLS — same
    approach as FilmPalastEpisode._html (see episode.py).

    Several source CDNs (aniworld/s.to/filmpalast/Crunchyroll) sit behind
    Cloudflare bot protection. Plain ``requests`` exposes a Python/OpenSSL TLS
    fingerprint that Cloudflare blocks on Windows builds — the reason posters
    "barely load" there while Docker (Linux OpenSSL) is fine. curl_cffi
    replays a real Chrome TLS handshake so the fingerprint matches the
    User-Agent; we fall back to plain ``requests`` when it is unavailable.
    """
    import time as _time

    import requests as _rq

    from ...config import DEFAULT_USER_AGENT

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    headers.update(_img_upstream_headers(raw_url))

    # Prefer a Chrome-impersonating client to defeat Cloudflare fingerprinting.
    try:
        from curl_cffi import requests as _curl_requests  # type: ignore

        def _do_get():
            return _curl_requests.get(
                raw_url, timeout=_IMG_FETCH_TIMEOUT,
                headers=headers, impersonate="chrome120",
            )
    except Exception:
        def _do_get():
            return _rq.get(raw_url, timeout=_IMG_FETCH_TIMEOUT, headers=headers)

    last_exc = None
    for attempt in range(_IMG_FETCH_RETRIES):
        try:
            resp = _do_get()
            if resp.status_code in (502, 503, 504) and attempt + 1 < _IMG_FETCH_RETRIES:
                _time.sleep(0.25 * (2**attempt))
                continue
            return resp
        except Exception as e:
            last_exc = e
            if attempt + 1 < _IMG_FETCH_RETRIES:
                _time.sleep(0.25 * (2**attempt))
                continue
            raise last_exc from None


def _img_cache_path(url: str, content_type: str = "image/jpeg") -> _Path:
    """Return the cache file path for a given URL."""
    url_hash = _hashlib.sha256(url.encode()).hexdigest()[:32]
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif", "image/avif": ".avif"}.get(content_type, ".jpg")
    return _IMAGE_CACHE_DIR / (url_hash + ext)


def _img_cache_path_any(url: str) -> "_Path | None":
    """Return existing cache file for a URL (regardless of extension), or None."""
    url_hash = _hashlib.sha256(url.encode()).hexdigest()[:32]
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
        p = _IMAGE_CACHE_DIR / (url_hash + ext)
        if p.exists():
            return p
    return None


def cleanup_image_cache(max_age_days: int = 30):
    """Delete cached image files not accessed in the last max_age_days days."""
    import time
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for f in _IMAGE_CACHE_DIR.iterdir():
            if f.is_file() and f.stat().st_atime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    except Exception as e:
        logger.debug(f"Image cache cleanup error: {e}")
    if removed:
        logger.debug(f"Image cache: removed {removed} stale file(s)")


def _precache_image_bg(url: str):
    """Fetch and save a single image to disk cache. Runs in background pool."""
    if not url or not url.startswith("http"):
        return
    if _img_cache_path_any(url):
        return  # already on disk
    try:
        resp = _img_fetch_with_retries(url)
        if not resp.ok:
            return
        ct = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not ct.startswith("image/"):
            return
        _img_cache_path(url, ct).write_bytes(resp.content)
    except Exception as exc:
        logger.debug(f"img pre-cache failed for {url}: {exc}")


def _poster_proxy(url: str) -> str:
    """
    Convert a raw source poster URL to the server-side proxy URL AND
    kick off a background pre-cache fetch.  The client browser will
    NEVER receive a direct URL to aniworld.to / s.to / filmpalast.to /
    image.tmdb.org — it always gets /api/img?url=… served by this server.
    """
    if not url:
        return ""
    if url.startswith("/api/img"):
        return url  # already proxied — no-op
    _img_pool.submit(_precache_image_bg, url)
    return "/api/img?url=" + _up_img.quote(url, safe="")


def _proxy_result_list(results: list) -> list:
    """Return results with proxied poster URLs and inline cached TMDB data."""
    api_key = get_setting("cineinfo_tmdb_api_key", "")
    country = get_setting("cineinfo_country", "DE")
    ui_lang = session.get("ui_language", "de")
    tmdb_on = bool(api_key)

    cache_hits = {}
    if tmdb_on and results:
        keys = []
        for r in results:
            if hasattr(r, "get"):
                title = r.get("title", "")
                if title:
                    keys.append(title + "|||" + country + "|||" + ui_lang)
        if keys:
            cache_hits = get_tmdb_cache_bulk(keys)

    out = []
    for r in results:
        r = dict(r)
        if r.get("poster_url"):
            r["poster_url"] = _poster_proxy(r["poster_url"])
        if tmdb_on:
            title = r.get("title", "")
            if title:
                cached = cache_hits.get(title + "|||" + country + "|||" + ui_lang)
                if cached is not None:
                    r["tmdb"] = cached
        out.append(r)
    return out


def register_image_proxy_routes(app):
    """Register the server-side image proxy route on the Flask app."""
    @app.route("/api/img")
    def api_image_proxy():
        """
        Serve GET /api/img?url=...: server-side image proxy with disk cache.

        Fetches poster/cover images on behalf of the client so mobile devices
        don't need a direct connection to source sites (avoids ISP DNS blocks,
        hotlink protection, and mixed-content issues).  Images are cached to
        disk for 30 days; the cache is served directly without re-fetching.

        Only whitelisted source domains are allowed.

        Called from templates/base.html's `proxyImg()` JS helper, which is
        used across the frontend wherever a raw source-site image URL needs
        to be rewritten to this proxied form.
        """
        from urllib.parse import urlparse
        from flask import Response, send_file

        raw_url = request.args.get("url", "").strip()
        if not raw_url:
            return ("", 400)

        try:
            parsed = urlparse(raw_url)
        except Exception:
            return ("Bad URL", 400)

        netloc = parsed.netloc.lower()
        host_stripped = netloc.removeprefix("www.")
        if (netloc not in _ALLOWED_IMAGE_HOSTS
                and host_stripped not in _ALLOWED_IMAGE_HOSTS
                and "megakino" not in host_stripped
                and "hanime" not in host_stripped
                and "htv-services" not in host_stripped):
            return ("Forbidden host", 403)

        # --- Serve from disk cache if available ---
        cached = _img_cache_path_any(raw_url)
        if cached and cached.exists():
            # Touch the file to reset the LRU timer
            try:
                cached.touch()
            except OSError:
                pass
            ext = cached.suffix.lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif"}.get(ext, "image/jpeg")
            r = send_file(cached, mimetype=mime)
            r.headers["Cache-Control"] = "public, max-age=604800"  # 7 days browser cache
            return r

        # --- Fetch from source ---
        try:
            resp = _img_fetch_with_retries(raw_url)
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                return ("Not an image", 400)
            data = resp.content
        except Exception as e:
            logger.debug(f"Image proxy fetch failed for {raw_url}: {e}")
            return ("", 502)

        # --- Save to disk cache ---
        cache_file = _img_cache_path(raw_url, content_type)
        try:
            cache_file.write_bytes(data)
        except OSError as e:
            logger.debug(f"Image cache write failed: {e}")

        r = Response(data, content_type=content_type)
        r.headers["Cache-Control"] = "public, max-age=604800"
        return r
