"""aniwaves.ru season -- the series' ordered episode list (single season; see
series.py's docstring for why a sequel is a separate series instead of a
second season here)."""
try:
    from ...config import ANIWAVES_SERIES_PATTERN
    from . import scraper
    from .episode import AniwavesEpisode
except ImportError:  # pragma: no cover
    from mediaforge.config import ANIWAVES_SERIES_PATTERN
    from mediaforge.models.aniwaves_ru import scraper
    from mediaforge.models.aniwaves_ru.episode import AniwavesEpisode


class AniwavesSeason:
    """Wraps one aniwaves.ru series' episode list. Always season_number == 1."""

    # aniwaves.ru has no movie concept of its own -- see HanimeSeason/
    # NineAnimeSeason for why this attribute exists at all (shared code
    # paths that branch on `season.are_movies`).
    are_movies = False

    def __init__(self, url=None, series=None, season_number=None, _html=None):
        if not ANIWAVES_SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid aniwaves.ru season URL: {url}")
        self.url = url.rstrip("/")
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
            from .series import AniwavesSeries
            self._series = AniwavesSeries(url=self.url, _html=self._html)
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
                eps.append(AniwavesEpisode(
                    url=meta["url"],
                    series=self.series,
                    season=self,
                    episode_number=meta["number"],
                    title=meta.get("title"),
                ))
            self.__episodes = eps
        return self.__episodes
