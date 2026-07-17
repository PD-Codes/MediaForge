"""Shared runtime configuration for MediaForge.

Central grab-bag module holding: version/update checks, the package's HTTP
session (``GLOBAL_SESSION``, thread-local, DoH-aware), provider HTTP headers,
audio/subtitle language enums and lookup tables, URL-classification regex
patterns for every supported site (AniWorld, SerienStream, MegaKino,
hanime.tv), and directory paths (mpv config/scripts). Most other modules in
the package import from here rather than reading ``os.environ`` directly.
"""

import os
import re
import threading
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import fake_useragent
from niquests import RequestException, Session
from packaging.version import parse as parse_version

from .env import merge_env
from .logger import get_logger

VERSION = None

try:
    VERSION = version("mediaforge")
except PackageNotFoundError:
    VERSION = None


def is_newest_version() -> bool:
    """Return True if the installed version is >= the latest on PyPI.

    Also returns False if the package isn't installed (no VERSION) or the
    PyPI request fails. Not currently called anywhere in the WebUI; kept
    available for a future update-check feature.
    """
    if not VERSION:
        return False

    try:
        response = GLOBAL_SESSION.get("https://pypi.org/pypi/mediaforge/json")
        response.raise_for_status()
        latest_version = response.json()["info"]["version"]
        return parse_version(VERSION) >= parse_version(latest_version)
    except RequestException:
        # Could not fetch PyPI info, assume not newest
        return False


# MediaForge's per-user config/data directory (formerly ~/.aniworld before
# the AniWorld Downloader -> MediaForge rename; see legacy_import.py).
MEDIAFORGE_CONFIG_DIR = Path.home() / ".mediaforge"

# Load .env file whenever config is imported
merge_env(
    Path(__file__).resolve().parent / ".env.example",
    MEDIAFORGE_CONFIG_DIR / ".env",
)

logger = get_logger(__name__)

NAMING_TEMPLATE = os.getenv(
    "MEDIAFORGE_NAMING_TEMPLATE",
    "{title} ({year}) [imdbid-{imdbid}]/Season {season}/{title} S{season}E{episode}.mkv",
)

# Video codec configuration
VIDEO_CODEC = os.getenv("MEDIAFORGE_VIDEO_CODEC", "copy")

# Simple codec mapping using ffmpeg defaults
VIDEO_CODEC_MAP = {
    "copy": "copy",
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",
}

ACTION_METHODS = {
    "Download": "download",
    "Watch": "watch",
    "Syncplay": "syncplay",
}


_SOURCE_UNAVAILABLE_PATTERN = re.compile(
    r"(video\s+(not\s+found|has\s+been\s+removed|is\s+not\s+available|was\s+deleted)"
    r"|file\s+not\s+found"
    r"|this\s+video\s+does\s+not\s+exist"
    r"|<title>[^<]*\b404\b[^<]*</title>"
    r"|<title>[^<]*not\s+found[^<]*</title>"
    r"|<title>[^<]*removed[^<]*</title>"
    r"|<title>[^<]*deleted[^<]*</title>"
    r"|im\s+wartungsmodus"
    r"|in\s+maintenance\s+mode"
    r"|web\s+server\s+is\s+down)",
    re.IGNORECASE,
)
_UNAVAILABLE_STATUS_CODES = frozenset({404, 410, 451})


def is_source_unavailable(html: str, status_code: int = 200) -> bool:
    """Return True if the hoster page signals that the content is gone.

    Uses only the already-fetched response — no extra HTTP requests.
    """
    if status_code in _UNAVAILABLE_STATUS_CODES:
        return True
    return bool(_SOURCE_UNAVAILABLE_PATTERN.search(html))


def _fetch_redirect_page(url: str, timeout: int, referer: str | None = None):
    """GET *url* and return (html, status_code), preferring curl_cffi (bypasses
    Cloudflare-style protection) and falling back to GLOBAL_SESSION."""
    headers = {"Referer": referer} if referer else None
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(
            url, impersonate="chrome120", timeout=timeout,
            allow_redirects=True, headers=headers,
        )
        return resp.text, resp.status_code
    except ImportError:
        resp = GLOBAL_SESSION.get(url, allow_redirects=True, timeout=timeout, headers=headers)
        return resp.text, resp.status_code


