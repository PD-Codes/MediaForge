"""The project resolver failing to resolve a host must not be terminal.

MediaForge routes its HTTP egress through a DoH resolver so an ISP-level DNS
block cannot hide a source site. When that resolver cannot answer at all --
blocked DoH endpoint, a filtered network, a domain it simply has no answer for
-- the request used to die with a bare ``NameResolutionError`` and the source
went dark, even though the machine's own resolver would have answered fine.

These tests pin the one retry that fixes it, and just as importantly the two
cases that must NOT retry.
"""

import types

import niquests
import pytest

from mediaforge import config as C
from mediaforge import mirrors as M


DNS_EXC = niquests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='filmo.to', port=443): Max retries exceeded with "
    "url: /popular (Caused by NameResolutionError(\"Failed to resolve "
    "'filmo.to' (Name or service not known: filmo.to using 1 resolver(s))\"))"
)
OTHER_EXC = niquests.exceptions.ConnectionError(
    "Connection aborted., OSError(5, 'Input/output error')"
)


@pytest.fixture(autouse=True)
def _clean_sticky_set():
    """The fallback is sticky per host (see config._dns_fallback_hosts), so one
    test marking a host would silently decide the next test's routing."""
    with C._dns_fallback_lock:
        C._dns_fallback_hosts.clear()
    yield
    with C._dns_fallback_lock:
        C._dns_fallback_hosts.clear()


@pytest.fixture
def proxy(monkeypatch):
    """A _SessionProxy whose two sessions are labelled stand-ins, so a test can
    see WHICH resolver a request went out on."""
    doh = types.SimpleNamespace(_label="doh")
    system = types.SimpleNamespace(_label="system")
    monkeypatch.setattr(C._SessionProxy, "_get_session", lambda self: doh)
    monkeypatch.setattr(C._SessionProxy, "_get_system_session", lambda self: system)
    return C._SessionProxy(resolver=["doh+google://"])


def _record(monkeypatch, behaviour):
    """Replace the mirror walk with *behaviour*, recording the session used."""
    calls = []

    def fake(session, method, url, **kwargs):
        calls.append(session._label)
        return behaviour(session)

    monkeypatch.setattr(M, "request_with_failover", fake)
    return calls


def test_dns_failure_is_retried_on_the_system_resolver(proxy, monkeypatch):
    def behaviour(session):
        if session._label == "doh":
            raise DNS_EXC
        return "answered"

    calls = _record(monkeypatch, behaviour)
    assert proxy.request("GET", "https://filmo.to/popular") == "answered"
    assert calls == ["doh", "system"]


def test_a_non_dns_error_is_not_retried(proxy, monkeypatch):
    """A dead site, a TLS problem or a reset connection are answers, not
    resolver failures -- retrying them on another resolver only doubles the
    wait before the same error surfaces."""
    calls = _record(monkeypatch, lambda session: (_ for _ in ()).throw(OTHER_EXC))
    with pytest.raises(niquests.exceptions.ConnectionError):
        proxy.request("GET", "https://filmo.to/popular")
    assert calls == ["doh"]


def test_no_retry_when_the_system_resolver_is_already_in_use(monkeypatch):
    """Nothing to fall back TO -- a second identical attempt would just fail
    the same way, twice as slowly."""
    doh = types.SimpleNamespace(_label="doh")
    system = types.SimpleNamespace(_label="system")
    monkeypatch.setattr(C._SessionProxy, "_get_session", lambda self: doh)
    monkeypatch.setattr(C._SessionProxy, "_get_system_session", lambda self: system)
    proxy = C._SessionProxy(resolver="system")

    calls = _record(monkeypatch, lambda session: (_ for _ in ()).throw(DNS_EXC))
    with pytest.raises(niquests.exceptions.ConnectionError):
        proxy.request("GET", "https://filmo.to/popular")
    assert calls == ["doh"]


@pytest.mark.parametrize("message, expected", [
    ("Failed to resolve 'filmo.to'", True),
    ("NameResolutionError(...)", True),
    ("Name or service not known", True),
    ("Temporary failure in name resolution", True),
    ("Connection aborted., OSError(5)", False),
    ("read timed out", False),
    ("certificate verify failed", False),
])
def test_dns_failure_detection(message, expected):
    assert C._looks_like_dns_failure(Exception(message)) is expected


def test_the_fallback_is_sticky_per_host(proxy, monkeypatch):
    """Once a host is known to be unresolvable, stop paying the failing lookup
    on every single request -- a page open or a download is dozens of requests
    to the same host, and each one ate a full resolver timeout first."""
    def behaviour(session):
        if session._label == "doh":
            raise DNS_EXC
        return "answered"

    calls = _record(monkeypatch, behaviour)
    proxy.request("GET", "https://filmo.to/a")
    proxy.request("GET", "https://filmo.to/b")
    proxy.request("GET", "https://filmo.to/c")
    # First request probes the project resolver, the rest go straight out.
    assert calls == ["doh", "system", "system", "system"]
    assert "filmo.to" in C.dns_fallback_hosts()

    # A different host is unaffected -- one site's DNS problem must not quietly
    # move the whole app off the resolver the user configured.
    calls.clear()
    proxy.request("GET", "https://aniworld.to/x")
    assert calls == ["doh", "system"]


def test_changing_the_dns_setting_clears_the_sticky_set(proxy, monkeypatch):
    """The user picking a different resolver is exactly the moment to give it
    a fresh chance; staying pinned would ignore the setting they just changed."""
    _record(monkeypatch, lambda session: (_ for _ in ()).throw(DNS_EXC)
            if session._label == "doh" else "answered")
    proxy.request("GET", "https://filmo.to/a")
    assert "filmo.to" in C.dns_fallback_hosts()

    proxy._swap(["doh+cloudflare://"])
    assert C.dns_fallback_hosts() == []


def test_the_fallback_session_shares_the_cookie_jar(monkeypatch):
    """Several scrapers only work because consecutive requests look like the
    same browser: filmo.to hands out a CSRF token plus a session cookie on the
    movie page and validates both on the /n POST. Two jars means the POST
    arrives without the session and filmo.to answers 419 Page Expired."""
    made = []

    class _FakeSession:
        def __init__(self, resolver):
            self.resolver = resolver
            self.cookies = {"jar": resolver}
            self.timeout = None
            made.append(self)

    monkeypatch.setattr(C, "_make_session", lambda resolver=None: _FakeSession(resolver))
    proxy = C._SessionProxy(resolver=["doh+google://"])

    primary = proxy._get_session()
    fallback = proxy._get_system_session()
    assert fallback is not primary
    assert fallback.cookies is primary.cookies
