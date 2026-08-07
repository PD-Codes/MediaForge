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
import tempfile
import threading
import time
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlparse as _urlparse

import fake_useragent
from niquests import RequestException, Session
from packaging.version import parse as parse_version

from .env import prepare_env
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
#
# Overridable so a test run (or a second instance) can be pointed at a scratch
# directory instead of the real database, secret key and image cache.
_cfg_dir_override = os.environ.get("MEDIAFORGE_CONFIG_DIR", "").strip()
MEDIAFORGE_CONFIG_DIR = (
    Path(_cfg_dir_override).expanduser() if _cfg_dir_override
    else Path.home() / ".mediaforge"
)

# Scratch directory for intermediate work files: yt-dlp raw downloads, ffmpeg
# tagging passes, and the encode/upscale temp outputs the web workers write
# before moving the finished file to its destination. Lives on the OS temp
# volume (main drive) so a slow network target never sees partial files.
# Defined here -- and not in models/common/common.py -- because both the CLI
# download path and web/encoding_worker.py + web/upscale_worker.py need it,
# and importing common.py from the web workers would drag in the whole
# extractor stack.
MEDIAFORGE_TEMP_DIR = Path(tempfile.gettempdir()) / "mediaforge"

# Mirror legacy ANIWORLD_* variables, and load a not-yet-imported .env once,
# whenever config is imported. Configuration itself lives in the app_settings
# DB table -- see web/settings_migration.py.
prepare_env(MEDIAFORGE_CONFIG_DIR / ".env")

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
# We are not the only ones who can break this, though -- and the other
# direction is what actually reached users. If *something else in the
# interpreter* injected a subclass into ``ssl.SSLContext`` before truststore
# was imported (a sitecustomize.py or a .pth file, as corporate TLS-inspection
# tooling likes to install), then truststore's captured
#     _original_super_SSLContext = super(ssl.SSLContext, ssl.SSLContext)
# no longer resolves to the C-level descriptor in ``_ssl._SSLContext`` but to
# the Python property in ``ssl.py``, whose setter reads the module global
# ``SSLContext`` at call time -- now the subclass -- and calls itself. Every
# TLS handshake through truststore then dies with
#     RecursionError: maximum recursion depth exceeded
# which reached the Modulmanager as "store unreachable" with nothing to act on.
#
# We cannot fix that interpreter, but we do not have to be taken down by it:
# certifi is a perfectly good fallback for our three urllib egress points, and
# it is what we would have used had truststore not been installed at all. So
# the state is detected once, reported once, and then routed around.
_TRUSTSTORE_UNSAFE_REASON = None
_truststore_checked = False


