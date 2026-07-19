"""Example Source models -- offline-safe reference for providers.register_provider().

Mirrors the property/method names of a real site's Series/Season/Episode
trio (see models/megakino_to/series.py, season.py, episode.py for the real
thing this is modeled on) closely enough to register with
providers.register_provider() and search.register_search_source() and be
browsable/searchable end to end. All metadata below is synthesized locally
-- there is no network call anywhere in this file, so the example always
works offline and is safe to enable.

download()/stream_url are the one place this deliberately stops short of a
real module: there is no real site behind example-source.invalid to
download from, so ExampleSourceEpisode.download() raises instead of
pretending to succeed -- see its docstring for what a real module does
there instead (resolve provider_url through
extractors.get_direct_link_for(...), then the shared models/common/
common.py download() pipeline every built-in site uses).
"""
from __future__ import annotations

import re

# The demo domain uses the .invalid TLD (RFC 2606) -- reserved for exactly
# this purpose, so it can never resolve to a real site by accident.
SERIES_PATTERN = re.compile(r"^https?://example-source\.invalid/serie/([a-z0-9\-]+)/?$")
SEASON_PATTERN = re.compile(r"^https?://example-source\.invalid/serie/([a-z0-9\-]+)/staffel-(\d+)/?$")
EPISODE_PATTERN = re.compile(r"^https?://example-source\.invalid/serie/([a-z0-9\-]+)/staffel-(\d+)/episode-(\d+)/?$")

# The entire "catalog" this example serves. A real module fetches this from
# the actual site instead of hardcoding it.
_CATALOG = {
    "example-series": {
        "title": "Example Series",
        "release_year": "2024",
        "description": "Placeholder series used to demonstrate providers.register_provider() end to end.",
        "genres": ["Demo"],
        "poster_url": "",
        "episode_count": 3,
    },
}
_DEFAULT_SLUG = "example-series"


def search(keyword: str) -> list:
    """Keyword search over the local catalog -- registered via
    search.register_search_source(). Returns the same {"title", "url"} shape
    every built-in search does."""
    keyword = (keyword or "").strip().lower()
    results = []
    for slug, data in _CATALOG.items():
        if not keyword or keyword in data["title"].lower():
            results.append({"title": data["title"], "url": f"https://example-source.invalid/serie/{slug}"})
    return results


def _slug_from_url(url: str) -> str:
    m = SERIES_PATTERN.match(url) or SEASON_PATTERN.match(url) or EPISODE_PATTERN.match(url)
    return (m.group(1) if m else "") or _DEFAULT_SLUG


class ExampleSourceSeries:
    """See models/megakino_to/series.py for the real-site equivalent this
    mirrors -- same property names, since providers.py and web/routes/*.py
    call these on any registered provider's series_cls without caring
    whether it's built-in or third-party."""

    def __init__(self, url=None, _data=None):
        if not SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid Example Source series URL: {url}")
        self.url = url
        self._data = _data or _CATALOG.get(_slug_from_url(url), _CATALOG[_DEFAULT_SLUG])

    @property
    def title(self):
        return self._data["title"]

    @property
    def title_cleaned(self):
        return re.sub(r'[<>:"/\\|?*]', "", self.title).strip() or "Series"

    @property
    def release_year(self):
        return self._data.get("release_year", "")

    @property
    def imdb(self):
        return ""  # no real IMDb id for a demo series

    @property
    def poster_url(self):
        return self._data.get("poster_url", "")

    @property
    def description(self):
        return self._data.get("description", "")

    @property
    def genres(self):
        return list(self._data.get("genres", []))

    @property
    def seasons(self):
        """Always exactly one season, same simplification MegaKino makes for
        real (see MegakinoSeries.seasons) -- nothing about
        providers.register_provider() requires more than one."""
        return [ExampleSourceSeason(
            url=f"{self.url.rstrip('/')}/staffel-1",
            series=self, season_number=1, _data=self._data,
        )]


class ExampleSourceSeason:
    # This example has no separate movie collection -- kept only so shared
    # code paths that branch on `season.are_movies` (AniWorld-style) don't
    # need to special-case this example, same reason MegaKino sets it.
    are_movies = False

    def __init__(self, url=None, series=None, season_number=None, _data=None):
        if not SEASON_PATTERN.match(url or ""):
            raise ValueError(f"Invalid Example Source season URL: {url}")
        self.url = url
        self._series = series
        self._data = _data or _CATALOG.get(_slug_from_url(url), _CATALOG[_DEFAULT_SLUG])
        self.season_number = season_number or 1

    @property
    def series(self):
        if self._series is None:
            series_url = re.sub(r"/staffel-\d+/?$", "", self.url)
            self._series = ExampleSourceSeries(url=series_url, _data=self._data)
        return self._series

    @property
    def episode_count(self):
        return self._data.get("episode_count", 0)

    @property
    def episodes(self):
        return [
            ExampleSourceEpisode(
                url=f"{self.url.rstrip('/')}/episode-{n}",
                series=self.series, season=self, episode_number=n,
            )
            for n in range(1, self.episode_count + 1)
        ]


class ExampleSourceEpisode:
    """Used by: providers.py (Provider(name="ExampleSource", episode_cls=...))
    and web/routes/search.py -- exactly like a built-in provider's episode
    class, see models/megakino_to/episode.py for the real-site version of
    every property/method below."""

    def __init__(self, url=None, series=None, season=None, episode_number=None,
                 selected_path=None, selected_language=None, selected_provider=None):
        if not EPISODE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid Example Source episode URL: {url}")
        self.url = url
        self._series = series
        self._season = season
        self._episode_number = episode_number
        self.selected_path = selected_path
        self.selected_language = selected_language or "German Dub"
        self.selected_provider = selected_provider or "ExampleHoster"

    @property
    def series(self):
        if self._series is None:
            self._series = self.season.series
        return self._series

    @property
    def season(self):
        if self._season is None:
            self._season = ExampleSourceSeason(url=re.sub(r"/episode-\d+/?$", "", self.url))
        return self._season

    @property
    def episode_number(self):
        if self._episode_number is None:
            m = re.search(r"/episode-(\d+)/?$", self.url)
            self._episode_number = int(m.group(1)) if m else 1
        return self._episode_number

    @property
    def title_de(self):
        return f"Episode {self.episode_number}"

    @property
    def title_en(self):
        return self.title_de

    @property
    def provider_data(self):
        """{language: {hoster_label: url}} -- the same shape every built-in
        site's episode.provider_data returns. A real module fills this from
        the parsed episode page; here it's a fixed placeholder resolved by
        nothing (see download() below)."""
        return {"German Dub": {"ExampleHoster": f"{self.url}#stream"}}

    @property
    def provider_url(self):
        data = self.provider_data.get(self.selected_language) or {}
        if not data:
            raise ValueError(f"No provider data for language: {self.selected_language}")
        url = data.get(self.selected_provider) or next(iter(data.values()))
        return url

    @property
    def is_downloaded(self):
        return False

    def download(self, cancel_event=None, **kwargs):
        """Deliberately NOT implemented -- there is no real site behind
        example-source.invalid to download from. A real module's download()
        resolves provider_url through extractors.get_direct_link_for(...)
        (see models/megakino_to/episode.py for the full real pattern,
        including the shared models/common/common.py download() pipeline
        every built-in site uses) and writes the file. Left raising so this
        is never mistaken for a working downloader."""
        raise NotImplementedError(
            "example_content_source is a browsing/search reference only -- "
            "see ExampleSourceEpisode.download()'s docstring for what a real "
            "module does instead."
        )
