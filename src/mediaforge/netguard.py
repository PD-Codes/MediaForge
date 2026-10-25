"""Connect-time "public addresses only" guard for user-supplied URLs.

The Direct Link feature hands a URL any logged-in account typed in to yt-dlp.
The SSRF gate (web/stream_proxy.is_safe_url) checked that URL once, up front;
everything after it was unchecked: yt-dlp follows HTTP redirects by itself
(302 -> http://127.0.0.1:8080/..., the router, 169.254.169.254), an HLS
playlist can list segment URLs on internal hosts, and a DNS-rebinding name
passes the check with a public answer and is connected at a private one.

``public_only()`` closes that for the code running inside it: every TCP
connection the current thread opens is refused when its address is private,
loopback, link-local, multicast, reserved or unspecified. The check runs on the
address actually connected to, so redirects, rebinding and playlist URLs are
all covered. Other threads are not affected -- Jellyfin/Plex on the LAN keep
working while a Direct Link download runs.

Through a configured HTTP(S) proxy the connection goes to the proxy (often a
local or LAN address) and the target is resolved there, so the guard stands
aside -- the same rule the stream proxy follows.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import socket
import threading

_local = threading.local()
_install_lock = threading.Lock()
_installed = False


def forbidden_ip(addr) -> bool:
    """True for addresses a user-supplied URL must never reach."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _check(sock, address) -> None:
    if getattr(_local, "depth", 0) <= 0:
        return
    if sock.type != socket.SOCK_STREAM or not isinstance(address, tuple) or not address:
        return
    if forbidden_ip(address[0]):
        raise OSError(errno.EACCES,
                      "Connection to a private or local address refused: %s" % address[0])


def _install() -> None:
    global _installed
    with _install_lock:
        if _installed:
            return
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex

        def connect(self, address):
            _check(self, address)
            return orig_connect(self, address)

        def connect_ex(self, address):
            _check(self, address)
            return orig_connect_ex(self, address)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        _installed = True


@contextlib.contextmanager
def public_only():
    """Refuse private/local TCP targets for this thread while inside."""
    import urllib.request
    if urllib.request.getproxies():
        yield
        return
    _install()
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1