def _truststore_is_safe():
    """Whether truststore can be used in this interpreter without recursing.

    Answered once and cached: the check imports ssl/truststore and compares
    identities, which is cheap, but the warning must not be logged per request.
    """
    global _truststore_checked, _TRUSTSTORE_UNSAFE_REASON
    if _truststore_checked:
        return _TRUSTSTORE_UNSAFE_REASON is None
    _truststore_checked = True
    try:
        import ssl

        from truststore import _ssl_constants
    except Exception as exc:  # truststore not installed -> certifi, silently
        _TRUSTSTORE_UNSAFE_REASON = "truststore unavailable (%s)" % exc
        return False

    original = getattr(_ssl_constants, "_original_SSLContext", None)
    if original is not None and ssl.SSLContext is not original:
        # Someone replaced ssl.SSLContext. Whether truststore itself did it
        # (inject_into_ssl) or another library did, the property chain above is
        # now recursive.
        _TRUSTSTORE_UNSAFE_REASON = (
            "ssl.SSLContext has been replaced by %r; something in this Python "
            "installation injected a subclass into the ssl module (commonly a "
            ".pth file or sitecustomize.py from TLS-inspection tooling). "
            "Falling back to the bundled certifi trust store for the module "
            "store, the Dev Info feed and the mpv download. Certificate "
            "verification stays ON." % (ssl.SSLContext,)
        )
        try:
            from .logger import get_logger

            get_logger(__name__).warning("[TLS] %s", _TRUSTSTORE_UNSAFE_REASON)
        except Exception:
            pass
        return False

    # Belt and braces: prove the write path terminates instead of trusting the
    # identity check to have covered every way this can be broken. A throwaway
    # context, one property write, RecursionError caught rather than raised
    # through a store fetch.
    try:
        probe = original(ssl.PROTOCOL_TLS_CLIENT)
        probe.check_hostname = False
        _ssl_constants._set_ssl_context_verify_mode(probe, ssl.CERT_NONE)
    except RecursionError:
        # Note this is NOT covered by the identity check above: when the
        # injection happened *before* truststore was imported, truststore
        # captured the injected subclass AS the original, so the two compare
        # equal and only an actual write reveals the recursion. The observed
        # real-world cause is the pip-system-certs package, whose
        # pip_system_certs.pth injects pip._vendor.truststore into ssl at
        # interpreter startup.
        _TRUSTSTORE_UNSAFE_REASON = (
            "truststore's verify_mode write recurses in this interpreter: "
            "ssl.SSLContext is %r, i.e. a subclass was injected into the ssl "
            "module before truststore was imported (the usual cause is the "
            "pip-system-certs package via its pip_system_certs.pth; check "
            "site-packages/*.pth). Falling back to the bundled certifi trust "
            "store for the module store, the Dev Info feed and the mpv "
            "download -- certificate verification stays ON. NOTE this only "
            "covers those three; urllib3-future builds its own context on "
            "every connection and recurses the same way, so GLOBAL_SESSION "
            "and the DoH resolver stay at risk until the injection is removed."
            % (ssl.SSLContext,)
        )
        try:
            from .logger import get_logger

            get_logger(__name__).warning("[TLS] %s", _TRUSTSTORE_UNSAFE_REASON)
        except Exception:
            pass
        return False
    except Exception:
        # Any other failure here says nothing about the recursion bug (e.g. a
        # platform that rejects this particular combination) -- do not disable
        # truststore over it.
        pass
    return True


def truststore_unsafe_reason():
    """Why truststore is being bypassed, or None when it is in use.

    Used by: web/routes/settings.py's DNS diagnostics, so the reason is
    visible in the UI rather than only in the log.
    """
    _truststore_is_safe()
    return _TRUSTSTORE_UNSAFE_REASON


def _os_trust_store_context():
    """An SSLContext validating against the OS trust store, or None if
    truststore isn't installed or cannot be used safely here."""
    if not _truststore_is_safe():
        return None
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


# -----------------------------
# TLS at the urllib egress points
# -----------------------------
# There used to be a TLS_INSECURE_HOSTS allowlist here that turned certificate
# verification OFF (check_hostname=False, CERT_NONE) for our own domains, so a
# lapsed certificate on our infrastructure could not take the module store,
# the Dev Info feed or the mpv download offline.
#
# That was the wrong trade. CERT_NONE accepts ANY certificate, including a
# self-signed one from whoever sits between the user and us -- and exactly
# those hosts carry code: the mpv binary is downloaded and later executed, and
# the store index decides what is offered for installation. An expired
# certificate on our side is an operations problem and has to be fixed there,
# not worked around on every user's machine.
#
# Verification is therefore always on. The only thing decided here is *which*
# root store validates the chain.


def ssl_context_for(url):  # noqa: ARG001 - url kept for call-site symmetry
    """The SSLContext for the urllib egress points (module store, mpv download).

    Returns the OS trust store (via truststore) when available -- the same
    certificate store the browser uses, which validates chains an ageing
    certifi bundle would wrongly reject as expired -- otherwise None, i.e.
    Python's default certifi-based context. Callers pass the result straight
    through: None simply means "your default". Verification is never disabled.
    """
    return _os_trust_store_context()


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


