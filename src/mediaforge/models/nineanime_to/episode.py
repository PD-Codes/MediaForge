"""9anime episode (flat synthetic URL, e.g. .../<slug>-episode-<n>-english-subbed/).

Two ways an instance comes to exist:

  * via NineAnimeSeason.episodes() -- episode_id/episode_number/title are
    already known from the episode-list REST call, so no extra fetch needed
    until hoster resolution.
  * from a bare URL (a user pasting a direct episode link) -- episode_id,
    the parent series URL and the episode number are all lazily resolved
    from the episode page itself (the theme embeds them as a small inline
    ``tsEp = {...}`` JS object -- see __extract_page_ids()), mirroring how
    AniworldEpisode resolves its own lazy fields from a bare URL.
"""
import os
import re
from pathlib import Path

try:
    from ...config import (
        Audio,
        NAMING_TEMPLATE,
        NINEANIME_EPISODE_PATTERN,
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
        Audio,
        NAMING_TEMPLATE,
        NINEANIME_EPISODE_PATTERN,
        Subtitles,
        logger,
    )
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import download as episode_download
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.nineanime_to import scraper

# 9anime only ever offers these two tracks -- same semantic pairs AniWorld's
# LANG_KEY_MAP already defines ("2" and "4"), reused here rather than
# inventing a third (Audio, Subtitles) enum pair just for this site.
_TYPE_TO_LANG = {
    "sub": (Audio.JAPANESE, Subtitles.ENGLISH),  # "English Sub"
    "dub": (Audio.ENGLISH, Subtitles.NONE),      # "English Dub"
}
_LANG_TO_TYPE = {v: k for k, v in _TYPE_TO_LANG.items()}
_LANG_LABELS = {
    (Audio.JAPANESE, Subtitles.ENGLISH): "English Sub",
    (Audio.ENGLISH, Subtitles.NONE): "English Dub",
}
_LABEL_TO_LANG = {v: k for k, v in _LANG_LABELS.items()}

_UNSET = object()


def _canonical_hoster(embed_url, site_label):
    """The hoster name for one server entry.

    9anime labels its servers by QUALITY, not by hoster -- every entry is
    called "HD" -- while actually embedding several different ones (MegaPlay,
    and the site's own player on my.1anime.site). Keying provider_data by that
    label would mean the name the user picks in the dropdown ("OneAnime") never
    matches the key stored here, so provider_link() would look up "OneAnime" in
    a dict whose only key is "HD" and come back empty.

    So the host decides, exactly like the extractor dispatch does
    (extractors.get_direct_link_for resolves by host first). The site's label
    is kept only when the host is unknown -- better a name that at least
    matches what the site showed than a blank one.
    """
    try:
        from ...extractors import provider_for_url
    except ImportError:  # pragma: no cover
        from mediaforge.extractors import provider_for_url
    try:
        from ...config import SUPPORTED_PROVIDERS
    except ImportError:  # pragma: no cover
        from mediaforge.config import SUPPORTED_PROVIDERS

    key = provider_for_url(embed_url)
    if key:
        for name in SUPPORTED_PROVIDERS:
            if name.lower() == key:
                return name
    return site_label


