"""DNS resolver patch — route DNS through a chosen resolver / DoH provider.

Note that the resolver chosen here is not the last word: if it cannot resolve
a host at all, ``config._SessionProxy.request`` retries that one request
through the OS resolver and logs a WARNING naming the host. Without that, a
DoH provider that is blocked or filtered on the user's network took every
source site offline with a bare NameResolutionError and nothing in the app
ever tried the resolver the rest of the machine uses successfully.
"""

import ipaddress as _ipaddress
import socket as _socket

from ..logger import get_logger

logger = get_logger(__name__)


_DNS_PRESETS = {
    "cloudflare": "1.1.1.1",
    "google":     "8.8.8.8",
    "quad9":      "9.9.9.9",
}

# Map preset names to niquests DoH resolver URLs.
# niquests uses these to resolve DNS over HTTPS directly, bypassing the OS
# resolver entirely — which is why socket.getaddrinfo patching alone doesn't
# affect niquests requests.
_DNS_NIQUESTS_MAP = {
    "cloudflare": ["doh+cloudflare://"],
    "google":     ["doh+google://"],
    "quad9":      ["doh://9.9.9.9/dns-query"],
}

_original_getaddrinfo = _socket.getaddrinfo
_active_dns_server: str | None = None


def _apply_dns_patch(server_ip: str | None, mode: str | None = None) -> None:
    """
    Apply DNS routing for the given mode/server_ip.

    Two layers are updated together:
      1. socket.getaddrinfo patch (covers stdlib HTTP, ffmpeg subprocesses, etc.)
      2. GLOBAL_SESSION niquests rebuild (covers all niquests HTTP requests)

    Args:
        server_ip: IP address for the socket patch, or None to restore system DNS.
        mode:      Preset name ("cloudflare", "google", "quad9") used to pick the
                   matching DoH URL for niquests.  For "custom" mode only the socket
                   patch is applied (no DoH URL available).  Pass None or "system"
                   to reset everything to defaults.

    Used by: routes/settings.py's DNS-settings endpoint, whenever the user
    changes the DNS mode in the UI.
    """
    from ..config import rebuild_global_session, set_active_dns_mode

    global _active_dns_server

    if not server_ip:
        # Restore system DNS
        _socket.getaddrinfo = _original_getaddrinfo
        _active_dns_server = None
        rebuild_global_session("system")  # use system DNS resolution
        set_active_dns_mode("system")
        return

    try:
        import dns.resolver as _dns_resolver  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("dnspython not installed — custom DNS not available")
        return

    _active_dns_server = server_ip

    def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        # Don't try to resolve bare IPs or empty strings
        is_ip = False
        if host:
            try:
                _ipaddress.ip_address(host)
                is_ip = True
            except ValueError:
                pass
        if not is_ip and host:
            try:
                res = _dns_resolver.Resolver(configure=False)
                res.nameservers = [server_ip]
                res.timeout = 3
                res.lifetime = 5
                answers = res.resolve(host, "A")
                resolved = str(answers[0])
                return _original_getaddrinfo(resolved, port, family, type, proto, flags)
            except Exception:
                pass  # fall back to system DNS on any error
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _socket.getaddrinfo = _patched_getaddrinfo

    # Also rebuild the niquests GLOBAL_SESSION with the matching DoH resolver.
    # For presets we have a known DoH URL; for "custom" IPs we can't build a
    # DoH URL, so we tell niquests to use system DNS resolution (which uses
    # our patched socket.getaddrinfo).
    doh_urls = _DNS_NIQUESTS_MAP.get(mode) if mode in _DNS_NIQUESTS_MAP else "system"
    rebuild_global_session(doh_urls)
    set_active_dns_mode(mode)


# Known CDN / edge networks — used to label shared anycast IPs so that several
# sites resolving to the same address (normal for Cloudflare) doesn't look like
# a DNS bug.
_CDN_NETS = [
    # Cloudflare IPv4
    ("Cloudflare", "173.245.48.0/20"), ("Cloudflare", "103.21.244.0/22"),
    ("Cloudflare", "103.22.200.0/22"), ("Cloudflare", "103.31.4.0/22"),
    ("Cloudflare", "141.101.64.0/18"), ("Cloudflare", "108.162.192.0/18"),
    ("Cloudflare", "190.93.240.0/20"), ("Cloudflare", "188.114.96.0/20"),
    ("Cloudflare", "197.234.240.0/22"), ("Cloudflare", "198.41.128.0/17"),
    ("Cloudflare", "162.158.0.0/15"), ("Cloudflare", "104.16.0.0/13"),
    ("Cloudflare", "104.24.0.0/14"), ("Cloudflare", "172.64.0.0/13"),
    ("Cloudflare", "131.0.72.0/22"),
]


def _ip_provider(ip):
    """Return a CDN/provider label for an IP (e.g. 'Cloudflare') or None.

    Used by: uptime_monitor.py, to annotate monitored hosts whose resolved
    IP falls in a known CDN range.
    """
    if not ip:
        return None
    try:
        import ipaddress as _ipa
        addr = _ipa.ip_address(ip)
        for name, cidr in _CDN_NETS:
            if addr in _ipa.ip_network(cidr):
                return name
    except Exception:
        pass
    return None
