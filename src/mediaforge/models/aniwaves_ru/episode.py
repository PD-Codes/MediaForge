"""aniwaves.ru episode (flat synthetic URL, ``/watch/<series_id>/ep-<n>``).

Two ways an instance comes to exist:

  * via AniwavesSeason.episodes() -- episode_number and title are already
    known from the episode-list ajax call, series_id comes from the parent
    series, so no extra fetch needed until hoster resolution.
  * from a bare URL (a user pasting a direct episode link) -- both
    series_id and episode_number are parsed straight out of the URL itself
    (see ANIWAVES_EPISODE_PATTERN: the URL *is* ``<series_id>/ep-<n>``, no
    inline-JS id to dig out of the page like 9anime needs).
"""
import os
import re
from pathlib import Path

try:
    from ...config import (
        ANIWAVES_EPISODE_PATTERN,
        Audio,
        NAMING_TEMPLATE,
        Subtitles,
        logger,
    )
    from ..common import check_downloaded
    from ..common.common import download as episode_download
    from ..common.common import syncplay as episode_syncplay
    from ..common.common import watch as episode_watch
    from . import scraper
except ImportError:  # pragma: no cover
    from mediaforge.config import (
        ANIWAVES_EPISODE_PATTERN,
        Audio,
        NAMING_TEMPLATE,
        Subtitles,
        logger,
    )
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import download as episode_download
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.aniwaves_ru import scraper

# aniwaves.ru also offers a "kord" (Korean Dub) track per episode, but the
# project's Audio enum has no KOREAN member (see config.py) and adding one
# just for this single site is out of scope for this pass -- only Sub/Dub
# are modelled here, same two tracks 9anime.or.at exposes.
_TYPE_TO_LANG = {
    "sub": (Audio.JAPANESE, Subtitles.ENGLISH),  # "English Sub"
    "dub": (Audio.ENGLISH, Subtitles.NONE),      # "English Dub"
}
_LANG_LABELS = {
    (Audio.JAPANESE, Subtitles.ENGLISH): "English Sub",
    (Audio.ENGLISH, Subtitles.NONE): "English Dub",
}
_LABEL_TO_LANG = {v: k for k, v in _LANG_LABELS.items()}

# Only the "Vidplay" server is dispatched (-> extractors/provider/echovideo.py)
# -- see that module's docstring for why the site's other two servers aren't
# implemented.
_WORKING_SERVER_NAME = "vidplay"

_UNSET = object()


