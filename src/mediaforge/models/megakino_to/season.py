"""MegaKino season == one post page (``/<category>/<id>-<slug>.html``)."""
try:
    from ...config import MEGAKINO_SERIES_PATTERN
    from . import scraper
    from .episode import MegakinoEpisode
except ImportError:  # pragma: no cover
    from mediaforge.config import MEGAKINO_SERIES_PATTERN
    from mediaforge.models.megakino_to import scraper
    from mediaforge.models.megakino_to.episode import MegakinoEpisode


class MegakinoSeason:
    """A MegaKino /watch post IS the season -- there is exactly one season
    per series (see MegakinoSeries.seasons), episode-numbered via the
    payload's stream list rather than separate season pages."""

    # MegaKino series posts have no separate movie collection (that's what
    # MegakinoMovie is for, a wholly separate class/URL shape). Always False;
    # kept only so shared code paths that branch on `season.are_movies`
    # (AniWorld-style) don't need to special-case MegaKino.
    are_movies = False

    def __init__(self, url=None, series=None, season_number=None, _data=None):
        if not MEGAKINO_SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid MegaKino season URL: {url}")
        self.url = url
        self._series = series
        self.__season_number = season_number
        self.__data = _data
        self.__episodes = None
        self.__episode_count = None

    @property
    def _data(self):
        if self.__data is None:
            self.__data = scraper.fetch_watch(self.url)
        return self.__data

    @property
    def series(self):
        if self._series is None:
            from .series import MegakinoSeries
            self._series = MegakinoSeries(url=self.url, _data=self.__data)
        return self._series

    @property
    def season_number(self):
        if self.__season_number is None:
            self.__season_number = scraper.season_number(self._data)
        return self.__season_number

    @property
    def episode_count(self):
        if self.__episode_count is None:
            self.__episode_count = len(scraper.episode_numbers(self._data))
        return self.__episode_count

    @property
    def episodes(self):
        if self.__episodes is None:
            data = self._data
            eps = []
            for n in scraper.episode_numbers(data):
                hosters = scraper.episode_hosters(data, n)
                ep = MegakinoEpisode(
                    url=f"{self.url}?episode={n}",
                    series=self.series,
                    season=self,
                    episode_number=n,
                    provider_data={"German Dub": hosters} if hosters else {},
                    _data=data,
                )
                eps.append(ep)
            self.__episodes = eps
        return self.__episodes
