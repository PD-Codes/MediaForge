"""filmo.to's CSRF token goes stale -- one refetch, not a dead download.

The movie page is fetched once and its CSRF token cached on the FilmoMovie.
That object outlives a failed download attempt, so the queue worker's second
try replayed the same token against /n and filmo.to answered
"419 Page Expired" -- which is Laravel for "reload the page", the one thing
the code never did. Every further attempt then failed identically.
"""

import pytest

from mediaforge.models.filmo_to import scraper
from mediaforge.models.filmo_to.movie import FilmoMovie


URL = "https://filmo.to/movies/some-movie"
PAGE_ONE = ('<meta name="csrf-token" content="TOKEN-1">'
            '<div class="lang-mark">Deutsch</div>'
            '<div class="provider-chip" data-p="PAYLOAD-1" data-link-id="1">VOE</div>')
PAGE_TWO = PAGE_ONE.replace("TOKEN-1", "TOKEN-2").replace("PAYLOAD-1", "PAYLOAD-2")


@pytest.fixture
def movie(monkeypatch):
    """A FilmoMovie whose page fetch and provider parsing are stubbed."""
    pages = iter([("html-1", "TOKEN-1"), ("html-2", "TOKEN-2")])
    fetched = []

    def _fetch(url, timeout=15):
        fetched.append(url)
        return next(pages)

    def _parse(html):
        suffix = html.split("-")[-1]
        return {"German Dub": {"VOE": {"data_p": "PAYLOAD-" + suffix,
                                       "movie_link_id": suffix}}}

    monkeypatch.setattr(scraper, "fetch_movie_page", _fetch)
    monkeypatch.setattr(scraper, "parse_provider_rows", _parse)
    mv = FilmoMovie(url=URL)
    mv._fetched = fetched
    return mv


def test_a_stale_token_triggers_exactly_one_refetch(movie, monkeypatch):
    seen = []

    def _resolve(data_p, csrf, referer, timeout=15):
        seen.append((data_p, csrf))
        if len(seen) == 1:
            raise scraper.FilmoTokenExpired("419")
        return "https://voe.sx/e/abc"

    monkeypatch.setattr(scraper, "resolve_provider_url", _resolve)

    assert movie.provider_url == "https://voe.sx/e/abc"
    # The retry must use the token AND the payload from the SECOND page --
    # a fresh token with a chip minted against the old one is still a 419.
    assert seen == [("PAYLOAD-1", "TOKEN-1"), ("PAYLOAD-2", "TOKEN-2")]
    assert len(movie._fetched) == 2


def test_a_second_expiry_is_not_retried_forever(movie, monkeypatch):
    """One refetch is a fix; a loop is a denial of service against the site."""
    calls = []

    def _always_expired(data_p, csrf, referer, timeout=15):
        calls.append(csrf)
        raise scraper.FilmoTokenExpired("419")

    monkeypatch.setattr(scraper, "resolve_provider_url", _always_expired)

    with pytest.raises(scraper.FilmoTokenExpired):
        _ = movie.provider_url
    assert len(calls) == 2


def test_other_failures_are_not_swallowed_by_the_retry(movie, monkeypatch):
    """Only 419 means "your token is old". A 404 or a network error must
    surface as itself, and must not cost a second page fetch."""
    def _boom(data_p, csrf, referer, timeout=15):
        raise scraper.FilmoUnavailable("gone")

    monkeypatch.setattr(scraper, "resolve_provider_url", _boom)

    with pytest.raises(scraper.FilmoUnavailable):
        _ = movie.provider_url
    assert len(movie._fetched) == 1
