"""9anime season -- the series' ordered episode list (single season; see
series.py's docstring for why a sequel is a separate series instead of a
second season here)."""
try:
    from ...config import NINEANIME_SERIES_PATTERN
    from . import scraper
    from .episode import NineAnimeEpisode
except ImportError:  # pragma: no cover
    from mediaforge.config import NINEANIME_SERIES_PATTERN
    from mediaforge.models.nineanime_to import scraper
    from mediaforge.models.nineanime_to.episode import NineAnimeEpisode


class NineAnimeSeason:
    """Wraps one 9anime series' episode list. Always season_number == 1."""

    # 9anime has no movie concept of its own (the site's "Movies" filter is
    # just a type facet on the same series shape) -- see HanimeSeason for why
    # this attribute exists at all (shared code paths that branch on
    # `season.are_movies`).
    are_movies = False

    def __init__(self, url=None, series=None, season_number=None, _html=None):
        if not NINEANIME_SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid 9anime season URL: {url}")
        self.url = url.rstrip("/") + "/"
        self._series = series
        self.__season_number = season_number or 1
        self.__html = _html
        self.__episodes = None

    @property
    def _html(self):
        if self.__html is None:
            self.__html = scraper.fetch_page(self.url)
        return self.__html

    @property
    def series(self):
        if self._series is None:
            from .series import NineAnimeSeries
            self._series = NineAnimeSeries(url=self.url, _html=self._html)
        return self._series

    @property
    def season_number(self):
        return self.__season_number

    @property
    def episode_count(self):
        return len(self.episodes)

    @property
    def episodes(self):
        if self.__episodes is None:
            series_id = self.series.series_id
            eps_meta = scraper.fetch_episode_list(series_id) if series_id else []
            eps = []
            for meta in eps_meta:
                eps.append(NineAnimeEpisode(
                    url=meta["url"],
                    series=self.series,
                    season=self,
                    episode_number=meta["number"],
                    episode_id=meta["id"],
                    title=meta.get("title"),
                ))
            self.__episodes = eps
        return self.__episodes
