"""Server-side image proxy + poster caching.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...config import HANIME_API_BASE
from ...config import HANIME_IMAGE_HOSTS
from ...config import HANIME_BASE_URL
from ...config import HANIME_SEARCH_URL
from ...config import ANIWAVES_BASE_URL
from ...config import FILMO_BASE_URL
from ...config import MEDIAFORGE_CONFIG_DIR
from ...config import MEGAKINO_BASE_URL
from ...config import NINEANIME_BASE_URL
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


def _domains_of(*urls):
    """Allowed domains derived from the configured base URLs.

    Returns both the configured host AND its registrable domain (the last two
    labels), because a site serves its images from sibling hosts: the search
    endpoint may be search.<domain> while the posters come from cdn.<domain>.
    Taking only the configured host would reject the image CDN -- which is
    exactly what happened when this check replaced the old substring test and
    hanime posters stopped loading.

    Still a suffix match, never a substring one: <domain> and *.<domain> are
    accepted, hanime.attacker.tld is not. Two-label public suffixes (co.uk and
    friends) would widen this by one level, but every domain fed in here comes
    from our own configuration defaults, not from user input.
    """
    from urllib.parse import urlparse as _u
    out = set()
    for u in urls:
        host = (_u(u).hostname or "").lower().removeprefix("www.")
        if not host:
            continue
        out.add(host)
        labels = host.split(".")
        if len(labels) > 2:
            out.add(".".join(labels[-2:]))
    return out


# Sites whose image hosts vary (mirror domains, CDN subdomains), so they are
# matched by domain suffix rather than by an exact host. This USED to be a
# substring test -- `"hanime" not in host` -- which let an attacker point
# hanime.evil.tld (or any host containing the word) at an internal address and
# have the server fetch it. Suffix matching only accepts the domain itself and
# its subdomains.
# filmo.to, 9anime.or.at and aniwaves.ru all serve their posters from the
# site itself (filmo.to/img/poster/..., the WordPress theme's uploads dir, the
# SPA's own /i/ path) or from a subdomain of it, which is exactly what a
# suffix entry covers -- the same reason MegaKino is in this list rather than
# in the exact-host set above. Without them every card from these three
# sources renders the placeholder, because /api/img rejects the poster with
# 403 "Forbidden host" before it ever fetches it.
_ALLOWED_IMAGE_DOMAINS = _domains_of(
    MEGAKINO_BASE_URL, HANIME_BASE_URL, HANIME_API_BASE, HANIME_SEARCH_URL,
    FILMO_BASE_URL, NINEANIME_BASE_URL, ANIWAVES_BASE_URL,
) | {h for h in HANIME_IMAGE_HOSTS if h}


# ---------------------------------------------------------------------------
# Third-party image hosts
# ---------------------------------------------------------------------------
# A third-party module's provider/search source (register_provider /
# register_search_source) commonly returns poster_url values pointing at its
# own site's image CDN. Those URLs go through _poster_proxy() -> /api/img
# exactly like every built-in source's do -- the client never gets a direct
# URL to the source site -- but until now the allowlist above was a plain
# module-level set only the core could extend, so a third-party module's
# posters were silently rejected with 403 "Forbidden host" the moment they
# reached the proxy. This mirrors extractors.register_hoster's host_patterns
# and subtitle_sources.register_subtitle_source's item_id convention: keyed
# by item_id so web/thirdparties/registry.py's unregister_module() can drop
# a module's hosts automatically on disable/uninstall.
#
# Adding a host here only widens *which domain the proxy is willing to fetch
# from*; it does not weaken the second, independent check in
# api_image_proxy() (stream_proxy.is_safe_url), which still resolves the
# host's DNS and rejects anything pointing at an internal/loopback address
# regardless of how it got onto the allowlist.
_image_hosts_lock = threading.Lock()

# item_id -> {"hosts": frozenset, "domains": frozenset}
_EXTRA_IMAGE_HOSTS: dict = {}


def register_image_hosts(item_id, hosts=(), domains=()) -> None:
    """Allow the image proxy (``/api/img``) to fetch from a third-party
    module's own image host(s).

    - ``item_id``: the id already passed to ``register_thirdparty()`` for this
      module's entry, so ``web/thirdparties/registry.py``'s
      ``unregister_module()`` drops these hosts automatically on disable/
      uninstall -- a host registered under any other id keeps being allowed
      after the module is gone.
    - ``hosts``: exact hostnames, e.g. ``("cdn.myhoster.example",)``. Matched
      case-insensitively; a leading ``www.`` is ignored on both sides, same
      as the built-in ``_ALLOWED_IMAGE_HOSTS`` set.
    - ``domains``: registrable domains matched by suffix, e.g.
      ``("myhoster.example",)`` also allows ``img1.myhoster.example`` and
      ``static.myhoster.example`` -- use this instead of ``hosts`` when a
      site serves posters from a CDN subdomain that varies or isn't known in
      advance. Never a substring match: ``myhoster.example`` does not allow
      ``myhoster.example.attacker.tld``.

    At least one of ``hosts``/``domains`` must be given. Call again with the
    same ``item_id`` to replace what was previously registered (safe under
    the debug reloader), same as every other ``register_*`` in this codebase.

    Without this, a module's own ``poster_url`` values (returned from
    ``register_provider``/``register_search_source``) get proxied through
    ``_poster_proxy()`` like any other source's, but every fetch 403s at
    ``_image_host_allowed()`` -- posters silently never load.
    """
    hosts = {str(h).strip().lower().removeprefix("www.") for h in (hosts or ()) if str(h or "").strip()}
    domains = {str(d).strip().lower().removeprefix("www.") for d in (domains or ()) if str(d or "").strip()}
    if not hosts and not domains:
        raise ValueError("register_image_hosts: need at least one of hosts/domains")
    with _image_hosts_lock:
        _EXTRA_IMAGE_HOSTS[item_id] = {"hosts": frozenset(hosts), "domains": frozenset(domains)}
    logger.info("[ImageProxy] Registered image host(s) for %s: %s",
                item_id, ", ".join(sorted(hosts | domains)))


def unregister_image_hosts(item_id) -> None:
    """Drop the hosts/domains a module previously added via
    :func:`register_image_hosts`."""
    with _image_hosts_lock:
        removed = _EXTRA_IMAGE_HOSTS.pop(item_id, None)
    if removed:
        logger.info("[ImageProxy] Unregistered image host(s) for %s", item_id)


def thirdparty_image_host_ids() -> set:
    """item_ids that currently own at least one registered image host.

    Read-only counterpart of :func:`unregister_image_hosts`, used by the
    Modulmanager's capability list (see
    ``web/thirdparties/registry.py``'s ``module_capabilities()``).
    """
    with _image_hosts_lock:
        return set(_EXTRA_IMAGE_HOSTS)


def _module_item_enabled(item_id: str) -> bool:
    """Whether the module owning *item_id* is currently switched on.

    Deliberately NOT registry.item_enabled(): that helper is fail-*open* -- an
    unknown item id or a DB error both answer "yes", which is the right default
    for a provider list or a search source, where refusing to serve would break
    a working setup for no security benefit.

    Here the answer widens an SSRF-relevant allowlist, so every uncertain case
    has to mean "no":

    * an item id that is not a registered thirdparty item has no toggle to
      read, so nothing can ever switch it off -- exactly the hole this check
      exists to close. Modules are documented to register capabilities under
      the id they passed to register_thirdparty(); one that does not, does not
      get to extend the proxy's reach.
    * an error resolving the state is not a licence to fetch.

    A blocked image is a missing poster. A wrongly allowed host is a request
    the server makes on an attacker's behalf.

    Cached for a second inside the registry, which matters here: the proxy is
    hit once per poster, so a library grid would otherwise mean hundreds of
    extra SQLite reads per page view.
    """
    try:
        from ..thirdparties.registry import item_enabled_strict

        return item_enabled_strict(item_id)
    except Exception:
        logger.exception("[ImageProxy] Could not resolve enabled state for %s", item_id)
        return False


def _image_host_allowed(netloc: str) -> bool:
    host = (netloc or "").lower().split(":")[0]
    if host in _ALLOWED_IMAGE_HOSTS or host.removeprefix("www.") in _ALLOWED_IMAGE_HOSTS:
        return True
    if any(host == d or host.endswith("." + d) for d in _ALLOWED_IMAGE_DOMAINS):
        return True
    bare_host = host.removeprefix("www.")
    with _image_hosts_lock:
        extra_items = list(_EXTRA_IMAGE_HOSTS.items())
    # A module's register(app) runs whether or not the module is switched on,
    # so this dict holds entries for disabled modules too. Those must not keep
    # widening the proxy's allowlist: every extra host is outbound fetch
    # surface, and the whole point of the suffix-matching rewrite above was to
    # keep that surface exactly as large as it needs to be.
    extra = [entry for item_id, entry in extra_items if _module_item_enabled(item_id)]
    for entry in extra:
        if host in entry["hosts"] or bare_host in entry["hosts"]:
            return True
        if any(bare_host == d or bare_host.endswith("." + d) for d in entry["domains"]):
            return True
    return False

import hashlib as _hashlib
from pathlib import Path as _Path

_IMAGE_CACHE_DIR = MEDIAFORGE_CONFIG_DIR / "image_cache"
_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# A poster is a nice-to-have, and this fetch happens inside a request handler
# on a server with 16 worker threads. At 3 x 20s plus backoff a single page
# with 40 uncached posters could hold every thread for a minute and take the
# whole app down with it -- including the queue polls. One retry at 5s keeps a
# slow-but-working CDN usable while bounding the damage at ~11s.
_IMG_FETCH_RETRIES = 2
_IMG_FETCH_TIMEOUT = 5

# url -> Event, for requests that are already fetching that URL. Without this,
# three tabs opening the same browse page produced three upstream requests per
# poster. The TMDB path has had the same guard for a while (_tmdb_inflight in
# web/tmdb_cache.py); the image path did not.
_img_inflight: dict = {}
_img_inflight_lock = threading.Lock()

import concurrent.futures as _cf
import urllib.parse as _up_img
import atexit as _atexit
_img_pool = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-precache")
_atexit.register(_img_pool.shutdown, wait=False)


_IMAGE_CACHE_CLEANUP_INTERVAL = 24 * 60 * 60  # re-run cleanup every 24h


def _image_cache_cleanup_worker():
    """Background loop: purge stale cached image files every 24h.

    Runs once shortly after startup, then repeats on a fixed 24h interval
    for the lifetime of the process (daemon thread, same pattern as the
    update-check / auto-update workers in routes/update.py).
    """
    import time as _time
    while True:
        try:
            cleanup_image_cache()
        except Exception:
            logger.exception("Image cache cleanup error")
        try:
            # Converted eBooks live in the same kind of derived-data cache and
            # go stale the same way, so they ride along on this loop instead of
            # spawning a second thread that wakes up on the same schedule.
            from ..books.convert import cleanup_converted
            cleanup_converted()
        except Exception:
            logger.exception("Book conversion cache cleanup error")
        _time.sleep(_IMAGE_CACHE_CLEANUP_INTERVAL)


def ensure_image_cache_cleanup():
    """Start the periodic image-cache cleanup worker (runs every 24h)."""
    threading.Thread(
        target=_image_cache_cleanup_worker, daemon=True, name="image-cache-cleanup",
    ).start()


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


def cleanup_image_cache(max_age_days: int = 7):
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
        disk for 7 days; the cache is served directly without re-fetching.

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

        if not _image_host_allowed(parsed.netloc):
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
        # Only now, on the miss path: the allowlist says which site may be
        # fetched, this says the name must not resolve to an internal address
        # (a whitelisted domain whose DNS points at 127.0.0.1 or 169.254.x.x).
        from ..stream_proxy import is_safe_url as _img_safe_url
        if not _img_safe_url(raw_url):
            return ("Forbidden host", 403)

        # Collapse concurrent requests for the same URL: the first one fetches,
        # the others wait for it and then read the file it wrote.
        with _img_inflight_lock:
            waiter = _img_inflight.get(raw_url)
            leader = waiter is None
            if leader:
                waiter = threading.Event()
                _img_inflight[raw_url] = waiter

        if not leader:
            # Bounded by the fetch timeout above plus a little slack, so a
            # hanging leader cannot pin this thread indefinitely.
            waiter.wait(timeout=_IMG_FETCH_TIMEOUT * _IMG_FETCH_RETRIES + 2)
            cached = _img_cache_path_any(raw_url)
            if cached and cached.exists():
                ext = cached.suffix.lower()
                mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                        ".webp": "image/webp", ".gif": "image/gif",
                        ".avif": "image/avif"}.get(ext, "image/jpeg")
                r = send_file(cached, mimetype=mime)
                r.headers["Cache-Control"] = "public, max-age=604800"
                return r
            return ("", 502)

        try:
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
        finally:
            # Released only once the cache file is on disk, so a waiter really
            # finds it -- and in the failure case too, where the waiters fall
            # through to their own 502 instead of blocking for the full
            # timeout.
            with _img_inflight_lock:
                _img_inflight.pop(raw_url, None)
            waiter.set()