# A DNS answer the configured resolver could not produce. niquests raises this
# as a plain ConnectionError whose message carries the resolver's wording, so
# the string is what we have to go on -- there is no dedicated exception type
# that survives the urllib3-future -> niquests translation.
#
# Matched loosely on purpose (any of the fragments is enough): the exact
# phrasing differs between "Failed to resolve 'x'", "Name or service not known"
# and "NameResolutionError", and a fragment that stops matching after a
# niquests update would silently disable the fallback below rather than break
# anything loudly.
_DNS_FAILURE_FRAGMENTS = (
    "nameresolutionerror",
    "failed to resolve",
    "name or service not known",
    "temporary failure in name resolution",
)


def _looks_like_dns_failure(exc) -> bool:
    """True if *exc* is "I could not turn that hostname into an address"."""
    text = f"{exc}".lower()
    return any(frag in text for frag in _DNS_FAILURE_FRAGMENTS)


# Hosts the project resolver has just failed on -> when that was noticed.
# Once a host is in here every further request for it goes straight to the
# system resolver until the entry expires, instead of paying the failing DoH
# lookup again on every single request. Three reasons that matters:
#
#   * cost -- a browse page or a download does dozens of requests to the same
#     host, and each one was eating a full resolver timeout before falling back;
#   * noise -- the warning was written once per request, which buried the log;
#   * consistency -- a multi-request flow (fetch page, POST token) stayed on
#     one session instead of alternating between two.
#
# Expires so a temporary DoH outage does not permanently retire the project
# resolver: after the window the next request tries it again, and re-arms this
# entry only if it fails again.
_DNS_FALLBACK_TTL = 600.0
_dns_fallback_lock = threading.Lock()
_dns_fallback_hosts: dict = {}


