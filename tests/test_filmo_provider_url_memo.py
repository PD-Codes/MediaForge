"""filmo.to: provider_url ran the whole mint flow (POST /n, GET /n/<token>,
hoster page) on every read -- 4-5 times per download attempt."""

from mediaforge.models.filmo_to import movie as fm


def test_provider_url_is_reused_per_language_and_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(fm.scraper, "resolve_provider_url",
                        lambda data_p, csrf, url: calls.append(data_p) or "https://voe.sx/e/" + data_p)
    monkeypatch.setattr(fm.FilmoMovie, "_selected_chip",
                        property(lambda self: {"data_p": self.selected_language[:2]}))
    monkeypatch.setattr(fm.FilmoMovie, "_csrf", property(lambda self: "csrf"))

    movie = fm.FilmoMovie("https://filmo.to/movies/some-movie-123")
    first = movie.provider_url
    assert movie.provider_url == first
    assert movie.provider_url == first
    assert len(calls) == 1

    movie.selected_language = "English Dub"
    assert movie.provider_url == "https://voe.sx/e/En"
    assert len(calls) == 2

    movie._invalidate_page()
    movie.provider_url
    assert len(calls) == 3