def _fetch_redirect_page_url(url: str, timeout: int, referer: str | None = None):
    """Like ``_fetch_redirect_page`` but also returns the final resolved URL
    (after following redirects), so callers can identify the real hoster host."""
    headers = {"Referer": referer} if referer else None
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(
            url, impersonate="chrome120", timeout=timeout,
            allow_redirects=True, headers=headers,
        )
        return resp.text, resp.status_code, resp.url
    except ImportError:
        resp = GLOBAL_SESSION.get(url, allow_redirects=True, timeout=timeout, headers=headers)
        return resp.text, resp.status_code, resp.url


def probe_redirect(redirect_url: str, provider_name: str = "", timeout: int = 5):
    """Follow a provider redirect once and report both liveness and real host.

    Returns ``(available, host_provider)``:
      * available      -- whether the hoster actually still has the content
                          (same verdict as check_redirect_available, incl. the
                          VOE JS-redirect second hop for removed VOE videos).
      * host_provider  -- provider key (extractor suffix, e.g. "voe") derived
                          from the *resolved* embed host, or None if unknown.
                          Lets callers collapse mirror labels (a "Vidara" entry
                          that really lands on voe.sx) onto the real hoster.

    On any network error returns ``(True, None)`` so a flaky check never hides a
    provider the download path could still try. Does a real GET because many
    hosters (e.g. VOE) return HTTP 200 even for removed videos and only show the
    error in the HTML body/title.
    """
    try:
        html, status_code, final_url = _fetch_redirect_page_url(redirect_url, timeout)
    except Exception as e:
        logger.debug(f"Failed to probe redirect for {redirect_url}: {e}")
        return True, None

    try:
        from .extractors import provider_for_url
        host_provider = provider_for_url(final_url)
    except Exception:
        host_provider = None

    if is_source_unavailable(html, status_code):
        return False, host_provider

    # VOE (by resolved host or label): the first page is a tiny anti-scraper
    # shell that JS-redirects to the real CDN page; a plain GET never runs that
    # JS, so follow the same hop the VOE extractor uses before deciding.
    if host_provider == "voe" or provider_name.strip().upper() == "VOE":
        try:
            from .extractors.provider.voe import (
                REDIRECT_PATTERN,
                extract_voe_source_from_html,
                is_maintenance_page,
            )
            if extract_voe_source_from_html(html):
                return True, host_provider or "voe"
            match = REDIRECT_PATTERN.search(html)
            if match:
                try:
                    cdn_html, cdn_status = _fetch_redirect_page(
                        match.group(0), timeout, referer=redirect_url
                    )
                except Exception as e:
                    logger.debug(f"VOE second-hop check failed for {redirect_url}: {e}")
                    return True, host_provider or "voe"
                if is_source_unavailable(cdn_html, cdn_status) or is_maintenance_page(cdn_html):
                    return False, host_provider or "voe"
                return bool(extract_voe_source_from_html(cdn_html)), host_provider or "voe"
        except Exception as e:
            logger.debug(f"VOE-specific availability check failed for {redirect_url}: {e}")
            return True, host_provider or "voe"

    return True, host_provider


def check_redirect_available(redirect_url: str, provider_name: str = "", timeout: int = 5) -> bool:
    """Follow a provider redirect and check if the hoster actually has the
    content. Thin wrapper over probe_redirect() kept for existing callers that
    only need the liveness verdict.

    On any network error returns True so the download path can fail with a
    proper message instead of silently hiding the provider.
    """
    available, _host_provider = probe_redirect(redirect_url, provider_name, timeout)
    return available


def resolve_redirect_url(redirect_url: str, timeout: int = 10) -> str:
    """Follow redirects and return the final destination URL.

    Uses curl_cffi to bypass Cloudflare protection on the target hoster,
    falling back to GLOBAL_SESSION.
    Used by: ``models/filmpalast_to/episode.py`` to resolve the real hoster
    URL behind a FilmPalast redirect.
    """
    try:
        try:
            from curl_cffi import requests as curl_requests
            resp = curl_requests.get(
                redirect_url,
                impersonate="chrome120",
                timeout=timeout,
                allow_redirects=True
            )
            return resp.url
        except ImportError:
            resp = GLOBAL_SESSION.get(redirect_url, allow_redirects=True, timeout=timeout)
            return resp.url
    except Exception as e:
        logger.debug(f"Failed to resolve redirect URL for {redirect_url}: {e}")
        return redirect_url


