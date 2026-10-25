"""HLS passthrough proxy for stream-from-source.

Instead of re-muxing/transcoding a provider stream through ffmpeg (which causes
video stutter on variable-frame-rate feeds), this proxies the provider's own
HLS playlists and segments through our server, adding only the required HTTP
headers (Referer / User-Agent). The browser then plays the provider's native
HLS exactly as the website does — no re-encode, no timestamp tampering, no CPU.

Flow:
  * ``create_proxy_session(headers)`` stores the provider headers under a token.
  * The browser loads ``/api/proxy/<token>/r/<b64(playlist_url)>``.
  * Playlists are fetched server-side and every child URI (variants, segments,
    EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA) is rewritten to point back at the proxy
    with the resolved absolute URL. Non-playlist resources (segments, keys) are
    streamed through verbatim, forwarding Range requests.

Security: only http/https is allowed and hosts resolving to private/loopback
addresses are rejected (SSRF guard).
"""

import base64
import http.client
import ipaddress
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

try:
    from ..logger import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging
    logger = logging.getLogger(__name__)

_SESSION_TIMEOUT = 3600  # 1 h
_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')

_sessions: dict = {}
_lock = threading.Lock()


# ── Session registry ─────────────────────────────────────────────────────────────────────
# In-memory token -> {headers, last-access} map. Tokens are short-lived and
# tied to a single playback session; they let the proxy route (routes/stream.py)
# re-attach the provider's Referer/User-Agent to every playlist/segment
# request without exposing those headers to the browser.

def create_proxy_session(headers: dict | None) -> str:
    """Create a new proxy session for the given provider headers and return its token.

    Used by: routes/stream.py when starting a stream-from-source playback.
    """
    _reap()
    token = uuid.uuid4().hex
    with _lock:
        _sessions[token] = {"headers": dict(headers or {}), "last": time.time()}
    return token


def get_proxy_session(token: str) -> dict | None:
    """Look up a session by token, refreshing its last-access time (keeps it alive)."""
    with _lock:
        s = _sessions.get(token)
        if s:
            s["last"] = time.time()
    return s


def close_proxy_session(token: str):
    """Explicitly drop a session, e.g. when playback ends."""
    with _lock:
        _sessions.pop(token, None)


def _reap():
    """Evict sessions that haven't been accessed within _SESSION_TIMEOUT."""
    now = time.time()
    with _lock:
        for t in [t for t, s in _sessions.items() if now - s["last"] > _SESSION_TIMEOUT]:
            _sessions.pop(t, None)


# ── URL (de)serialisation ─────────────────────────────────────────────────────────────────
# Absolute child URLs are embedded in the proxy path as unpadded urlsafe-base64
# so they survive being placed in a URL segment without extra escaping.

def b64e(url: str) -> str:
    """Encode a URL for embedding in a proxy path segment."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def b64d(s: str) -> str:
    """Decode a URL previously encoded with :func:`b64e` (re-adds stripped padding)."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode("utf-8")


# ── SSRF guard ────────────────────────────────────────────────────────────────────

def _forbidden_ip(addr) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


# Host verdicts of is_safe_url(). A stream asks for a segment every few seconds
# and every one of them used to cost a DNS lookup here and another one for the
# connection. Caching the verdict is safe because the connection itself is
# checked again (see _safe_create_connection), so a host that changes its DNS
# answer after the check still cannot be reached at a private address.
_SAFE_HOST_TTL = 60.0
_safe_hosts: dict = {}
_safe_hosts_lock = threading.Lock()


