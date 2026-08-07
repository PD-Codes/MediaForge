"""aniwaves.ru series == one ``/watch/<slug>-<id>`` page.

Like 9anime (see models/nineanime_to/series.py), a sequel gets its own,
separate ``/watch/<slug>-season-2-<id>`` entry rather than a second season
under the same series URL -- so a "series" here also wraps exactly one
season.
"""
try:
    from ...config import ANIWAVES_SERIES_PATTERN
    from . import scraper
    from .season import AniwavesSeason
except ImportError:  # pragma: no cover
    from mediaforge.config import ANIWAVES_SERIES_PATTERN
    from mediaforge.models.aniwaves_ru import scraper
    from mediaforge.models.aniwaves_ru.season import AniwavesSeason


class AniwavesSeries:
    """An aniwaves.ru series/anime entry.

    Used by: mediaforge.providers (Provider(name="Aniwaves", ...)) and
    web/routes/search.py, same pattern as NineAnimeSeries/HanimeSeries.
    """

    def __init__(self, url=None, _html=None):
        if not ANIWAVES_SERIES_PATTERN.match(url or ""):
            raise ValueError(f"Invalid aniwaves.ru series URL: {url}")
        self.url = url.rstrip("/")
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
        return self._meta.get("series_id") or scraper.series_id_from_url(self.url)

    @property
    def title(self):
        return self._meta.get("title") or ""

    @property
    def title_alt(self):
        return self._meta.get("title_alt") or ""

    @property
    def title_cleaned(self):
        import re
        t = re.sub(r'[<>:"/\\|?*]', "", self.title or "").strip()
        return t or "Anime"

    @property
    def release_year(self):
        return self._meta.get("year") or ""

    @property
    def imdb(self):
        return ""  # aniwaves.ru content has no IMDb linkage on-site

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
        """schema.org aggregateRating value -- a 0-5 scale (not the 0-10
        badge shown in the site's own UI, which is computed differently and
        not exposed anywhere in the page's static/LD-JSON data)."""
        return self._meta.get("score")

    @property
    def content_rating(self):
        return self._meta.get("content_rating") or ""

    @property
    def seasons(self):
        """Always exactly one season -- see module docstring."""
        if self.__seasons is None:
            self.__seasons = [AniwavesSeason(url=self.url, series=self, _html=self._html)]
        return self.__seasons
