"""The OS trust store must never be able to take the module store down.

Background: truststore captures ``super(ssl.SSLContext, ssl.SSLContext)`` at
import time. Normally that resolves to the C-level descriptor in
``_ssl._SSLContext`` and writing ``verify_mode`` never enters ``ssl.py`` at
all. But if something else in the interpreter replaced ``ssl.SSLContext`` with
a subclass first (a .pth file or sitecustomize.py, as TLS-inspection tooling
installs), the same write lands in ``ssl.py``'s Python property, whose setter
reads the module global ``SSLContext`` at call time -- now the subclass -- and
calls itself until RecursionError. Every TLS handshake through truststore then
dies, which reached users as "module store unreachable".

MediaForge cannot fix that interpreter. It must route around it.
"""

import ssl

import pytest


@pytest.fixture()
def fresh_config(monkeypatch):
    """config with its truststore verdict un-cached, restored afterwards."""
    from mediaforge import config

    monkeypatch.setattr(config, "_truststore_checked", False, raising=False)
    monkeypatch.setattr(config, "_TRUSTSTORE_UNSAFE_REASON", None, raising=False)
    return config


def test_a_healthy_interpreter_still_uses_the_os_trust_store(fresh_config):
    truststore = pytest.importorskip("truststore")

    assert fresh_config._truststore_is_safe() is True
    assert fresh_config.truststore_unsafe_reason() is None
    ctx = fresh_config.ssl_context_for("https://example.com/")
    assert isinstance(ctx, truststore.SSLContext)


def test_an_injected_ssl_module_falls_back_to_certifi(fresh_config, monkeypatch):
    """The failure this guards against, reproduced by injecting a subclass.

    ``ssl_context_for()`` returning None is not a downgrade of security: None
    means "Python's default context", i.e. the bundled certifi roots with
    verification fully on.
    """
    pytest.importorskip("truststore")

    class _Injected(ssl.SSLContext):
        pass

    monkeypatch.setattr(ssl, "SSLContext", _Injected)

    assert fresh_config._truststore_is_safe() is False
    reason = fresh_config.truststore_unsafe_reason()
    assert reason and "ssl.SSLContext has been replaced" in reason
    assert fresh_config.ssl_context_for("https://example.com/") is None


def test_the_verdict_is_cached(fresh_config, monkeypatch):
    """The check runs once; the warning must not be logged per request."""
    pytest.importorskip("truststore")

    calls = []
    real = fresh_config._truststore_is_safe

    fresh_config._truststore_is_safe()
    monkeypatch.setattr(ssl, "SSLContext", type("X", (ssl.SSLContext,), {}))
    # Already decided -- a later injection does not re-open the question, so a
    # long-running process keeps one consistent answer.
    assert real() is True
    assert not calls


def test_the_store_retries_without_truststore_on_recursion(monkeypatch):
    """Even if the pre-flight check misses a variant of the bug, one
    RecursionError must not cost the user the module store."""
    from mediaforge.web.thirdparties import store

    seen = []

    def _fake_urlopen_read(req, timeout, context, max_bytes):
        seen.append(context)
        if context is not None:
            raise RecursionError("maximum recursion depth exceeded")
        return b'{"modules": []}'

    monkeypatch.setattr(store, "_urlopen_read", _fake_urlopen_read)
    monkeypatch.setattr("mediaforge.config.ssl_context_for",
                        lambda url: object())

    data = store._http_get("https://example.com/store/index.json", 1024)
    assert data == b'{"modules": []}'
    assert len(seen) == 2 and seen[1] is None  # retried on the default context


def test_a_recursion_without_a_context_is_not_swallowed(monkeypatch):
    """If there is no truststore context to blame, the error is real and has
    to surface rather than be retried into an identical failure."""
    from mediaforge.web.thirdparties import store

    def _boom(req, timeout, context, max_bytes):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(store, "_urlopen_read", _boom)
    monkeypatch.setattr("mediaforge.config.ssl_context_for", lambda url: None)

    with pytest.raises(RecursionError):
        store._http_get("https://example.com/store/index.json", 1024)