class NineAnimeEpisode:
    """One 9anime episode.

    Used by: mediaforge.providers (Provider(name="NineAnime", episode_cls=...))
    and web/routes/search.py, same as any other site's episode class.
    """

    def __init__(
        self, url=None, series=None, season=None, episode_number=None,
        episode_id=None, title=None, selected_path=None,
        selected_language=None, selected_provider=None,
    ):
        if not NINEANIME_EPISODE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid 9anime episode URL: {url}")
        self.url = url
        self._series = series
        self._season = season
        self.__episode_number = episode_number
        self.__episode_id = episode_id
        self.__title = title

        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_provider_param = selected_provider
        self.__selected_path = None
        self.__selected_language = None
        self.__selected_provider = None

        self.__html = None
        self.__series_url = _UNSET
        self.__provider_data = None

        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None
        self.__is_downloaded = None

    # -----------------------------
    # Lazy resolution from a bare episode URL
    # -----------------------------
    @property
    def _html(self):
        """Only fetched when something wasn't already known at construction
        time (episode_id, episode_number or the parent series) -- an episode
        built via NineAnimeSeason.episodes() never touches this."""
        if self.__html is None:
            self.__html = scraper.fetch_page(self.url)
        return self.__html

    def __extract_page_ids(self):
        m_ep = re.search(r"episodeId\s*:\s*(\d+)", self._html)
        m_num = re.search(r'data-number="(\d+)"[^>]*class="[^"]*active', self._html)
        return (m_ep.group(1) if m_ep else None), (m_num.group(1) if m_num else None)

    @property
    def episode_id(self):
        if self.__episode_id is None:
            self.__episode_id, _ = self.__extract_page_ids()
            if self.__episode_id is None:
                raise ValueError(f"Could not resolve 9anime episode id for: {self.url}")
        return self.__episode_id

    @property
    def episode_number(self):
        if self.__episode_number is None:
            m = re.search(r"-episode-(\d+)", self.url)
            self.__episode_number = int(m.group(1)) if m else 1
        return self.__episode_number

    @property
    def title(self):
        if self.__title is None:
            self.__title = f"Episode {self.episode_number}"
        return self.__title

    # 9anime is English-only -- there is no separate German title to scrape,
    # so both aliases just return the one title. Exist purely so the generic
    # `getattr(ep, "title_de"/"title_en", "")` fallback in
    # web/routes/search.py's api_episodes() (written for AniWorld/s.to, which
    # DO have distinct German/English titles) finds something instead of "".
    @property
    def title_de(self):
        return self.title

    @property
    def title_en(self):
        return self.title

    @property
    def _series_url(self):
        if self.__series_url is _UNSET:
            m = re.search(r'breadcrumb-item"><a href="(https://9anime\.or\.at/anime/[^"]+)"', self._html)
            self.__series_url = m.group(1) if m else None
        return self.__series_url

    # -----------------------------
    # Relations
    # -----------------------------
    @property
    def series(self):
        if self._series is None:
            from .series import NineAnimeSeries
            series_url = self._series_url
            if not series_url:
                raise ValueError(f"Could not resolve parent series for 9anime episode: {self.url}")
            self._series = NineAnimeSeries(url=series_url)
        return self._series

    @property
    def season(self):
        if self._season is None:
            from .season import NineAnimeSeason
            self._season = NineAnimeSeason(url=self.series.url, series=self._series)
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
        Sub: 9anime (like most fansub aggregators) has broader, more
        reliable Sub coverage than Dub."""
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
            # "Megaplay" as the written-down default is historical: 9anime
            # embedded only MegaPlay when this model was written. It now
            # serves most episodes through its own player instead, so the
            # name is just a preference -- provider_link()/provider_url()
            # both fall back to whatever the episode actually offers.
            self.__selected_provider = self.__selected_provider_param or os.getenv(
                "MEDIAFORGE_PROVIDER", "Megaplay")
        return self.__selected_provider

    # -----------------------------
    # Hoster data
    # -----------------------------
    @property
    def provider_data(self):
        """{(Audio, Subtitles): {hoster_name: embed_url}} -- same shape as
        AniworldEpisode.provider_data, built from scraper.fetch_episode_servers()."""
        if self.__provider_data is None:
            raw = scraper.fetch_episode_servers(self.episode_id, referer=self.url)
            data = {}
            for type_, servers in raw.items():
                lang = _TYPE_TO_LANG.get(type_)
                if lang is None:
                    continue
                data[lang] = {
                    _canonical_hoster(s["embed_url"], s["name"]): s["embed_url"]
                    for s in servers
                }
            self.__provider_data = data
        return self.__provider_data

    @property
    def available_languages(self):
        return [_LANG_LABELS.get(k, k) for k in self.provider_data]

    def provider_link(self, language=None, provider=None):
        """The embed URL for one language+hoster, or None.

        Falls back to whatever the episode DOES offer instead of returning
        None for a combination that merely does not exist: the default hoster
        is "Megaplay" (env MEDIAFORGE_PROVIDER), but 9anime nowadays serves
        most episodes through its own player (OneAnime) and offers MegaPlay on
        only some -- an exact-match-only lookup therefore came back empty for
        an episode that plays perfectly well. Same behaviour as
        :attr:`provider_url` below, which had this fallback from the start;
        the two disagreeing was the bug.
        """
        lang_label = language or self.selected_language
        lang_key = _LABEL_TO_LANG.get(lang_label, lang_label)
        provider_dict = self.provider_data.get(lang_key) or {}
        if not provider_dict:
            return None
        wanted = provider or self.selected_provider
        link = provider_dict.get(wanted)
        if link:
            return link
        # Case-insensitive second try before giving up on the name entirely:
        # the site's label and our canonical spelling differ only in case for
        # some hosters ("megaplay" vs "Megaplay").
        for name, url in provider_dict.items():
            if name.lower() == str(wanted).lower():
                return url
        # An explicitly requested hoster that is not offered is an answer in
        # itself -- only the DEFAULT falls through to "any available", so a
        # caller asking for one specific hoster still gets None.
        if provider is not None:
            return None
        return next(iter(provider_dict.values()))

    @property
    def provider_url(self):
        """The hoster embed URL for the selected language+provider, falling
        back to any available language / provider -- same "don't dead-end on
        a picky combination" behaviour as MegakinoMovie.provider_url."""
        data = self.provider_data
        lang_key = _LABEL_TO_LANG.get(self.selected_language, self.selected_language)
        hosters = data.get(lang_key) or {}
        if not hosters and data:
            hosters = next(iter(data.values()))
        url = hosters.get(self.selected_provider)
        if not url and hosters:
            url = next(iter(hosters.values()))
        if not url:
            raise ValueError(
                f"Language '{self.selected_language}' with provider "
                f"'{self.selected_provider}' is not available for episode: {self.url}"
            )
        return url

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
