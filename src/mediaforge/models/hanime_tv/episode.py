"""hanime.tv episode.

The URL is synthetic: ``<series-slug>?ep=<n>`` (n = 1-based index into the
franchise's ordered video list).  The actual per-episode video slug is
resolved lazily from the franchise detail, then the HLS stream is pulled via
the hanime extractor.  There is no hoster and no language variants, so the
download path is a self-contained yt-dlp HLS grab (best quality).
"""
import os
import re
from pathlib import Path

try:
    from ...config import HANIME_EPISODE_PATTERN, NAMING_TEMPLATE, logger
    from ..common import check_downloaded
    from ..common.common import syncplay as episode_syncplay
    from ..common.common import watch as episode_watch
    from . import scraper
except ImportError:  # pragma: no cover
    from mediaforge.config import HANIME_EPISODE_PATTERN, NAMING_TEMPLATE, logger
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common.common import syncplay as episode_syncplay
    from mediaforge.models.common.common import watch as episode_watch
    from mediaforge.models.hanime_tv import scraper

# hanime is Japanese audio with burned-in subtitles -> one logical "language".
HANIME_LANGUAGE = "Japanese Dub"


class HanimeEpisode:
    """One video within a hanime franchise (see HanimeSeason).

    No hoster/provider selection (hanime has a single HLS source per video)
    and no `is_movie`/`skip_times` (hanime has no movie concept and no
    AniSkip integration). `selected_provider` is hard-coded to "hanime" only
    for API-shape compatibility with the other episode classes.

    Used by: mediaforge.providers (Provider(episode_cls=HanimeEpisode)) and
    web/routes/search.py (via HanimeSeries -> HanimeSeason.episodes).
    """

    def __init__(self, url=None, series=None, season=None, episode_number=None,
                 episode_slug=None, title_de=None, title_en=None, censored="",
                 selected_path=None, selected_language=None, selected_provider=None):
        if not HANIME_EPISODE_PATTERN.match(url or ""):
            raise ValueError(f"Invalid hanime episode URL: {url}")
        self.url = url
        self._series_url = url.split("?")[0]
        self._series = series
        self._season = season
        self._episode_slug = episode_slug
        self.censored = censored
        self.__episode_number = episode_number
        self.__title_de = title_de
        self.__title_en = title_en
        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_path = None
        self.__selected_language = None
        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None
        self.__is_downloaded = None
        self.__stream_url = None
        self.__stream_headers = {}

    # --- relations ---
    @property
    def series(self):
        if self._series is None:
            from .series import HanimeSeries
            self._series = HanimeSeries(url=self._series_url)
        return self._series

    @property
    def season(self):
        if self._season is None:
            from .season import HanimeSeason
            self._season = HanimeSeason(url=self._series_url, series=self._series)
        return self._season

    @property
    def episode_number(self):
        if self.__episode_number is None:
            m = re.search(r"[?&]ep=(\d+)", self.url)
            self.__episode_number = int(m.group(1)) if m else 1
        return self.__episode_number

    @property
    def episode_slug(self):
        """Resolve the concrete hanime video slug for this episode index."""
        if self._episode_slug is None:
            detail = scraper.video_detail(scraper.slug_from_url(self._series_url)) or {}
            eps = scraper.franchise_episodes(detail)
            idx = self.episode_number - 1
            if 0 <= idx < len(eps):
                self._episode_slug = eps[idx].get("slug")
            elif eps:
                self._episode_slug = eps[0].get("slug")
        return self._episode_slug

    @property
    def title_de(self):
        if self.__title_de is None:
            self.__title_de = f"Episode {self.episode_number}"
        return self.__title_de

    @property
    def title_en(self):
        return self.__title_en or self.title_de

    # --- selection (kept for API compatibility; hanime has one stream) ---
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
            self.__selected_language = self.__selected_language_param or HANIME_LANGUAGE
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None
        self.__base_folder = self.__folder_path = self.__episode_path = self.__file_name = None

    @property
    def selected_provider(self):
        return "hanime"

    # --- stream resolution (via extractor) ---
    def _resolve_stream(self):
        """Resolve the signed HLS URL + its session headers via the hanime
        extractor, which drives a headless browser to click play and capture
        the request (see hanime_tv/browser.py). The headers stay with the URL:
        the CDN rejects the signed link without the session's cookies."""
        if self.__stream_url is None:
            try:
                from ...extractors.provider.hanime import get_stream_info_from_hanime
            except ImportError:
                from mediaforge.extractors.provider.hanime import get_stream_info_from_hanime
            info = get_stream_info_from_hanime(scraper.series_url(self.episode_slug))
            if not info or not info.get("url"):
                raise ValueError(f"No HLS stream found for hanime episode: {self.url}")
            self.__stream_url = info["url"]
            self.__stream_headers = info.get("headers") or {}
        return self.__stream_url, self.__stream_headers

    @property
    def stream_url(self):
        """The signed HLS (.m3u8) URL. See _resolve_stream."""
        return self._resolve_stream()[0]

    @property
    def stream_headers(self):
        """Headers that have to accompany every request for stream_url."""
        self._resolve_stream()
        return self.__stream_headers

    # --- paths (NAMING_TEMPLATE, season/episode layout) ---
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
            if len(parts) <= 1:
                self.__base_folder = Path(self.selected_path)
            else:
                self.__base_folder = Path(self.selected_path) / self._fmt(parts[0])
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            parts = os.getenv("MEDIAFORGE_NAMING_TEMPLATE", NAMING_TEMPLATE).split("/")
            if len(parts) <= 2:
                self.__folder_path = self._base_folder
            else:
                self.__folder_path = self._base_folder / self._fmt(parts[1])
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

    # --- actions ---
    def download(self, cancel_event=None, **kwargs):
        """Download the single HLS stream for this video. Unlike the other
        site families, this does NOT go through models/common/common.py's
        download() -- there is no per-language/provider track selection to
        reconcile, so a dedicated single-stream downloader is used instead."""
        try:
            from ...extractors.provider.hanime import download_from_hanime
        except ImportError:
            from mediaforge.extractors.provider.hanime import download_from_hanime
        ep_label = os.path.splitext(self._file_name)[0] if self._file_name else ""
        os.makedirs(self._folder_path, exist_ok=True)
        stream_url, stream_headers = self._resolve_stream()
        try:
            return download_from_hanime(
                stream_url, self._episode_path,
                cancel_event=cancel_event, label=ep_label,
                headers=stream_headers,
            )
        except Exception:
            # The signed URL is short-lived and session-bound. Forget it so a
            # retry resolves a fresh one instead of replaying the dead link.
            self.__stream_url = None
            self.__stream_headers = {}
            scraper.forget_video(self.episode_slug)
            raise

    watch = episode_watch
    syncplay = episode_syncplay
