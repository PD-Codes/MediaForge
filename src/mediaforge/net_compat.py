"""Keep ``requests`` off HTTP/3.

With niquests installed, ``urllib3`` is really urllib3-future, so a plain
``requests`` call silently upgrades to HTTP/3 (QUIC over UDP) as soon as the
server advertises it via Alt-Svc. Behind Docker/VPN setups (e.g. gluetun) with a
small MTU that fails with ``OSError 90 Message too long`` or an HTTP/3 idle
timeout. HTTP/1.1 and HTTP/2 over TCP are unaffected, so only h3 is disabled.
niquests sessions (GLOBAL_SESSION) are not touched.
"""
import requests.adapters


def disable_requests_http3() -> None:
    try:
        from urllib3 import HttpVersion  # only exists in urllib3-future
    except ImportError:
        return  # stock urllib3 has no HTTP/3, nothing to turn off

    original = requests.adapters.HTTPAdapter.init_poolmanager
    if getattr(original, "_mf_no_h3", False):
        return

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs.setdefault("disabled_svn", {HttpVersion.h3})
        return original(self, connections, maxsize, block, **pool_kwargs)

    init_poolmanager._mf_no_h3 = True
    requests.adapters.HTTPAdapter.init_poolmanager = init_poolmanager
