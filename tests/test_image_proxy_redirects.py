"""The image proxy must re-check every redirect hop, not just the first URL.

`/api/img` guards its fetch twice: an allowlist decides which sites may be
fetched at all, and `stream_proxy.is_safe_url()` then refuses a name that
resolves to an internal address. Both only ever saw the URL the browser asked
for. The client libraries followed redirects silently, so an allowlisted CDN
answering `302 -> http://127.0.0.1:8080/...` or `-> 169.254.169.254` got the
proxy to fetch that and hand the body back to the browser -- cached under the
harmless-looking original URL.

`stream_proxy._CheckedRedirectHandler` already solved this for the stream
proxy. These tests pin the same rule for the image proxy, where the client has
no handler to hook into and the hops are walked by hand.
"""

import pytest

from mediaforge.web.routes import image_proxy


class _Resp:
    """The bit of a requests/curl_cffi response the fetch helper touches."""

    def __init__(self, status_code, headers=None, content=b"x"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.ok = 200 <= status_code < 300


@pytest.fixture()
def fake_upstream(monkeypatch):
    """Script the upstream's answers and record which URLs were fetched.

    `_img_fetch_with_retries` imports its HTTP client *inside* the function, so
    patching an attribute on the route module would do nothing -- the real
    `requests.get` has to be replaced. curl_cffi is optional and may or may not
    be installed on the host, so it is forced to fail its import and the plain
    path is exercised either way.

    Patching the client rather than the helper is deliberate: the code walking
    the redirects is exactly the code under test.
    """
    import sys

    import requests

    def _install(responses):
        seen = []

        def _fake_get(url, **kwargs):
            seen.append(url)
            # Following redirects ourselves is the entire point; if the helper
            # ever hands that back to the client, the test is meaningless.
            assert kwargs.get("allow_redirects") is False, (
                "the fetch let the client follow redirects again"
            )
            return responses[len(seen) - 1]

        monkeypatch.setattr(requests, "get", _fake_get)
        monkeypatch.setitem(sys.modules, "curl_cffi", None)
        return seen

    return _install


def test_a_redirect_to_localhost_is_refused(fake_upstream, monkeypatch):
    seen = fake_upstream([
        _Resp(302, {"Location": "http://127.0.0.1:8080/secret"}),
        _Resp(200, {"Content-Type": "image/jpeg"}),
    ])

    with pytest.raises(PermissionError):
        image_proxy._img_fetch_with_retries("https://image.tmdb.org/t/p/w154/a.jpg")

    assert seen == ["https://image.tmdb.org/t/p/w154/a.jpg"], (
        "the forbidden target was fetched anyway"
    )


def test_a_redirect_to_link_local_metadata_is_refused(fake_upstream):
    """169.254.169.254 is the cloud metadata address -- the classic target."""
    fake_upstream([
        _Resp(302, {"Location": "http://169.254.169.254/latest/meta-data/"}),
    ])

    with pytest.raises(PermissionError):
        image_proxy._img_fetch_with_retries("https://image.tmdb.org/t/p/w154/a.jpg")


def test_a_refused_redirect_is_not_retried(fake_upstream):
    """The retry loop must not ask a hostile upstream the same question twice."""
    seen = fake_upstream([
        _Resp(302, {"Location": "http://127.0.0.1/x"}),
        _Resp(302, {"Location": "http://127.0.0.1/x"}),
    ])

    with pytest.raises(PermissionError):
        image_proxy._img_fetch_with_retries("https://image.tmdb.org/a.jpg")

    assert len(seen) == 1


def test_an_ordinary_redirect_is_still_followed(fake_upstream):
    """A CDN handing off to its regional edge has to keep working."""
    ok = _Resp(200, {"Content-Type": "image/jpeg"}, b"jpegbytes")
    seen = fake_upstream([
        _Resp(302, {"Location": "https://image.tmdb.org/edge/a.jpg"}),
        ok,
    ])

    resp = image_proxy._img_fetch_with_retries("https://image.tmdb.org/t/p/w154/a.jpg")

    assert resp is ok
    assert seen == ["https://image.tmdb.org/t/p/w154/a.jpg",
                    "https://image.tmdb.org/edge/a.jpg"]


def test_a_relative_redirect_resolves_against_the_hop_it_came_from(fake_upstream):
    """Relative Location headers are legal and common.

    Resolving against the original URL instead of the current hop would build
    the wrong address -- and an address nobody checked.
    """
    ok = _Resp(200, {"Content-Type": "image/jpeg"})
    seen = fake_upstream([
        _Resp(302, {"Location": "https://image.tmdb.org/a/b.jpg"}),
        _Resp(302, {"Location": "../c/d.jpg"}),
        ok,
    ])

    image_proxy._img_fetch_with_retries("https://image.tmdb.org/t/p/w154/a.jpg")

    # Relative to the SECOND hop (/a/b.jpg), not to the original /t/p/w154/.
    assert seen[-1] == "https://image.tmdb.org/c/d.jpg"


def test_a_redirect_loop_ends(fake_upstream):
    """Without a hop cap this walks forever on a self-referencing upstream."""
    fake_upstream([_Resp(302, {"Location": "https://image.tmdb.org/loop"})] * 50)

    with pytest.raises(PermissionError):
        image_proxy._img_fetch_with_retries("https://image.tmdb.org/loop")