def is_safe_url(url: str) -> bool:
    """Reject non-http(s) schemes and hosts resolving to private/loopback/link-local
    addresses, so the proxy can't be used to reach internal network services."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    now = time.monotonic()
    with _safe_hosts_lock:
        hit = _safe_hosts.get(host)
        if hit and hit[0] > now:
            return hit[1]
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    verdict = not any(_forbidden_ip(info[4][0]) for info in infos)
    with _safe_hosts_lock:
        if len(_safe_hosts) > 512:
            _safe_hosts.clear()
        _safe_hosts[host] = (now + _SAFE_HOST_TTL, verdict)
    return verdict


def _safe_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                            source_address=None, *args, **kwargs):
    """socket.create_connection() that refuses forbidden addresses.

    is_safe_url() resolves the host once and the connection used to resolve it
    again -- a host with a zero-TTL record could pass the check with a public
    address and then be connected at 127.0.0.1 or a LAN address (DNS
    rebinding), and the proxy hands the body back to the browser. Here the
    addresses that are checked are the ones that are connected to.
    """
    host, port = address[0], address[1]
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos or any(_forbidden_ip(info[4][0]) for info in infos):
        raise OSError("connection to a forbidden address refused")
    err = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    raise err or OSError("could not connect")


class _SafeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _safe_create_connection


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    # TLS still verifies against the URL's host name (server_hostname is
    # self.host); only the TCP target is vetted.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _safe_create_connection


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


# ── Fetching ────────────────────────────────────────────────────────────────────────

# Upper bound for one proxied response. A playlist is a few KB and a segment a
# few MB; without a limit a hostile or broken upstream could stream gigabytes
# into memory, since the body is read in full before it is passed on.
MAX_PROXY_BYTES = 64 * 1024 * 1024
# How many redirects to follow. Each hop is re-checked against is_safe_url().
MAX_PROXY_REDIRECTS = 5


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-runs the SSRF check on every hop.

    is_safe_url() validated the URL the client asked for, but urlopen() then
    followed redirects unchecked -- so an upstream that answered 302 ->
    http://127.0.0.1:8080/... or -> 169.254.169.254 got the proxy to fetch it
    and hand the body straight back to the browser.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "redirect to a forbidden address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    # With an explicit HTTP(S) proxy configured the TCP peer is the proxy (often
    # a LAN address), so the connect-time check cannot apply; is_safe_url()
    # and the per-redirect check still do.
    if urllib.request.getproxies():
        return urllib.request.build_opener(_CheckedRedirectHandler)
    return urllib.request.build_opener(_CheckedRedirectHandler,
                                       _SafeHTTPHandler, _SafeHTTPSHandler)


_proxy_opener = _build_opener()

# Read size when passing a body through.
STREAM_CHUNK = 64 * 1024
# A playlist is read whole (it is rewritten); one larger than this is refused.
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024


def open_upstream(url: str, headers: dict, range_header: str | None = None):
    """Open *url* with the provider headers, without reading the body.

    Returns ``(code, headers, response, final_url)``; the caller reads and
    closes ``response``.
    """
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # Avoid compressed responses so playlists parse and segments pass through raw.
    req.add_header("Accept-Encoding", "identity")
    if range_header:
        req.add_header("Range", range_header)
    # Opened through the checked opener, not urlopen(): see _CheckedRedirectHandler.
    resp = _proxy_opener.open(req, timeout=30)
    final_url = resp.geturl()
    # Belt and braces: geturl() is the address the body actually came from.
    if final_url != url and not is_safe_url(final_url):
        resp.close()
        raise ValueError("upstream resolved to a forbidden address")
    return resp.getcode(), dict(resp.headers), resp, final_url


def fetch(url: str, headers: dict, range_header: str | None = None) -> tuple:
    """Fetch a URL with the provider headers. Returns (code, headers, data, final_url)."""
    code, up_headers, resp, final_url = open_upstream(url, headers, range_header)
    try:
        # One byte over the limit is enough to notice and refuse.
        data = resp.read(MAX_PROXY_BYTES + 1)
    finally:
        resp.close()
    if len(data) > MAX_PROXY_BYTES:
        raise ValueError(f"upstream response larger than {MAX_PROXY_BYTES} bytes")
    return code, up_headers, data, final_url


def iter_body(resp, first: bytes = b""):
    """Yield *first*, then the rest of *resp* in chunks, at most
    MAX_PROXY_BYTES in total; closes *resp* when done or abandoned."""
    sent = 0
    try:
        if first:
            sent += len(first)
            yield first
        while sent <= MAX_PROXY_BYTES:
            chunk = resp.read(STREAM_CHUNK)
            if not chunk:
                return
            sent += len(chunk)
            if sent > MAX_PROXY_BYTES:
                logger.warning("[StreamProxy] upstream body over %d bytes cut off",
                               MAX_PROXY_BYTES)
                return
            yield chunk
    finally:
        resp.close()


def is_playlist(data: bytes) -> bool:
    return data[:7] == b"#EXTM3U"


# ── Playlist rewriting ─────────────────────────────────────────────────────────

def rewrite_playlist(text: str, playlist_url: str, proxy_base: str) -> str:
    """Rewrite all child URIs in an HLS playlist to go through the proxy.

    ``proxy_base`` ends with ``/`` (e.g. ``/api/proxy/<token>/r/``). Relative
    URIs are resolved against ``playlist_url`` (the playlist's own final URL).
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            # Rewrite URI="…" attributes (EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, …).
            m = _URI_ATTR_RE.search(line)
            if m:
                child_abs = urljoin(playlist_url, m.group(1))
                line = line[:m.start(1)] + proxy_base + b64e(child_abs) + line[m.end(1):]
            out.append(line)
        else:
            # A media/segment/variant URI line.
            child_abs = urljoin(playlist_url, stripped)
            out.append(proxy_base + b64e(child_abs))
    return "\n".join(out)
