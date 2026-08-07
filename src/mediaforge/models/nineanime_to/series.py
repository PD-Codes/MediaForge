"""9anime series == one ``/anime/<slug>/`` page.

Unlike AniWorld/s.to, 9anime does not split a long-running show into
site-side season pages under one series URL -- a sequel gets its own,
separate ``/anime/<slug>-season-2/`` entry instead (see "Solo Leveling
Season 2: Arise from the Shadow" in scraper.py's docstring examples). So,
same as hanime.tv, a 9anime "series" here wraps exactly one season.
"""
import re

try:
    from ...config import NINEANIME_SERIES_PATTERN
    from . import scraper
    from .season import NineAnimeSeason
except ImportError:  # pragma: no cover
    from mediaforge.config import NINEANIME_SERIES_PATTERN
    from mediaforge.models.nineanime_to import scraper
    from mediaforge.models.nineanime_to.season import NineAnimeSeason


class NineAnimeSeries:
    """A 9anime series/anime entry.

    Used by: mediaforge.providers (Provider(name="NineAnime", ...)) and
    web/routes/search.py (imported directly, same pattern as
    HanimeSeries/MegakinoSeries -- see models/__init__.py).
    """

    def __init__(self, url=None, _html=None):
        if not NINEANIME_SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid 9anime series URL: {url}")
        self.url = url.rstrip("/") + "/"
        self.__html = _html
        self.__meta = None
        self.__seasons = None

    @property
    def _html(self):
        if self.__html is None:
            self.__html = scraper.fetch_page(self.url)
        return self.__html

    @property
    def _meta(self):
        if self.__meta is None:
            self.__meta = scraper.parse_series_meta(self._html)
        return self.__meta

    @property
    def series_id(self):
        return self._meta.get("series_id")

    @property
    def title(self):
        return self._meta.get("title") or ""

    @property
    def title_jp(self):
        return self._meta.get("jname") or ""

    @property
    def title_cleaned(self):
        t = re.sub(r'[<>:"/\\|?*]', "", self.title or "").strip()
        return t or "Anime"

    @property
    def release_year(self):
        return self._meta.get("year") or ""

    @property
    def imdb(self):
        return ""  # 9anime content has no IMDb linkage on-site

    @property
    def poster_url(self):
        return self._meta.get("poster_url") or ""

    @property
    def description(self):
        return self._meta.get("description") or ""

    @property
    def genres(self):
        return self._meta.get("genres") or []

    @property
    def studios(self):
        return self._meta.get("studios") or []

    @property
    def status(self):
        return self._meta.get("status") or ""

    @property
    def score(self):
        return self._meta.get("score")

    @property
    def seasons(self):
        """Always exactly one season -- see module docstring."""
        if self.__seasons is None:
            self.__seasons = [NineAnimeSeason(url=self.url, series=self, _html=self._html)]
        return self.__seasons
