"""The service worker's caching rules.

Parsed as text rather than executed: there is no service-worker runtime in
pytest, and the properties worth guarding are all decisions visible in the
source. The one that matters most is a *negative*: API responses must never be
cached. A queue that claims three downloads are running while the server is
unreachable, or a library listing full of files deleted this morning, is worse
than an honest "you are offline" — stale operational data reads as truth.
"""

import pathlib
import re

import pytest

SW = (pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web"
      / "static" / "sw.js")


@pytest.fixture(scope="module")
def source():
    return SW.read_text(encoding="utf-8")


def test_the_service_worker_actually_handles_fetches(source):
    """It used to cache two files and never serve them -- a PWA in name only,
    installable and completely blank the moment the network hiccupped."""
    assert 'addEventListener("fetch"' in source


def test_api_responses_are_never_cached(source):
    """The negative that matters. See the module docstring."""
    assert "isLiveData" in source
    body = source[source.index("function isLiveData"):]
    body = body[:body.index("}")]
    for path in ("/api/", "/healthz", "/readyz"):
        assert path in body, path
    # ...and the fetch handler has to bail out on them before any caching.
    handler = source[source.index('addEventListener("fetch"'):]
    assert re.search(r"if \(isLiveData\(url\)\) return;", handler)


def test_non_get_requests_are_left_alone(source):
    """A POST replayed from a cache would be a download queued twice."""
    handler = source[source.index('addEventListener("fetch"'):]
    assert re.search(r'request\.method !== "GET"\) return;', handler)


def test_range_requests_pass_through(source):
    """Video. The player relies on the server's own 206 handling, and a
    service worker "helping" here breaks seeking."""
    handler = source[source.index('addEventListener("fetch"'):]
    assert 'request.headers.has("range")' in handler


def test_only_complete_same_origin_responses_are_stored(source):
    """A 206 or an opaque cross-origin response cached here would later be
    served back as if it were the whole file."""
    checks = re.findall(r"response\.status === 200 && response\.type === \"basic\"", source)
    assert len(checks) >= 2, "both caching paths must apply the check"


def test_the_cache_is_versioned_and_old_ones_are_dropped(source):
    assert re.search(r'CACHE_VERSION = "[^"]+"', source)
    activate = source[source.index('addEventListener("activate"'):]
    assert "caches.delete" in activate


def test_shell_entries_are_added_individually(source):
    """addAll() is atomic: one renamed asset would reject the whole install,
    and a service worker that never installs never updates either."""
    install = source[source.index('addEventListener("install"'):
                     source.index('addEventListener("activate"')]
    # Comments stripped first -- the reason this is not addAll() is explained
    # in a comment that says "addAll", and a test that reads comments is a
    # test that fails on documentation.
    code = re.sub(r"//.*", "", install)
    assert "addAll" not in code
    assert "cache.add(url)" in code


def test_the_offline_page_is_part_of_the_shell(source):
    assert 'OFFLINE_URL = "/offline"' in source
    assert "OFFLINE_URL," in source          # listed in SHELL


def test_offline_page_answers_without_a_session(client, users):
    """Precached at install time, when the fetch carries no session worth
    relying on. A login redirect cached under this URL would be shown as
    "you are offline" forever.

    ``users`` is requested only so an admin exists: without one the app
    redirects everything to /setup, and this would fail for a reason that has
    nothing to do with the offline page.
    """
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert b"MediaForge" in resp.data


def test_offline_page_carries_no_data(client, users):
    """Showing a cached queue here would be worse than showing nothing."""
    body = client.get("/offline").data.decode("utf-8", "replace").lower()
    # Words like "queue" and "library" legitimately appear in the sentence
    # explaining why nothing is shown, so the check is for the SHAPES data
    # would arrive in, not for the vocabulary.
    # "<li" without the closing bracket would match the <line> in the SVG.
    for marker in ("<table", "<ul", "<li>", "<li ", "browse-card", "queue-row"):
        assert marker not in body, marker
    assert "fetch(" not in body, "the offline page must not try to load data"
    assert "xmlhttprequest" not in body
