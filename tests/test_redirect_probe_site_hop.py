"""Provider availability probe: the site's redirect hop goes through
GLOBAL_SESSION (configured DNS + mirror failover); only the hoster page it
points to is fetched with curl_cffi."""

from types import SimpleNamespace

import pytest

from mediaforge import config

curl_requests = pytest.importorskip("curl_cffi.requests")


def test_site_hop_uses_the_session_and_the_hoster_page_curl(monkeypatch):
    session_calls, curl_calls = [], []

    def session_get(url, **kw):
        session_calls.append((url, kw.get("allow_redirects")))
        return SimpleNamespace(status_code=302, headers={"Location": "https://voe.sx/e/abc"},
                               text="", url="https://aniworld.cc/redirect/1")

    def curl_get(url, **kw):
        curl_calls.append(url)
        return SimpleNamespace(text="<html>player</html>", status_code=200, url=url)

    monkeypatch.setattr(config, "GLOBAL_SESSION", SimpleNamespace(get=session_get))
    monkeypatch.setattr(curl_requests, "get", curl_get)

    html, status, final = config._fetch_redirect_page_url("https://aniworld.to/redirect/1", 5)
    assert session_calls == [("https://aniworld.to/redirect/1", False)]
    assert curl_calls == ["https://voe.sx/e/abc"]
    assert (html, status, final) == ("<html>player</html>", 200, "https://voe.sx/e/abc")


def test_a_page_without_redirect_is_returned_as_is(monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_SESSION", SimpleNamespace(
        get=lambda url, **kw: SimpleNamespace(status_code=404, headers={}, text="gone", url=url)))
    monkeypatch.setattr(curl_requests, "get", lambda *a, **k: pytest.fail("no hoster hop"))
    assert config._fetch_redirect_page_url("https://s.to/r?t=x", 5)[:2] == ("gone", 404)