def get_video_codec():
    """Return the ffmpeg codec name for MEDIAFORGE_VIDEO_CODEC, falling back
    to "copy" (stream copy, no re-encoding) if the configured value isn't a
    recognized key in VIDEO_CODEC_MAP."""
    codec = VIDEO_CODEC
    if codec not in VIDEO_CODEC_MAP:
        logger.warning(
            f"Invalid video codec '{codec}', falling back to 'copy'. Valid options: {list(VIDEO_CODEC_MAP.keys())}"
        )
        return "copy"
    return VIDEO_CODEC_MAP[codec]


# NIQUESTS

try:
    DEFAULT_USER_AGENT = str(
        fake_useragent.UserAgent(os=["Windows", "Mac OS X"]).random
    )
except fake_useragent.errors.FakeUserAgentError:
    # TODO: fix - currently happens on nuitka builds
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

LULUVDO_USER_AGENT = (
    "Mozilla/5.0 (Android 15; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0"
)

_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://aniworld.to/search",
    "Priority": "u=0, i",
}


# Default timeout for all HTTP requests via GLOBAL_SESSION (connect, read)
_DEFAULT_TIMEOUT = (10, 30)


# -----------------------------
# TLS: validate against the OS trust store where we can
# -----------------------------
# Python does NOT use the operating system's certificate store -- it validates
# against the CA bundle that ships with certifi. That bundle ages with the
# installed package, so a chain the browser happily accepts (Chrome/Edge use the
# Windows store, which Windows Update keeps current) can still fail in Python
# with "certificate has expired" -- typically because the served chain is
# validated up through an expired cross-signed root (the classic Let's Encrypt
# ISRG Root X1 <- DST Root CA X3 case) that the OS store replaced long ago. The
# leaf certificate is perfectly valid in that case; the trust anchor is stale.
#
# truststore fixes that by validating against the OS store -- but ONLY as an
# explicitly passed SSLContext, never via truststore.inject_into_ssl().
#
# Do NOT call inject_into_ssl() here. It swaps out the ssl.SSLContext module
# attribute, and CPython's own SSLContext property setters resolve the name
# ``SSLContext`` from the ssl module at call time
# (``super(SSLContext, SSLContext).minimum_version.__set__(...)``). Once that
# name points at truststore's subclass, urllib3-future's create_urllib3_context()
# -- which niquests, and therefore GLOBAL_SESSION *and the DoH resolver*, runs on
# every single connection -- recurses until RecursionError. The result is every
# site going "offline" at once, DNS included.
def _os_trust_store_context():
    """An SSLContext validating against the OS trust store, or None if
    truststore isn't installed. Passed explicitly to the urllib call sites."""
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


# -----------------------------
# TLS: first-party hosts exempt from certificate verification
# -----------------------------
# MediaForge's own infrastructure (the Dev Info feed, the module store, the
# mpv download) occasionally serves an expired/misissued certificate, which
# makes every outbound call to it die with SSLCertVerificationError. These
# hosts are OURS, and what actually protects the payloads that matter is not
# TLS but the signature check: module packages are verified against the
# built-in signing keys (see web/thirdparties/store.py + trusted_keys.py)
# regardless of how they were transported.
#
# So: certificate verification is skipped for these hosts, and ONLY these
# hosts. This is deliberately a short, hard-coded allowlist and not a global
# "verify=False" -- every third-party host (hosters, TMDB, Jellyfin, the
# scraper sites) keeps full verification. Note this does trade away MITM
# protection on the listed hosts; renewing the certificate is still the real
# fix, this only stops a lapsed cert from taking features offline.
#
# Extendable for self-hosters via MEDIAFORGE_TLS_INSECURE_HOSTS (comma-separated,
# same glob syntax).
TLS_INSECURE_HOSTS = (
    "domekologe.eu",
    "*.domekologe.eu",
    "softarchiv.com",
    "*.softarchiv.com",
)

_extra_insecure = os.environ.get("MEDIAFORGE_TLS_INSECURE_HOSTS", "")
if _extra_insecure:
    TLS_INSECURE_HOSTS = TLS_INSECURE_HOSTS + tuple(
        h.strip().lower() for h in _extra_insecure.split(",") if h.strip()
    )


