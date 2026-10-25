"""Direct Link SSRF: the connect-time guard refuses private targets for the
guarded thread only (redirects, DNS rebinding and playlist URLs included)."""

import socket
import threading

import pytest

from mediaforge import netguard


@pytest.fixture()
def listener(monkeypatch):
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()
    srv.close()


def _connect(addr):
    with socket.create_connection(addr, timeout=2):
        return True


def test_private_target_is_refused_inside_the_guard(listener):
    with netguard.public_only():
        with pytest.raises(OSError):
            _connect(listener)
    assert _connect(listener)          # outside: normal again


def test_other_threads_are_not_affected(listener):
    result = {}
    with netguard.public_only():
        t = threading.Thread(target=lambda: result.update(ok=_connect(listener)))
        t.start()
        t.join(5)
    assert result.get("ok") is True


@pytest.mark.parametrize("addr,bad", [("10.1.2.3", True), ("169.254.169.254", True),
                                      ("::ffff:127.0.0.1", True), ("93.184.215.14", False)])
def test_forbidden_ranges(addr, bad):
    assert netguard.forbidden_ip(addr) is bad
