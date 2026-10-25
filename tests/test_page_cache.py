"""Scraper page reuse (models/common/page_cache.py): one fetch per page for a
short while across model objects, never for error/challenge responses."""

from types import SimpleNamespace

from mediaforge.models.common import page_cache


def _session(status=200, text="<html>page</html>"):
    calls = []

    def get(url, **_kw):
        calls.append(url)
        return SimpleNamespace(status_code=status, text=text, headers={}, url=url)

    return SimpleNamespace(get=get), calls


def test_a_page_is_fetched_once_per_session_and_url():
    page_cache.forget()
    sess, calls = _session()
    a = page_cache.fetch_page(sess, "https://site.example/a")
    b = page_cache.fetch_page(sess, "https://site.example/a")
    assert a.text == b.text == "<html>page</html>" and a.status_code == 200
    assert calls == ["https://site.example/a"]

    other, other_calls = _session(text="other")
    assert page_cache.fetch_page(other, "https://site.example/a").text == "other"
    assert other_calls == ["https://site.example/a"]


def test_error_pages_are_not_kept():
    page_cache.forget()
    sess, calls = _session(status=403, text="challenge")
    page_cache.fetch_page(sess, "https://site.example/b")
    page_cache.fetch_page(sess, "https://site.example/b")
    assert len(calls) == 2


def test_forget_drops_a_page():
    page_cache.forget()
    sess, calls = _session()
    page_cache.fetch_page(sess, "https://site.example/c")
    page_cache.forget("https://site.example/c")
    page_cache.fetch_page(sess, "https://site.example/c")
    assert len(calls) == 2