def is_tls_insecure_host(url_or_host):
    """True if *url_or_host* is one of our own hosts on TLS_INSECURE_HOSTS.

    Accepts either a full URL or a bare hostname. Matching is glob-style
    (``*.softarchiv.com``) and case-insensitive; a plain http:// URL is never
    "insecure" in this sense (there is no certificate to skip).
    """
    import fnmatch
    from urllib.parse import urlsplit

    value = str(url_or_host or "").strip()
    if not value:
        return False
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme != "https":
            return False
        host = parts.hostname or ""
    else:
        host = value
    host = host.lower()
    return any(fnmatch.fnmatch(host, pattern) for pattern in TLS_INSECURE_HOSTS)


def insecure_ssl_context_for(url):
    """The SSLContext to use for *url* at the urllib egress points
    (``urllib.request.urlopen(..., context=...)``) -- the module store and the
    mpv auto-download.

    - One of our own hosts (TLS_INSECURE_HOSTS): a context with verification
      switched off, so a lapsed certificate on our own infrastructure can't take
      the Modulmanager/Dev Infos/mpv download offline.
    - Anything else: the OS trust store (via truststore) when available -- the
      same certificate store the browser uses, which validates chains an ageing
      certifi bundle would wrongly reject as expired. Verification stays fully
      ON here; this only fixes *which* roots are trusted.
    - truststore missing: None, i.e. Python's default certifi-based context.

    Callers pass the result straight through: None simply means "your default".
    """
    if not is_tls_insecure_host(url):
        return _os_trust_store_context()

    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    logger.debug("TLS verification skipped for first-party host: %s", url)
    return ctx


def _make_session(resolver=None):
    """Create a new niquests Session with the given DoH resolver (or default Google DoH)."""
    kwargs = {"headers": _DEFAULT_HEADERS}
    if resolver == "system":
        kwargs["resolver"] = None
    else:
        kwargs["resolver"] = resolver if resolver is not None else ["doh+google://"]
    sess = Session(**kwargs)
    sess.timeout = _DEFAULT_TIMEOUT
    return sess


class _SessionProxy:
    """
    Thread-local HTTP session pool.

    Each thread gets its own niquests.Session so concurrent workers never
    share mutable session state. rebuild_global_session() stores the new
    resolver and invalidates the current thread's session; other threads
    lazily recreate their session on next use.
    """

    def __init__(self, resolver=None):
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_local", threading.local())

    def _get_session(self):
        local = object.__getattribute__(self, "_local")
        resolver = object.__getattribute__(self, "_resolver")
        if not hasattr(local, "session") or getattr(local, "session_resolver", None) != resolver:
            local.session = _make_session(resolver)
            local.session_resolver = resolver
        return local.session

    def _swap(self, resolver):
        """Update the resolver and drop this thread's session so it is recreated on next use."""
        object.__setattr__(self, "_resolver", resolver)
        local = object.__getattribute__(self, "_local")
        if hasattr(local, "session"):
            del local.session

    # -- Site-mirror failover -------------------------------------------------
    # Every request for one of the scraper sites (s.to, aniworld.to, ...) is
    # routed through mediaforge.mirrors, which rewrites the host to whichever
    # mirror of that site is currently healthy and walks the rest of the list
    # if it isn't (e.g. s.to -> serienstream.to -> the bare origin IP). URLs
    # for anything else (TMDB, hosters, DoH endpoints, ...) pass through
    # untouched. See mirrors.py.
    def request(self, method, url, **kwargs):
        from .mirrors import request_with_failover
        # Our own hosts (Dev Info feed, module store) are exempt from
        # certificate verification -- see TLS_INSECURE_HOSTS above. An explicit
        # verify= from the caller always wins.
        if "verify" not in kwargs and is_tls_insecure_host(url):
            kwargs["verify"] = False
        return request_with_failover(self._get_session(), method, url, **kwargs)

    def get(self, url, **kwargs):
        kwargs.setdefault("allow_redirects", True)
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def head(self, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._get_session(), name)

    def __setattr__(self, name, value):
        setattr(self._get_session(), name, value)

    def __repr__(self):
        return repr(self._get_session())


GLOBAL_SESSION = _SessionProxy()