class AniwavesEpisode:
    """One aniwaves.ru episode.

    Used by: mediaforge.providers (Provider(name="Aniwaves", episode_cls=...))
    and web/routes/search.py, same as any other site's episode class.
    """

    def __init__(
        self, url=None, series=None, season=None, episode_number=None,
        title=None, selected_path=None,
        selected_language=None, selected_provider=None,
    ):
        if not ANIWAVES_EPISODE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid aniwaves.ru episode URL: {url}")
        self.url = url
        self._series = series
        self._season = season
        self.__episode_number = episode_number
        self.__title = title

        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_provider_param = selected_provider
        self.__selected_path = None
        self.__selected_language = None
        self.__selected_provider = None

        self.__series_id = _UNSET
        self.__servers_by_lang = None
        self.__provider_data = None

        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None
        self.__is_downloaded = None

    # -----------------------------
    # Fields parsed straight from the URL
    # -----------------------------
    @property
    def series_id(self):
        if self.__series_id is _UNSET:
            m = re.search(r"/watch/(\d+)/ep-\d+", self.url)
            self.__series_id = m.group(1) if m else None
        return self.__series_id

    @property
    def episode_number(self):
        if self.__episode_number is None:
            m = re.search(r"/ep-(\d+)", self.url)
            self.__episode_number = int(m.group(1)) if m else 1
        return self.__episode_number

    @property
    def title(self):
        if self.__title is None:
            self.__title = f"Episode {self.episode_number}"
        return self.__title

    # -----------------------------
    # Relations
    # -----------------------------
    @property
    def series(self):
        if self._series is None:
            from .series import AniwavesSeries
            if not self.series_id:
                raise ValueError(f"Could not resolve parent series for aniwaves.ru episode: {self.url}")
            self._series = AniwavesSeries(url=f"{scraper.base_url()}/watch/{self.series_id}")
        return self._series

    @property
    def season(self):
        if self._season is None:
            from .season import AniwavesSeason
            self._season = AniwavesSeason(url=self.series.url, series=self._series)
        return self._season

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
        """"English Sub" or "English Dub" -- see _LANG_LABELS. Defaults to
        Sub, same reasoning as NineAnimeEpisode: broader, more reliable
        coverage than Dub on fansub-style aggregators."""
        if self.__selected_language is None:
            self.__selected_language = self.__selected_language_param or "English Sub"
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None
        self.__base_folder = self.__folder_path = self.__episode_path = self.__file_name = None

    @property
    def selected_provider(self):
        if self.__selected_provider is None:
            self.__selected_provider = self.__selected_provider_param or os.getenv(
                "MEDIAFORGE_PROVIDER", "EchoVideo")
        return self.__selected_provider

    # -----------------------------
    # Hoster data
    # -----------------------------
    @property
    def _servers_by_lang(self):
        """{(Audio, Subtitles): [{"name","server_id","link_id"}]} -- only
        "Vidplay" entries (see _WORKING_SERVER_NAME / module docstring)."""
        if self.__servers_by_lang is None:
            raw = scraper.fetch_episode_servers(self.series_id, self.episode_number, referer=self.url)
            data = {}
            for type_, servers in raw.items():
                lang = _TYPE_TO_LANG.get(type_)
                if lang is None:
                    continue
                working = [s for s in servers if s["name"].strip().lower() == _WORKING_SERVER_NAME]
                if working:
                    data[lang] = working
            self.__servers_by_lang = data
        return self.__servers_by_lang

    @property
    def provider_data(self):
        """{(Audio, Subtitles): {"EchoVideo": link_id}} -- kept for interface
        parity with other site models (available_languages/provider_link use
        it), but the value is an opaque *link_id* here, not a ready embed
        URL: aniwaves only exposes those through a second, per-hoster
        /ajax/sources call (see scraper.resolve_source). That resolution
        happens lazily in provider_url (not here) so building this property
        never fires the extra request for a language you don't end up
        selecting -- same laziness as models/filmo_to/movie.py's
        _selected_chip."""
        if self.__provider_data is None:
            self.__provider_data = {
                lang: {"EchoVideo": servers[0]["link_id"]}
                for lang, servers in self._servers_by_lang.items()
            }
        return self.__provider_data

    @property
    def available_languages(self):
        return [_LANG_LABELS.get(k, k) for k in self.provider_data]

    def provider_link(self, language=None, provider=None):
        """The unresolved link_id, not a usable URL -- see provider_url."""
        lang_label = language or self.selected_language
        lang_key = _LABEL_TO_LANG.get(lang_label, lang_label)
        provider_dict = self.provider_data.get(lang_key) or {}
        return provider_dict.get(provider or self.selected_provider)

    @property
    def provider_url(self):
        """Resolves the selected language's Vidplay link_id to the actual
        embed URL via scraper.resolve_source(), falling back to any
        available language -- same "don't dead-end on a picky combination"
        behaviour as MegakinoMovie/FilmoMovie/NineAnimeEpisode.provider_url."""
        data = self.provider_data
        lang_key = _LABEL_TO_LANG.get(self.selected_language, self.selected_language)
        hosters = data.get(lang_key) or {}
        if not hosters and data:
            hosters = next(iter(data.values()))
        link_id = hosters.get(self.selected_provider)
        if not link_id and hosters:
            link_id = next(iter(hosters.values()))
        if not link_id:
            raise ValueError(
                f"Language '{self.selected_language}' with provider "
                f"'{self.selected_provider}' is not available for episode: {self.url}"
            )
        return scraper.resolve_source(link_id, referer=self.url)

    @property
    def stream_url(self):
        # Dispatched by the resolved embed host (HOST_PROVIDER_MAP), not the
        # site's server label -- same convention as every other site model.
        from ...extractors import get_direct_link_for
        return get_direct_link_for(self.provider_url, self.selected_provider)

    # -----------------------------
    # Filesystem paths
    # -----------------------------
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
            self.__base_folder = (
                Path(self.selected_path) if len(parts) <= 1
                else Path(self.selected_path) / self._fmt(parts[0])
            )
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            parts = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE).split("/")
            self.__folder_path = self._base_folder if len(parts) <= 2 else self._base_folder / self._fmt(parts[1])
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

    # Episode actions are implemented in mediaforge.models.common.common
    download = episode_download
    watch = episode_watch
    syncplay = episode_syncplay
