"""Stream proxy: the connect-time address check and the streamed body.

* The SSRF check used to resolve the host once and the connection resolved it
  again, so a host could pass with a public address and then be connected at a
  private one (DNS rebinding). The connection now vets the addresses it
  actually connects to.
* Segments used to be read whole before the first byte went out; they are now
  passed through in chunks with the same overall size limit.
"""

import io

import pytest

from mediaforge.web import stream_proxy as sp


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.2", "::1"])
def test_connection_to_a_private_address_is_refused(host):
    with pytest.raises(OSError):
        sp._safe_create_connection((host, 80), timeout=1)


def test_rebinding_host_is_refused_at_connect_time(monkeypatch):
    # The pre-check saw a public address; the resolver now answers loopback.
    monkeypatch.setattr(sp.socket, "getaddrinfo",
                        lambda *a, **k: [(sp.socket.AF_INET, sp.socket.SOCK_STREAM, 6, "",
                                          ("127.0.0.1", 80))])
    with pytest.raises(OSError):
        sp._safe_create_connection(("rebind.example", 80), timeout=1)


def test_private_urls_are_not_safe():
    sp._safe_hosts.clear()
    assert not sp.is_safe_url("http://127.0.0.1/x.m3u8")
    assert not sp.is_safe_url("file:///etc/passwd")


class _Body(io.BytesIO):
    closed_by_proxy = False

    def close(self):
        self.closed_by_proxy = True
        super().close()


def test_body_is_streamed_in_chunks_and_closed():
    payload = b"x" * (sp.STREAM_CHUNK * 3 + 10)
    body = _Body(payload)
    first = body.read(sp.STREAM_CHUNK)
    chunks = list(sp.iter_body(body, first))
    assert b"".join(chunks) == payload
    assert len(chunks) == 4
    assert body.closed_by_proxy


def test_body_is_cut_at_the_size_limit(monkeypatch):
    monkeypatch.setattr(sp, "MAX_PROXY_BYTES", sp.STREAM_CHUNK * 2)
    body = _Body(b"y" * (sp.STREAM_CHUNK * 5))
    out = b"".join(sp.iter_body(body))
    assert len(out) <= sp.STREAM_CHUNK * 2
    assert body.closed_by_proxy