def _host_of(url):
    try:
        return (_urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _dns_fallback_active(host) -> bool:
    """True while *host* is known to be unresolvable by the project resolver."""
    if not host:
        return False
    with _dns_fallback_lock:
        seen = _dns_fallback_hosts.get(host)
        if seen is None:
            return False
        if (time.monotonic() - seen) > _DNS_FALLBACK_TTL:
            _dns_fallback_hosts.pop(host, None)
            return False
        return True


def _mark_dns_fallback(host) -> bool:
    """Record *host* as needing the fallback. Returns True the first time, so
    the caller logs once per host per window rather than once per request."""
    if not host:
        return True
    with _dns_fallback_lock:
        first = host not in _dns_fallback_hosts
        _dns_fallback_hosts[host] = time.monotonic()
    return first


def dns_fallback_hosts() -> list:
    """Hosts currently served through the system resolver instead of the
    project one. Read-only; exposed for the DNS diagnostics UI."""
    with _dns_fallback_lock:
        now = time.monotonic()
        return sorted(h for h, ts in _dns_fallback_hosts.items()
                      if (now - ts) <= _DNS_FALLBACK_TTL)


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

    def _get_system_session(self):
        """A second thread-local session that uses the OS resolver.

        Only ever used by the DNS fallback in :meth:`request`. Built lazily so
        an install whose DoH works never pays for it.

        It SHARES the primary session's cookie jar, and that is not an
        optimisation -- it is the whole reason this works. Several scrapers run
        a multi-request flow that only holds together because every step looks
        like the same browser: filmo.to fetches a movie page (which sets a
        Laravel session + XSRF cookie and hands out a CSRF token), then POSTs
        that token to /n. Sending those two requests from two sessions with
        separate jars drops the session cookie and filmo.to answers
        "419 Page Expired". The same applies to any DDoS-Guard / Cloudflare
        clearance cookie earned on an earlier request.
        """
        local = object.__getattribute__(self, "_local")
        if not hasattr(local, "system_session"):
            session = _make_session("system")
            try:
                session.cookies = self._get_session().cookies
            except Exception:
                # Never let cookie sharing be the thing that stops the
                # fallback: a jar of its own still beats no request at all.
                logger.debug("[DNS] Could not share the cookie jar with the fallback session",
                             exc_info=True)
            local.system_session = session
        return local.system_session

    def _swap(self, resolver):
        """Update the resolver and drop this thread's session so it is recreated on next use."""
        object.__setattr__(self, "_resolver", resolver)
        # A different resolver deserves a clean slate: hosts the OLD one could
        # not resolve say nothing about the new one, and keeping them pinned to
        # the system resolver would quietly ignore the setting the user just
        # changed.
        with _dns_fallback_lock:
            _dns_fallback_hosts.clear()
        local = object.__getattribute__(self, "_local")
        if hasattr(local, "session"):
            del local.session
        # The system-resolver fallback is dropped as well: it holds its own
        # connection pool, and keeping it across a DNS-mode change would let a
        # connection opened under the old settings survive the switch.
        if hasattr(local, "system_session"):
            del local.system_session

    # -- Site-mirror failover -------------------------------------------------
    # Every request for one of the scraper sites (s.to, aniworld.to, ...) is
    # routed through mediaforge.mirrors, which rewrites the host to whichever
    # mirror of that site is currently healthy and walks the rest of the list
    # if it isn't (e.g. s.to -> serienstream.to -> the bare origin IP). URLs
    # for anything else (TMDB, hosters, DoH endpoints, ...) pass through
    # untouched. See mirrors.py.
    def request(self, method, url, **kwargs):
        from .mirrors import request_with_failover
        # No host is exempt from certificate verification here any more; the
        # former first-party allowlist silently turned verify=False on for the
        # Dev Info feed and the module store. An explicit verify= from the
        # caller still wins, as niquests intends.
        resolver = object.__getattribute__(self, "_resolver")
        # Known-bad host: skip the lookup that is going to fail anyway.
        if resolver != "system" and _dns_fallback_active(_host_of(url)):
            return request_with_failover(self._get_system_session(), method, url, **kwargs)
        try:
            return request_with_failover(self._get_session(), method, url, **kwargs)
        except Exception as exc:
            # The configured resolver could not resolve the host at all. That
            # is a different class of problem from "the site is down": the
            # request never left the machine, every mirror in the list failed
            # for the same reason, and retrying the same way can only fail
            # again. Before this, such a failure was terminal -- a DoH
            # provider that is blocked, filtered or simply cannot answer for
            # one domain took the whole source offline with a bare
            # "NameResolutionError", and nothing in the app ever tried the
            # resolver the rest of the machine uses successfully.
            #
            # So: exactly one retry through the OS resolver. It is a
            # last-resort widening, not a silent downgrade -- it happens only
            # after the project resolver already failed, and it is logged at
            # WARNING with the host, because it also means this particular
            # request did NOT go around a possible ISP-level DNS block (which
            # is the reason the DoH resolver exists in the first place).
            if resolver == "system" or not _looks_like_dns_failure(exc):
                raise
            host = _host_of(url) or url
            if _mark_dns_fallback(host):
                # Reported once per host per window, same as the log line, and
                # WITHOUT the host: which site the user was visiting is not the
                # point, "the configured resolver failed somewhere" is. Lazy
                # import + best-effort because config must not depend on the
                # telemetry package at import time (telemetry imports config).
                try:
                    from .telemetry import client as _tel_client
                    from .telemetry import events as _tel_events
                    _tel_client.submit(_tel_events.build_network_detail_event("dns_fallback"))
                except Exception:
                    pass
                logger.warning(
                    "[DNS] Project resolver could not resolve %s (%s) -- using "
                    "the system resolver for this host for the next %d minutes. "
                    "If this repeats, check Settings -> Network -> DNS.",
                    host, type(exc).__name__, int(_DNS_FALLBACK_TTL // 60),
                )
            return request_with_failover(self._get_system_session(), method, url, **kwargs)

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
    "filmo.to", "www.filmo.to",
    "9anime.or.at", "www.9anime.or.at",
    "aniwaves.ru", "www.aniwaves.ru",
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
#
# A *list*, not a tuple: extractors/__init__.py's register_hoster() appends a
# third-party hoster's name here in place (list.append(), never reassigned),
# so every existing `from .config import SUPPORTED_PROVIDERS` import -- which
# binds to this exact list object -- sees the addition immediately. Iterating
# it (`for p in SUPPORTED_PROVIDERS`) or checking membership behaves exactly
# like it did as a tuple; nothing else needed to change.
SUPPORTED_PROVIDERS = [
    "VOE",
    "Vidmoly",
    "Vidoza",
    "VeeV",
    "Vidara",
    "Vidavaca",
    "Megaplay",
    "EchoVideo",
    "OneAnime",
    # "Doodstream",
    # "Filemoon",
    # "LoadX",
    # "Luluvdo",
    # "Streamtape",
]

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
    # The resolved master.m3u8 needs only a Referer -- unlike VeeV, no
    # session cookies are bound to it (verified: fetchable with plain HTTP
    # once resolved). See extractors/provider/megaplay.py.
    "Megaplay": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://megaplay.buzz/"},
    # OneAnime (9anime.or.at's own player on my.1anime.site): /stream/<hash>
    # answers 403 without a Referer, and it wants the PLAYER's origin, not
    # 9anime's -- the embed page is the one that wants the 9anime Referer (set
    # separately in extractors/provider/oneanime.py). Verified live: with this
    # header the stream 302s to the .mp4, without it the CDN returns 403.
    "OneAnime": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://my.1anime.site/"},
    # EchoVideo (aniwaves.ru's "Vidplay" server): resolved master.m3u8 needs
    # no auth at all (verified: fetchable with no Referer/cookies once
    # resolved) -- Referer kept here anyway for consistency/future-proofing.
    # See extractors/provider/echovideo.py.
    "EchoVideo": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://aniwaves.ru/"},
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
    "Megaplay": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://megaplay.buzz/"},
    "OneAnime": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://my.1anime.site/"},
    "EchoVideo": {"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://aniwaves.ru/"},
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
# Filmo (filmo.to) -- movies only
# -----------------------------
# filmo.to is a server-rendered (Laravel) site. A movie page embeds one
# "provider chip" per (language, hoster) pair, each carrying an encrypted
# ``data-p`` payload instead of a hoster URL -- the real embed only comes back
# after POSTing that payload to ``urls.openMint`` (``/n``) and following the
# short-lived ``/n/<token>`` redirect it mints. See models/filmo_to/scraper.py.
FILMO_BASE_URL = os.environ.get("FILMO_BASE_URL", "https://filmo.to").rstrip("/")

# Movie landing page: /movies/<slug>
FILMO_MOVIE_PATTERN = re.compile(
    r"^https?://(www\.)?filmo\.to/movies/[a-zA-Z0-9\-]+/?$",
    re.IGNORECASE,
)

# -----------------------------
# 9anime (9anime.or.at) -- English-only, DISABLED by default (same UI gate as
# hanime.tv below -- not adult content, but a fansub/dub clone site whose
# catalogue is inconsistent with the DE-first providers, so it should not
# silently start contributing to search results).
# -----------------------------
# A WordPress "9animetv" theme install: series pages are server-rendered, but
# the episode list and per-episode hoster list are both fetched client-side
# from the theme's own endpoints -- see models/nineanime_to/scraper.py.
NINEANIME_BASE_URL = os.environ.get("NINEANIME_BASE_URL", "https://9anime.or.at").rstrip("/")

# Series landing page: /anime/<slug>/
NINEANIME_SERIES_PATTERN = re.compile(
    r"^https?://(www\.)?9anime\.or\.at/anime/[a-zA-Z0-9\-]+/?$",
    re.IGNORECASE,
)

# Synthetic flat episode URL, e.g.
# /solo-leveling-season-2-arise-from-the-shadow-episode-1-english-subbed/
# Requires "-episode-<n>" so this can't accidentally match the site's other
# flat top-level paths (/random, /az-list, /filter, ...).
NINEANIME_EPISODE_PATTERN = re.compile(
    r"^https?://(www\.)?9anime\.or\.at/[a-zA-Z0-9\-]+-episode-\d+[a-zA-Z0-9\-]*/?$",
    re.IGNORECASE,
)

# -----------------------------
# Aniwaves (aniwaves.ru) -- English-only, DISABLED by default (same "off,
# explicit opt-in" gate as 9anime/hanime above), series only, no movies.
# -----------------------------
# A different codebase than 9anime.or.at (client-rendered SPA instead of a
# WordPress theme) but the same overall shape: series/episode pages carry
# only SEO metadata (scraped from a JSON-LD <script> block, not classic HTML
# tags), while the episode list and per-episode hoster ("server") list are
# both fetched from legacy jQuery-style ajax endpoints that work fine without
# JS execution -- see models/aniwaves_ru/scraper.py.
ANIWAVES_BASE_URL = os.environ.get("ANIWAVES_BASE_URL", "https://aniwaves.ru").rstrip("/")

# Series landing page: /watch/<slug>-<id>. The slug is decorative -- the site
# accepts a bare /watch/<id> too (verified: identical response), which lets
# AniwavesEpisode resolve its parent series from an episode URL (which only
# carries the numeric id, see ANIWAVES_EPISODE_PATTERN) without an extra
# lookup fetch just to recover the slug.
ANIWAVES_SERIES_PATTERN = re.compile(
    r"^https?://(www\.)?aniwaves\.ru/watch/(?:[a-zA-Z0-9\-]+-)?\d+/?$",
    re.IGNORECASE,
)

# Episode page: /watch/<id>/ep-<n> (flat/synthetic, same numeric id as the
# series it belongs to -- no separate episode-id lookup needed, unlike
# 9anime's inline `episodeId:` JS var).
ANIWAVES_EPISODE_PATTERN = re.compile(
    r"^https?://(www\.)?aniwaves\.ru/watch/\d+/ep-\d+/?$",
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
# The search backend moved: search.htv-services.com stopped resolving entirely
# (NXDOMAIN from every public resolver, and the remaining htv-services.com apex
# answers Cloudflare 530), and the site's own frontend now calls the endpoint
# below. Overridable, because this is the part most likely to move again -- and
# note that routes/image_proxy.py derives its allowed poster hosts from this
# URL's domain, so pointing it somewhere new also permits that host's CDN.
HANIME_SEARCH_URL = os.environ.get(
    "HANIME_SEARCH_URL", "https://guest.freeanimehentai.net/api/v11/search_hvs"
)

# Hosts that serve this site's artwork. Kept separate from the URLs above
# because the images do NOT live under any of those domains: the catalogue is
# served from freeanimehentai.net while every poster_url/cover_url in it points
# at hanime-cdn.com. routes/image_proxy.py only allows what it can derive from
# the configured base URLs, so without this list every poster is answered with
# a 403 -- titles appear, images do not.
HANIME_IMAGE_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("HANIME_IMAGE_HOSTS", "hanime-cdn.com,htv-services.com").split(",")
    if h.strip()
)

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

# Determine mpv scripts directory
# On Linux/macOS: ~/.config/mpv/scripts
# On Windows: %APPDATA%\mpv\scripts
if os.name == "nt":
    MPV_CONFIG_DIR = Path(os.getenv("APPDATA")) / "mpv"
else:
    MPV_CONFIG_DIR = Path.home() / ".config" / "mpv"

MPV_SCRIPTS_DIR = MPV_CONFIG_DIR / "scripts"
