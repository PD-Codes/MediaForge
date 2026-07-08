"""MegaKino movie (megakino.to, tv=0). Flat single-file download, VOE by default."""
import os
import re
from pathlib import Path

try:
    from ...config import MEGAKINO_MOVIE_PATTERN, logger
    from ...extractors import provider_functions
    from ..common import check_downloaded
    from ..common.common import download as episode_download
    from ..common.common import syncplay as episode_syncplay
    from ..common.common import watch as episode_watch
    from . import scraper
except ImportError:  # pragma: no cover
    from mediaforge.config import MEGAKINO_MOVIE_PATTERN, logger
    from mediaforge.extractors import provider_functions
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import download as episode_download
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.megakino_to import scraper


class MegakinoMovie:
    """A standalone MegaKino movie page (megakino.to /watch/<slug>/<id>,
    no ``?episode=`` query param). NOT a superclass of MegakinoEpisode --
    they are distinguished purely by URL shape, see models/megakino_to/__init__.py.

    Used by: mediaforge.providers (Provider(name="MegakinoFilm", episode_cls=...))
    and web/routes/search.py (which also uses `isinstance(ep, MegakinoMovie)`
    to branch its live-availability check).
    """

    def __init__(self, url, selected_path=None, selected_language=None,
                 selected_provider=None, _data=None):
        if not MEGAKINO_MOVIE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid MegaKino movie URL: {url}")
        self.url = url
        self.__data = _data
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

    @property
    def _data(self):
        if self.__data is None:
            self.__data = scraper.fetch_watch(self.url)
        return self.__data

    @property
    def _meta(self):
        if self.__meta is None:
            self.__meta = scraper.parse_meta(self._data)
        return self.__meta

    @property
    def title_de(self):
        return self._meta.get("title") or ""

    @property
    def title(self):
        return self.title_de

    @property
    def release_year(self):
        return self._meta.get("year") or ""

    @property
    def genres(self):
        return self._meta.get("genres") or []

    @property
    def description(self):
        return self._meta.get("description") or ""

    @property
    def image_url(self):
        return self._meta.get("poster_url") or ""

    @property
    def imdb(self):
        return self._meta.get("imdb_id") or ""

    @property
    def provider_data(self):
        if self.__provider_data is None:
            hosters = scraper.movie_hosters(self._data)
            self.__provider_data = {"German Dub": hosters} if hosters else {}
        return self.__provider_data

    @property
    def available_providers(self):
        names = []
        for hosters in self.provider_data.values():
            for n in hosters:
                if n not in names:
                    names.append(n)
        return names

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
                f"Provider '{self.selected_provider}' not available for movie: {self.url}")
        return url

    @property
    def stream_url(self):
        try:
            fn = provider_functions[f"get_direct_link_from_{self.selected_provider.lower()}"]
        except KeyError:
            raise ValueError(f"The provider '{self.selected_provider}' is not yet implemented.")
        return fn(self.provider_url)

    @property
    def title_cleaned(self):
        t = re.sub(r'[<>:"/\\|?*]', "", self.title_de or "").strip()
        return t or "Film"

    @property
    def _base_folder(self):
        if self.__base_folder is None:
            self.__base_folder = Path(self.selected_path)
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            use_subfolder = (
                os.getenv("MEDIAFORGE_MOVIE_SUBFOLDER", "0") == "1"
                or os.getenv("MEGAKINO_MOVIE_SUBFOLDER", "0") == "1"
                or os.getenv("FILMPALAST_MOVIE_SUBFOLDER", "0") == "1"
            )
            if use_subfolder:
                self.__folder_path = self._base_folder / self._file_name
            else:
                self.__folder_path = self._base_folder
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

    def download(self, cancel_event=None, **kwargs):
        """Download this movie; VeeV is routed to its dedicated extractor
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
