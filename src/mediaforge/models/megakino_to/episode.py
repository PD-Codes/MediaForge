"""MegaKino episode (megakino.to). Synthetic URL: <watch-post>?episode=<n>."""
import os
import re
from pathlib import Path

try:
    from ...config import MEGAKINO_EPISODE_PATTERN, NAMING_TEMPLATE, logger
    from ...extractors import provider_functions
    from ..common import check_downloaded
    from ..common.common import download as episode_download
    from ..common.common import syncplay as episode_syncplay
    from ..common.common import watch as episode_watch
    from . import scraper
except ImportError:  # pragma: no cover
    from mediaforge.config import MEGAKINO_EPISODE_PATTERN, NAMING_TEMPLATE, logger
    from mediaforge.extractors import provider_functions
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import download as episode_download
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.megakino_to import scraper


class MegakinoEpisode:
    """One episode of a MegaKino series post (see module docstring for the
    episode-vs-movie URL convention). NOT a subclass of MegakinoMovie -- see
    models/megakino_to/__init__.py.

    Used by: mediaforge.providers (Provider(name="Megakino", episode_cls=...))
    and web/routes/search.py.
    """

    def __init__(self, url=None, series=None, season=None, episode_number=None,
                 title_de=None, title_en=None, provider_data=None, _data=None,
                 selected_path=None, selected_language=None, selected_provider=None):
        if not MEGAKINO_EPISODE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid MegaKino episode URL: {url}")
        self.url = url
        self._post_url = url.split("?")[0]
        self._series = series
        self._season = season
        self.__data = _data
        self.__episode_number = episode_number
        self.__title_de = title_de
        self.__title_en = title_en
        self.__provider_data = provider_data
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

    @property
    def _data(self):
        if self.__data is None:
            self.__data = scraper.fetch_watch(self._post_url)
        return self.__data

    @property
    def series(self):
        if self._series is None:
            from .series import MegakinoSeries
            self._series = MegakinoSeries(url=self._post_url, _data=self.__data)
        return self._series

    @property
    def season(self):
        if self._season is None:
            from .season import MegakinoSeason
            self._season = MegakinoSeason(url=self._post_url, series=self._series, _data=self.__data)
        return self._season

    @property
    def episode_number(self):
        if self.__episode_number is None:
            m = re.search(r"[?&]episode=(\d+)", self.url)
            self.__episode_number = int(m.group(1)) if m else 1
        return self.__episode_number

    @property
    def title_de(self):
        if self.__title_de is None:
            self.__title_de = f"Episode {self.episode_number}"
        return self.__title_de

    @property
    def title_en(self):
        return self.__title_en or self.title_de

    @property
    def provider_data(self):
        """MegaKino has no dub/sub language variants -- every hoster link is
        filed under a single synthetic "German Dub" key so this class exposes
        the same {language: {provider: url}} shape as AniWorld/s.to."""
        if self.__provider_data is None:
            hosters = scraper.episode_hosters(self._data, self.episode_number)
            self.__provider_data = {"German Dub": hosters} if hosters else {}
        return self.__provider_data

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
            self.__selected_language = self.__selected_language_param or "German Dub"
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None
        self.__base_folder = self.__folder_path = self.__episode_path = self.__file_name = None

    @property
    def selected_provider(self):
        if self.__selected_provider is None:
            raw = self.__selected_provider_param or os.getenv("MEDIAFORGE_PROVIDER", "VOE")
            self.__selected_provider = raw.replace(" HD", "").replace(" HQ", "").strip()
        return self.__selected_provider

    @property
    def provider_url(self):
        data = self.provider_data.get(self.selected_language) or {}
        if not data and self.provider_data:
            data = next(iter(self.provider_data.values()))
        url = data.get(self.selected_provider)
        if not url and data:
            url = next(iter(data.values()))
        if not url:
            raise ValueError(
                f"Provider '{self.selected_provider}' not available for episode: {self.url}")
        return url

    @property
    def stream_url(self):
        # Dispatch the extractor by the resolved provider_url host, not the
        # site's hoster label (mirrored labels / the "next available" fallback
        # in provider_url can yield another hoster's domain). See
        # extractors.get_direct_link_for.
        from ...extractors import get_direct_link_for
        return get_direct_link_for(self.provider_url, self.selected_provider)

    def _fmt(self, template_part):
        return template_part.format(
            title=self.series.title_cleaned,
            year=self.series.release_year,
            imdbid=self.series.imdb,
            season=f"{self.season.season_number:02d}",
            episode=f"{self.episode_number:03d}",
            language=self.selected_language,
        ).strip()

    @property
    def _base_folder(self):
        if self.__base_folder is None:
            parts = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE).split("/")
            self.__base_folder = (Path(self.selected_path) if len(parts) <= 1
                                  else Path(self.selected_path) / self._fmt(parts[0]))
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            parts = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE).split("/")
            self.__folder_path = (self._base_folder if len(parts) <= 2
                                  else self._base_folder / self._fmt(parts[1]))
        return self.__folder_path

    @property
    def _file_name(self):
        if self.__file_name is None:
            template = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE)
            file_template = template.split("/")[-1]
            if "." in file_template:
                file_template = ".".join(file_template.split(".")[:-1])
            for a, b in (("%title%", "{title}"), ("%year%", "{year}"), ("%imdbid%", "{imdbid}"),
                         ("%season%", "{season}"), ("%episode%", "{episode}"), ("%language%", "{language}")):
                file_template = file_template.replace(a, b)
            self.__file_name = self._fmt(file_template)
        return self.__file_name

    @property
    def _file_extension(self):
        if self.__file_extension is None:
            file_part = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE).split("/")[-1]
            self.__file_extension = file_part.rsplit(".", 1)[-1] if "." in file_part else "mkv"
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

    def download(self, cancel_event=None, **kwargs):
        """Download this episode; VeeV is routed to its dedicated extractor
        (TLS-fingerprint gated), everything else goes through the shared
        models/common/common.py download() pipeline."""
        if self.selected_provider.upper() == "VEEV":
            try:
                from ...extractors.provider.veev import download_from_veev
            except ImportError:
                from mediaforge.extractors.provider.veev import download_from_veev
            ep_label = os.path.splitext(self._file_name)[0] if self._file_name else ""
            os.makedirs(self._folder_path, exist_ok=True)
            download_from_veev(self.provider_url, self._episode_path,
                               cancel_event=cancel_event, label=ep_label)
        else:
            episode_download(self, cancel_event=cancel_event, **kwargs)

    watch = episode_watch
    syncplay = episode_syncplay
