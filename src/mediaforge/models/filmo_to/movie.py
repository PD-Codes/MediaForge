"""Filmo movie (filmo.to /movies/<slug>). Flat single-file download.

Movies only -- filmo.to has no series/season concept, same shape as
FilmPalastEpisode. Multiple languages ARE offered (unlike MegaKino, which is
German-only): see scraper.parse_provider_rows.
"""
import os
import re
from pathlib import Path

try:
    from ...config import FILMO_MOVIE_PATTERN, logger
    from ..common import check_downloaded
    from ..common.common import download as episode_download
    from ..common.common import syncplay as episode_syncplay
    from ..common.common import watch as episode_watch
    from . import scraper
except ImportError:  # pragma: no cover
    from mediaforge.config import FILMO_MOVIE_PATTERN, logger
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import download as episode_download
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.filmo_to import scraper

# imdb() hasn't been resolved yet vs. "resolved to nothing" -- see that
# property. None is a legitimate cached result (TMDB unconfigured or no hit).
_UNSET = object()


class FilmoMovie:
    """A standalone filmo.to movie page.

    Used by: mediaforge.providers (Provider(name="Filmo", episode_cls=...)).
    """

    def __init__(self, url, selected_path=None, selected_language=None,
                 selected_provider=None):
        if not FILMO_MOVIE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid Filmo movie URL: {url}")
        self.url = url

        self.__html = None
        self.__csrf = None
        self.__meta = None
        self.__provider_data = None

        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_provider_param = selected_provider
        self.__selected_path = None
        self.__selected_language = None
        self.__selected_provider = None

        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None
        self.__is_downloaded = None

        self.__imdb = _UNSET

    # -----------------------------
    # Page fetch (HTML + CSRF share one request; both are needed to mint a
    # provider URL later, so they're cached together).
    # -----------------------------
    @property
    def _html(self):
        if self.__html is None:
            self.__html, self.__csrf = scraper.fetch_movie_page(self.url)
        return self.__html

    @property
    def _csrf(self):
        if self.__csrf is None:
            _ = self._html  # populates both
        return self.__csrf

    @property
    def _meta(self):
        if self.__meta is None:
            self.__meta = scraper.parse_meta(self._html)
        return self.__meta

    # -----------------------------
    # Metadata
    # -----------------------------
    @property
    def title(self):
        return self._meta.get("title") or ""

    @property
    def title_cleaned(self):
        t = re.sub(r'[<>:"/\\|?*]', "", self.title or "").strip()
        return t or "Film"

    @property
    def release_year(self):
        return self._meta.get("year") or ""

    @property
    def runtime_minutes(self):
        return self._meta.get("runtime_minutes")

    @property
    def genres(self):
        return self._meta.get("genres") or []

    @property
    def directors(self):
        return self._meta.get("directors") or []

    @property
    def cast(self):
        return self._meta.get("cast") or []

    @property
    def description(self):
        return self._meta.get("description") or ""

    @property
    def image_url(self):
        return self._meta.get("poster_url") or ""

    @property
    def rating(self):
        """filmo.to's own (crowd-sourced) rating out of 10, or None."""
        return self._meta.get("rating")

    @property
    def imdb(self):
        """IMDb id ("tt...."), or None.

        filmo.to shows an "IMDb x.x/10" rating but never links or embeds the
        id itself -- unlike AniWorld/s.to (scraped straight from an
        <a data-imdb> link) or MegaKino (returned by its own JSON API), there
        is nothing to scrape here. Resolved instead via the app's existing
        TMDB integration (web.tmdb_cache), which already keys off IMDb ids
        for FSK/provider lookups elsewhere -- avoids standing up a second
        title-matching heuristic just for this one field.

        Requires a configured TMDB API key (Settings > CineInfo); returns
        None without one, same as an unmatched title, so this never raises.
        Lazily imported and broadly guarded, same reasoning as
        aniworld_to/episode.py's `_get_setting`: models/* also runs from the
        CLI, where the web/Flask stack may not be importable at all.
        """
        if self.__imdb is _UNSET:
            self.__imdb = self.__resolve_imdb()
        return self.__imdb

    def __resolve_imdb(self):
        try:
            from ...web.tmdb_cache import is_tmdb_configured, lookup_media
        except Exception:
            try:
                from mediaforge.web.tmdb_cache import is_tmdb_configured, lookup_media
            except Exception:
                return None
        try:
            if not is_tmdb_configured():
                return None
            info = lookup_media(title=self.title, media_type="movie", require_confident=True)
        except Exception as exc:
            logger.debug("Filmo IMDb lookup failed for %r: %s", self.title, exc)
            return None
        if not info:
            return None
        try:
            return ((info.get("raw_details") or {}).get("external_ids") or {}).get("imdb_id") or None
        except Exception:
            return None

    # -----------------------------
    # Provider data: {language_label: {hoster: {"data_p", "movie_link_id"}}}
    # -----------------------------
    @property
    def provider_data(self):
        if self.__provider_data is None:
            self.__provider_data = scraper.parse_provider_rows(self._html)
        return self.__provider_data

    @property
    def available_languages(self):
        return list(self.provider_data.keys())

    @property
    def available_providers(self):
        names = []
        for hosters in self.provider_data.values():
            for n in hosters:
                if n not in names:
                    names.append(n)
        return names

    # -----------------------------
    # Selection
    # -----------------------------
    @property
    def selected_path(self):
        if self.__selected_path is None:
            raw = self.__selected_path_param or os.getenv(
                "MEDIAFORGE_DOWNLOAD_PATH", str(Path.home() / "Downloads"))
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            self.__selected_path = str(p)
        return self.__selected_path

    @selected_path.setter
    def selected_path(self, value):
        self.__selected_path_param = value
        self.__selected_path = None
        self.__base_folder = self.__folder_path = self.__episode_path = None

    @property
    def selected_language(self):
        if self.__selected_language is None:
            self.__selected_language = self.__selected_language_param or os.getenv(
                "MEDIAFORGE_LANGUAGE", "German Dub")
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None

    @property
    def selected_provider(self):
        if self.__selected_provider is None:
            raw = self.__selected_provider_param or os.getenv("MEDIAFORGE_PROVIDER", "VOE")
            self.__selected_provider = raw.replace(" HD", "").replace(" HQ", "").strip()
        return self.__selected_provider

    # -----------------------------
    # Hoster resolution (mint flow -- see scraper.resolve_provider_url)
    # -----------------------------
    @property
    def _selected_chip(self):
        """{"data_p", "movie_link_id"} for the selected language+provider,
        falling back to any available language / any available provider the
        same way MegakinoMovie.provider_url does -- a language toggle that
        silently has nothing for the current provider must not dead-end."""
        data = self.provider_data.get(self.selected_language) or {}
        if not data and self.provider_data:
            data = next(iter(self.provider_data.values()))
        chip = data.get(self.selected_provider)
        if not chip and data:
            chip = next(iter(data.values()))
        if not chip:
            raise ValueError(
                f"Provider '{self.selected_provider}' not available for movie: {self.url}")
        return chip

    @property
    def provider_url(self):
        """The resolved hoster URL (e.g. https://voe.sx/e/<id>) -- mints a
        fresh one-shot token on every access, since filmo.to's tokens are
        short-lived and tied to the chip that produced them."""
        chip = self._selected_chip
        return scraper.resolve_provider_url(chip["data_p"], self._csrf, self.url)

    @property
    def stream_url(self):
        # Dispatch the extractor by the resolved provider_url host, not the
        # site's hoster label -- see extractors.get_direct_link_for.
        from ...extractors import get_direct_link_for
        return get_direct_link_for(self.provider_url, self.selected_provider)

    # -----------------------------
    # Filesystem paths
    # -----------------------------
    @property
    def _base_folder(self):
        if self.__base_folder is None:
            self.__base_folder = Path(self.selected_path)
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            use_subfolder = os.getenv("MEDIAFORGE_MOVIE_SUBFOLDER", "0") == "1"
            self.__folder_path = (
                self._base_folder / self._file_name if use_subfolder else self._base_folder
            )
        return self.__folder_path

    @property
    def _file_name(self):
        if self.__file_name is None:
            year = self.release_year
            suffix = f" ({year})" if year else ""
            self.__file_name = f"{self.title_cleaned}{suffix}"
        return self.__file_name

    @property
    def _file_extension(self):
        if self.__file_extension is None:
            self.__file_extension = "mkv"
        return self.__file_extension

    @property
    def _episode_path(self):
        if self.__episode_path is None:
            self.__episode_path = self._folder_path / f"{self._file_name}.{self._file_extension}"
        return self.__episode_path

    @property
    def is_downloaded(self):
        if self.__is_downloaded is None:
            self.__is_downloaded = check_downloaded(self._episode_path)
        return self.__is_downloaded

    # Movie actions are implemented in mediaforge.models.common.common
    download = episode_download
    watch = episode_watch
    syncplay = episode_syncplay