def rebuild_global_session(resolver=None):
    """
    Switch to a different DoH resolver.

    Pass a list of resolver URLs (e.g. ``["doh+cloudflare://"]``) or
    ``None`` to go back to the default (Google DoH).

    Each thread will recreate its session on next use with the new resolver.
    Used by: ``web/dns_patch.py`` when the user changes the DNS setting.
    """
    GLOBAL_SESSION._swap(resolver)
    logger.debug(f"GLOBAL_SESSION rebuilt with resolver={resolver!r}")


# -----------------------------
# Active DNS state (shared across egress points)
# -----------------------------
# The niquests GLOBAL_SESSION already routes its DNS through a DoH resolver.
# Subprocesses (e.g. the captcha Chromium) do NOT inherit Python's patched
# socket.getaddrinfo, so they must be told about the project DNS separately.
# These templates map our DoH presets onto Chromium's --dns-over-https-* flags.
_CHROMIUM_DOH_TEMPLATES = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google":     "https://dns.google/dns-query",
    "quad9":      "https://dns.quad9.net/dns-query",
}

# Current DNS mode: "system" | "cloudflare" | "google" | "quad9" | "custom".
ACTIVE_DNS_MODE = "system"


def set_active_dns_mode(mode):
    """Record the active DNS mode so non-niquests egress points can mirror the
    same DNS as GLOBAL_SESSION."""
    global ACTIVE_DNS_MODE
    ACTIVE_DNS_MODE = mode or "system"


# IP-form DoH templates: using the resolver IP (which is in the cert SAN) rather
# than its hostname means Chromium does NOT have to bootstrap the DoH server name
# through the OS/ISP resolver first.
_CHROMIUM_DOH_IP_TEMPLATES = {
    "cloudflare": "https://1.1.1.1/dns-query",
    "google":     "https://8.8.8.8/dns-query",
    "quad9":      "https://9.9.9.9/dns-query",
}

# DoH JSON ("application/dns-json") endpoints used to resolve the ISP-blocked
# site hosts in-process, through the SAME project DoH that already works for
# niquests/yt-dlp -- never the ISP resolver.
_DOH_JSON_ENDPOINTS = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google":     "https://dns.google/resolve",
    "quad9":      "https://dns.quad9.net:5053/dns-query",
}

# Hosts that German ISPs (CUII) DNS-block and that the captcha browser must
# reach directly.  These are pinned with --host-resolver-rules so Chromium uses
# the DoH-resolved IP and never queries the ISP resolver for them.
_CHROMIUM_MAP_HOSTS = (
    "s.to", "www.s.to",
    "serienstream.to", "www.serienstream.to",
    "aniworld.to", "www.aniworld.to",
    "filmpalast.to", "www.filmpalast.to",
    "megakino.to", "www.megakino.to",
)

def _chromium_map_hosts():
    """The hosts to pin, including every configured mirror domain (see
    mirrors.py) — so the captcha browser can reach a fallback domain
    (serienstream.to, ...) on the project DNS too, not just the primary one.
    Falls back to the static tuple above if the mirror registry is unavailable.
    """
    try:
        from .mirrors import all_hosts
        hosts = all_hosts()
    except Exception:
        hosts = ()
    return tuple(dict.fromkeys(tuple(_CHROMIUM_MAP_HOSTS) + tuple(hosts)))


_CHROMIUM_MAP_LOCK = threading.Lock()
_CHROMIUM_MAP_CACHE = {"mode": None, "ts": 0.0, "rules": []}
_CHROMIUM_MAP_TTL = 600  # re-resolve the pinned hosts at most every 10 minutes


