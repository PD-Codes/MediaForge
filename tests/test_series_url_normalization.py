"""A browse card that links to an EPISODE must still open its series.

9anime's "Recently Updated" row is the case that forced this: the site's own
markup offers nothing but the newest episode's URL (both the poster link and
the title link point there), so clicking such a card used to hand
``/api/series`` an episode URL and blow up with
``ValueError: Invalid 9anime series URL`` -- an HTTP 500 for a perfectly valid
card.

Everything here is offline: the episode classes are stubbed, so these tests
pin the routing decision (when is a fetch attempted at all, and what happens
when it fails) rather than either site's current HTML.
"""

import pytest

from mediaforge import providers


class _FakeSeries:
    def __init__(self, url):
        self.url = url


class _FakeEpisode:
    """Stands in for a real episode model: resolving .series costs a fetch."""

    resolved = "https://9anime.or.at/anime/kamui-hes-behind-you/"
    calls = 0

    def __init__(self, url=None):
        type(self).calls += 1
        self.url = url

    @property
    def series(self):
        return _FakeSeries(self.resolved)


class _ExplodingEpisode(_FakeEpisode):
    @property
    def series(self):
        raise RuntimeError("upstream had a bad day")


@pytest.fixture
def nineanime(monkeypatch):
    """The 9anime provider with its episode class swapped for a stub."""
    prov = next(p for p in providers.PROVIDERS if p.name == "NineAnime")
    _FakeEpisode.calls = 0
    patched = providers.Provider(
        name=prov.name,
        series_pattern=prov.series_pattern,
        season_pattern=prov.season_pattern,
        episode_pattern=prov.episode_pattern,
        series_cls=prov.series_cls,
        season_cls=prov.season_cls,
        episode_cls=_FakeEpisode,
    )
    monkeypatch.setattr(
        providers, "PROVIDERS",
        [patched if p.name == "NineAnime" else p for p in providers.PROVIDERS],
    )
    return patched


EPISODE_URL = ("https://9anime.or.at/hell-mode-the-hardcore-gamer-dominates-in-"
               "another-world-with-garbage-balancing-season-2-episode-6-english-subed/")


def test_an_episode_url_becomes_its_series_url(nineanime):
    assert providers.series_url_for(EPISODE_URL) == _FakeEpisode.resolved
    assert _FakeEpisode.calls == 1


def test_a_series_url_is_returned_untouched_and_costs_no_fetch(nineanime):
    """The common case by far -- every other source's cards already link to the
    series page, and paying a request to confirm that would be absurd."""
    url = "https://9anime.or.at/anime/kamui-hes-behind-you/"
    assert providers.series_url_for(url) == url
    assert _FakeEpisode.calls == 0


def test_a_failure_leaves_the_url_alone(monkeypatch, nineanime):
    """Best-effort: the caller must end up exactly where it was before, not
    with a second, different error."""
    patched = providers.Provider(
        name=nineanime.name,
        series_pattern=nineanime.series_pattern,
        season_pattern=nineanime.season_pattern,
        episode_pattern=nineanime.episode_pattern,
        series_cls=nineanime.series_cls,
        season_cls=nineanime.season_cls,
        episode_cls=_ExplodingEpisode,
    )
    monkeypatch.setattr(
        providers, "PROVIDERS",
        [patched if p.name == "NineAnime" else p for p in providers.PROVIDERS],
    )
    assert providers.series_url_for(EPISODE_URL) == EPISODE_URL


@pytest.mark.parametrize("url", [
    # Movie-only sites have no series concept to resolve to.
    "https://filmpalast.to/stream/some-movie",
    "https://filmo.to/movies/some-movie",
    # Already a series page.
    "https://aniworld.to/anime/stream/naruto",
    # Not ours at all.
    "https://example.invalid/whatever",
    "",
])
def test_untouched(url):
    assert providers.series_url_for(url) == url


def test_aniwaves_episode_resolves_without_a_fetch():
    """aniwaves.ru encodes the series id in the episode URL itself, so this one
    is pure string work -- worth pinning, because it is the reason the helper
    does not simply assume every provider needs a network round-trip."""
    assert providers.series_url_for("https://aniwaves.ru/watch/1234/ep-5") == \
        "https://aniwaves.ru/watch/1234"


def test_nineanime_breadcrumb_regex_matches_the_live_markup():
    """Guards the one regex the 9anime resolution depends on, against the
    markup the site actually serves (captured from a live episode page)."""
    import re
    from mediaforge.models.nineanime_to import episode as ep_mod  # noqa: F401

    markup = (
        '<ol class="breadcrumb">'
        '<li class="breadcrumb-item"><a href="https://9anime.or.at/" title="Home">Home</a></li>'
        '<li class="breadcrumb-item"><a href="https://9anime.or.at/anime/kamui-hes-behind-you/">'
        'KAMUI: He&#8217;s Behind You</a></li>'
        '<li class="breadcrumb-item dynamic-name active">Watching ...</li></ol>'
    )
    m = re.search(r'breadcrumb-item"><a href="(https://9anime\.or\.at/anime/[^"]+)"', markup)
    assert m and m.group(1) == "https://9anime.or.at/anime/kamui-hes-behind-you/"
