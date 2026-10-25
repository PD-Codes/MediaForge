"""Custom DNS patch: one cached resolver, local names skipped, and a server
that does not answer is bypassed for a while instead of stalling every
connection for its full timeout."""

import socket

import pytest

dns_resolver = pytest.importorskip("dns.resolver")

from mediaforge.web import dns_patch  # noqa: E402


@pytest.fixture()
def patched(monkeypatch):
    queries = []

    class FakeResolver:
        def __init__(self, configure=False):
            self.cache = None

        def resolve(self, name, rdtype):
            queries.append(name)
            if name == "down.example.com":
                raise dns_resolver.LifetimeTimeout(timeout=5, errors=[])
            return ["93.184.215.14"]

    monkeypatch.setattr(dns_resolver, "Resolver", FakeResolver)
    monkeypatch.setattr("mediaforge.config.rebuild_global_session", lambda *a, **k: None)
    monkeypatch.setattr("mediaforge.config.set_active_dns_mode", lambda *a, **k: None)
    original = socket.getaddrinfo
    seen = []
    monkeypatch.setattr(dns_patch, "_original_getaddrinfo",
                        lambda host, *a, **k: seen.append(host) or [])
    # Restored after the test like every other monkeypatched value.
    monkeypatch.setattr(dns_patch, "_active_dns_server", dns_patch._active_dns_server)
    dns_patch._apply_dns_patch("192.0.2.53", "custom")
    try:
        yield queries, seen
    finally:
        socket.getaddrinfo = original


def test_public_names_use_the_server_local_names_do_not(patched):
    queries, seen = patched
    socket.getaddrinfo("example.com", 443)
    socket.getaddrinfo("jellyfin", 8096)
    socket.getaddrinfo("nas.local", 80)
    socket.getaddrinfo("localhost", 80)
    socket.getaddrinfo("example.com", 443, socket.AF_INET6)
    assert queries == ["example.com"]
    assert seen[0] == "93.184.215.14"


def test_server_that_does_not_answer_is_bypassed(patched):
    queries, seen = patched
    socket.getaddrinfo("down.example.com", 443)
    socket.getaddrinfo("other.example.com", 443)
    assert queries == ["down.example.com"]          # second lookup: no query
    assert seen == ["down.example.com", "other.example.com"]