def _looks_like_ipv4(value):
    parts = str(value).split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def _doh_resolve_a(hostname, endpoint):
    """Resolve *hostname* to an IPv4 string via the given DoH JSON *endpoint*.

    Uses GLOBAL_SESSION, which itself resolves through the project DoH, so this
    lookup never touches the ISP resolver.  Best-effort: returns None on any
    failure (the host then falls back to Chromium\'s own DoH switches).
    """
    try:
        resp = GLOBAL_SESSION.get(
            endpoint,
            params={"name": hostname, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=(4, 6),
        )
        for ans in (resp.json().get("Answer") or []):
            if ans.get("type") == 1:  # A record
                ip = str(ans.get("data", "")).strip()
                if _looks_like_ipv4(ip):
                    return ip
    except Exception:
        pass
    return None


def _chromium_host_map_rules():
    """Build (and cache) the --host-resolver-rules MAP entries for the blocked
    site hosts, resolved via the active project DoH."""
    import time as _time
    endpoint = _DOH_JSON_ENDPOINTS.get(ACTIVE_DNS_MODE)
    if not endpoint:
        return []
    with _CHROMIUM_MAP_LOCK:
        now = _time.monotonic()
        cache = _CHROMIUM_MAP_CACHE
        if (cache["mode"] == ACTIVE_DNS_MODE and cache["rules"]
                and now - cache["ts"] < _CHROMIUM_MAP_TTL):
            return list(cache["rules"])
        rules = []
        for host in _chromium_map_hosts():
            ip = _doh_resolve_a(host, endpoint)
            if ip:
                rules.append("MAP %s %s" % (host, ip))
        if rules:  # cache only a usable result; retry next launch otherwise
            cache["mode"] = ACTIVE_DNS_MODE
            cache["ts"] = now
            cache["rules"] = list(rules)
        return rules


def chromium_dns_args():
    """Chromium args that force the captcha browser onto the project DNS.

    Used by: ``playwright/captcha.py`` when launching the captcha browser.

    The DoH command-line switches alone are unreliable: in "secure" mode
    Chromium still bootstraps the DoH server *hostname* via the OS/ISP resolver,
    and some builds/profiles ignore the switch entirely -- so the browser
    silently falls back to the ISP resolver and hits the ISP block, even though
    in-process DoH (niquests/yt-dlp) works.  We therefore also resolve the
    ISP-blocked site hosts here through the same project DoH and pin them with
    --host-resolver-rules, so Chromium never asks the ISP resolver for them.

    Only the DoH presets can be mapped onto Chromium; "system"/"custom" modes
    return no args (matching the niquests fallback to system DNS).
    """
    # Only the DoH presets can be resolved via the project DoH JSON API.
    if ACTIVE_DNS_MODE not in _DOH_JSON_ENDPOINTS:
        return []
    # Pin ONLY the ISP-blocked site hosts to their DoH-resolved IPs.  We do NOT
    # force global secure DoH on the browser: every other host (Cloudflare
    # Turnstile, gstatic, ...) resolves via the normal OS resolver, exactly like
    # a normal browser.  Forcing secure DoH could break Turnstile token issuance
    # on networks where DoH is flaky/filtered while adding nothing here -- the
    # only hosts that must bypass the ISP resolver are already pinned below.
    rules = _chromium_host_map_rules()
    if rules:
        return ["--host-resolver-rules=" + ",".join(rules)]
    return []


# Set once curl_cffi's Curl.perform has been wrapped to inject DoH.
_CURL_CFFI_PATCHED = False


def ensure_curl_cffi_doh():
    """Route the curl_cffi / libcurl backend (used by yt-dlp's ``impersonate``
    downloads, e.g. VeeV) through the project DoH server.

    Used by: ``models/common/common.py`` before starting an impersonated
    download.

    libcurl resolves host names in C and ignores Python's patched
    socket.getaddrinfo, so the only way to keep impersonated downloads on the
    project DNS is libcurl's native DoH support (CURLOPT_DOH_URL).  We wrap
    Curl.perform so the DoH URL is (re)applied on every transfer and follows
    later DNS-mode changes.  Idempotent and best-effort (no-op if curl_cffi is
    absent or the active mode has no DoH template, e.g. system/custom).
    """
    global _CURL_CFFI_PATCHED
    if _CURL_CFFI_PATCHED:
        return
    try:
        from curl_cffi import Curl
        from curl_cffi.const import CurlOpt
    except Exception:
        return  # curl_cffi not installed — impersonate path unused

    _orig_perform = Curl.perform

    def _perform_with_doh(self, *args, **kwargs):
        template = _CHROMIUM_DOH_TEMPLATES.get(ACTIVE_DNS_MODE)
        if template:
            try:
                self.setopt(CurlOpt.DOH_URL, template)
            except Exception:
                pass
        return _orig_perform(self, *args, **kwargs)

    Curl.perform = _perform_with_doh
    _CURL_CFFI_PATCHED = True
    logger.debug("curl_cffi Curl.perform wrapped for project DoH")


logger.debug("Config initialized successfully")

# -----------------------------
# Provider Stuff
# -----------------------------
# Hosters actually offered to users. The commented-out names below have a
# working extractor under extractors/provider/ but are intentionally left
# disabled here (e.g. unreliable or superseded) -- re-enable by uncommenting.
SUPPORTED_PROVIDERS = (
    "VOE",
    "Vidmoly",
    "Vidoza",
    "VeeV",
    "Vidara",
    "Vidavaca",
    # "Doodstream",
    # "Filemoon",
    # "LoadX",
    # "Luluvdo",
    # "Streamtape",
)

PROVIDER_HEADERS_D = {
    "Vidmoly": {"Referer": "https://vidmoly.biz"},
    "Vidara": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://vidara.so/"},
    "Vidavaca": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://vidavaca.net/"},
    "Doodstream": {"Referer": "https://dood.li/"},
    "VOE": {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Referer": "https://voe.sx/",
        "Origin": "https://voe.sx",
    },
    "LoadX": {"Accept": "*/*"},
    "Filemoon": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://filemoon.to"},
    "Luluvdo": {
        "User-Agent": LULUVDO_USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://luluvdo.com",
        "Referer": "https://luluvdo.com/",
    },
    "VeeV": {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://veev.to/",
        "Origin": "https://veev.to",
    },
}

PROVIDER_HEADERS_W = {
    "Vidmoly": {"Referer": "https://vidmoly.biz"},
    "Vidara": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://vidara.so/"},
    "Vidavaca": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://vidavaca.net/"},
    "Doodstream": {"Referer": "https://dood.li/"},
    "VOE": {"User-Agent": DEFAULT_USER_AGENT},
    "Luluvdo": {"User-Agent": LULUVDO_USER_AGENT},
    "Filemoon": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://filemoon.to"},
    "VeeV": {"User-Agent": DEFAULT_USER_AGENT,"Referer": "https://veev.to/"},
}


# -----------------------------
# Language Stuff
# -----------------------------
class Audio(Enum):
    """
    Available audio language options:

        - JAPANESE: Japanese dubbed audio
        - GERMAN:   German dubbed audio
        - ENGLISH:  English dubbed audio

    Required source for each option:

        Japanese Dub -> Source: German Sub, English Sub
        German Dub   -> Source: German Dub
        English Dub  -> Source: English Dub
    """

    JAPANESE = "Japanese"
    GERMAN = "German"
    ENGLISH = "English"


class Subtitles(Enum):
    """
    Available subtitle language options:

        - NONE:    No subtitles
        - GERMAN:  German subtitles
        - ENGLISH: English subtitles

    Required source for each option:

        German Sub   -> Source: German Sub
        English Sub  -> Source: English Sub
    """

    NONE = "None"
    GERMAN = "German"
    ENGLISH = "English"


# Map site-specific language keys to semantic meaning
LANG_KEY_MAP = {
    "1": (Audio.GERMAN, Subtitles.NONE),  # German Dub
    "2": (Audio.JAPANESE, Subtitles.ENGLISH),  # English Sub
    "3": (Audio.JAPANESE, Subtitles.GERMAN),  # German Sub
    "4": (Audio.ENGLISH, Subtitles.NONE),  # English Dub
}

LANG_LABELS = {
    "1": "German Dub",
    "2": "English Sub",
    "3": "German Sub",
    "4": "English Dub",
}

LANG_CODE_MAP = {
    Audio.ENGLISH: "eng",
    Audio.GERMAN: "deu",
    Audio.JAPANESE: "jpn",
    Subtitles.ENGLISH: "eng",
    Subtitles.GERMAN: "deu",
    Subtitles.NONE: None,
}


INVERSE_LANG_KEY_MAP = {v: k for k, v in LANG_KEY_MAP.items()}
INVERSE_LANG_LABELS = {v: k for k, v in LANG_LABELS.items()}

# -----------------------------
# Patterns
# -----------------------------


MEDIAFORGE_SERIES_PATTERN = re.compile(
    r"^https?://(www\.)?aniworld\.to/anime/stream/[a-zA-Z0-9\-]+/?$", re.IGNORECASE
)

# series slug + (/staffel-N or /filme)
MEDIAFORGE_SEASON_PATTERN = re.compile(
    r"^https?://(www\.)?aniworld\.to/anime/stream/"
    r"[a-zA-Z0-9\-]+/"
    r"(staffel-\d+|filme)"
    r"/?$",
    re.IGNORECASE,
)

MEDIAFORGE_EPISODE_PATTERN = re.compile(
    r"^https?://(www\.)?aniworld\.to/anime/stream/"
    r"[a-zA-Z0-9\-]+/"  # series slug
    r"(staffel-\d+/episode-\d+|"  # season/episode
    r"filme/film-\d+)"  # movie/film
    r"/?$",
    re.IGNORECASE,
)

SERIENSTREAM_SERIES_PATTERN = re.compile(
    r"^https?://(www\.)?(serienstream|s)\.to/serie/[a-zA-Z0-9\-]+/?$", re.IGNORECASE
)

SERIENSTREAM_SEASON_PATTERN = re.compile(
    r"^https?://(www\.)?(serienstream|s)\.to/serie/"
    r"[a-zA-Z0-9\-]+/"
    r"staffel-\d+"
    r"/?$",
    re.IGNORECASE,
)

SERIENSTREAM_EPISODE_PATTERN = re.compile(
    r"^https?://(www\.)?(serienstream|s)\.to/serie/"
    r"[a-zA-Z0-9\-]+/"
    r"staffel-\d+/episode-\d+"
    r"/?$",
    re.IGNORECASE,
)

# -----------------------------
# MegaKino (megakino.to)
# -----------------------------
# megakino.to is a React SPA backed by a JSON API. Content lives at
# /watch/<slug>/<24-hex-id>; movies and series share that URL form (the media
# type is decided by the API's ``tv`` field). Episodes use a synthetic
# ``…?episode=<n>`` URL. The base URL is overridable and the patterns match any
# host containing "megakino".
MEGAKINO_BASE_URL = os.environ.get("MEGAKINO_BASE_URL", "https://megakino.to").rstrip("/")

# Movie / series landing (no query): /watch/<slug>/<hexid>
MEGAKINO_MOVIE_PATTERN = re.compile(
    r"^https?://[^/]*megakino[^/]*/watch/[^/?#]+/[a-f0-9]{24}$",
    re.IGNORECASE,
)

# Series and movies share the same landing URL form.
MEGAKINO_SERIES_PATTERN = MEGAKINO_MOVIE_PATTERN

# Synthetic single-episode URL: <watch-post>?episode=<n>
MEGAKINO_EPISODE_PATTERN = re.compile(
    r"^https?://[^/]*megakino[^/]*/watch/[^/?#]+/[a-f0-9]{24}\?episode=\d+$",
    re.IGNORECASE,
)

# -----------------------------
# hanime.tv (adult / 18+)  -- DISABLED by default, gated in the UI
# -----------------------------
# Base + API endpoints are overridable via env because hanime occasionally
# moves its search host.  Everything hanime-specific that touches the network
# lives in models/hanime_tv/scraper.py -- these patterns only classify URLs.
HANIME_BASE_URL = os.environ.get("HANIME_BASE_URL", "https://hanime.tv").rstrip("/")
HANIME_API_BASE = os.environ.get("HANIME_API_BASE", "https://hanime.tv/api/v8").rstrip("/")
HANIME_SEARCH_URL = os.environ.get("HANIME_SEARCH_URL", "https://search.htv-services.com/")

# A "series" is a franchise, represented by one of its video slugs:
#   https://hanime.tv/videos/hentai/<slug>
HANIME_SERIES_PATTERN = re.compile(
    r"^https?://hanime\.tv/videos/hentai/[a-zA-Z0-9._\-]+/?$",
    re.IGNORECASE,
)

# Synthetic single-episode URL: <series-slug>?ep=<n>  (n = 1-based index into
# the franchise's ordered video list).
HANIME_EPISODE_PATTERN = re.compile(
    r"^https?://hanime\.tv/videos/hentai/[a-zA-Z0-9._\-]+\?ep=\d+$",
    re.IGNORECASE,
)

# -----------------------------
# Directories
# -----------------------------

# TODO: add many other directories and use them throughout the app

# Determine mpv scripts directory
# On Linux/macOS: ~/.config/mpv/scripts
# On Windows: %APPDATA%\mpv\scripts
if os.name == "nt":
    MPV_CONFIG_DIR = Path(os.getenv("APPDATA")) / "mpv"
else:
    MPV_CONFIG_DIR = Path.home() / ".config" / "mpv"

MPV_SCRIPTS_DIR = MPV_CONFIG_DIR / "scripts"
