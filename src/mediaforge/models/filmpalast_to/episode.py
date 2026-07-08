import os
import re
from pathlib import Path

try:
    from ...config import (
        GLOBAL_SESSION,
        NAMING_TEMPLATE,
        logger,
    )
    from ...extractors import provider_functions
    from ..common import check_downloaded
    from ..common.common import (
        download as episode_download,
    )
    from ..common.common import (
        syncplay as episode_syncplay,
    )
    from ..common.common import (
        watch as episode_watch,
    )
except ImportError:
    from mediaforge.config import (
        GLOBAL_SESSION,
        NAMING_TEMPLATE,
        logger,
    )
    from mediaforge.extractors import provider_functions
    from mediaforge.models.common import check_downloaded
    from mediaforge.models.common import (
        download as episode_download,
    )
    from mediaforge.models.common import (
        syncplay as episode_syncplay,
    )
    from mediaforge.models.common import (
        watch as episode_watch,
    )

# NOTE: this local pattern is only used for this class's own URL validation;
# it is intentionally looser than mediaforge.providers.FILMPALAST_EPISODE_PATTERN
# (used for provider-routing). Keep both in sync if the site's URL scheme changes.
FILMPALAST_EPISODE_PATTERN = re.compile(r"^https?://filmpalast\.to/stream/.+")


class FilmPalastEpisode:
    """
    Represents a single FilmPalast movie page.

    FilmPalast is a movie-only site: there is no series/season concept here.
    A single "episode" URL (``/stream/<slug>``) IS the movie -- this class
    doubles as both the episode and the movie metadata holder (title, cast,
    IMDb rating, etc. are all read directly off this page). Callers that need
    to special-case movie-only sites check `prov.name == "FilmPalast"` rather
    than an `is_movie` attribute (see web/routes/search.py).

    Parameters:
        url:                Required. The FilmPalast URL for this episode, e.g.,
                            https://filmpalast.to/stream/scream-7
        selected_path:      Optional. The chosen path; provided in cases such as using a menu.
        selected_language:  Optional. The chosen language; provided in cases such as using a menu.
        selected_provider:  Optional. The chosen provider; provided in cases such as using a menu.

    Attributes (Example):
        url:                    "https://filmpalast.to/stream/scream-7"
        title_de:               "Scream 7"
        user_watched:           4006
        release_year:           2026
        runtime_min:            114
        genres:                 ["Horror", "Mystery"]
        description:            "Als in der beschaulichen Stadt, in der sich Sidney Prescott ein neues Leben aufgebaut hat, ein neuer Ghostface-Killer auftaucht, werden ihre schlimmsten Befürchtungen wahr: Ihre Tochter gerät ins Visier der Ermordung. Entschlossen, ihre Familie zu schützen, muss Sidney sich den Schrecken ihrer Vergangenheit stellen, um dem Blutvergießen ein für alle Mal ein Ende zu setzen."
        image_url:              "/files/movies/240/scream-7.jpg"
        director:               "Kevin Williamson"
        actors:                 ["Neve Campbell", "Courteney Cox", "Isabel May", "Jasmin Savoy Brown", "Mason Gooding", "Roger L. Jackson"]
        imdb_rating:            7.3

        provider_data:          {('German', 'None'): {'VOE': 'https://serienstream.to/r?t=eyJpdiI6IkdVR2hyQjFXOUVLUGRqRGd1Ylo3cUE9PSIsInZhbHVlIjoiRDVIOEdHb2xOcGk3MmsrTVB2Yk9lakZoYS9YVXFsNlJ0SVJwYTNXZTM1bmQvOFpQaFJ4TWdoSHRzUEhzRTZoQVg1Zkx1OFVxZjhpNWYyR3VUd1U0SVE9PSIsIm1hYyI6IjAzOTAxNjA1YTFkMmM0OWI0MDEzNGE3NzQ5YzI0NWZmYTRiZDgxZDRiMDg0ZGYzOGE2M2JiZDQyMjgyZGE4YjMiLCJ0YWciOiIifQ%3D%3D'}, ('English', 'None'): {'VOE': 'https://serienstream.to/r?t=eyJpdiI6IitKSjl2K1EwOGcyZjNHS1VrRW0yQ0E9PSIsInZhbHVlIjoiVDRSQ01RMnpUdFZLblpLb1BGSm1LdE5RQ0U2b2h0cmdDRGRlTi82Q1VPMWJGellGQjhGZE45TldoeE9ESWNxWEhNNDBPQWl0OHM1MjJlaDNRdVY3Z0E9PSIsIm1hYyI6IjUyYjNkZjIwZGMwZWFlZjA1ZTgzNzIzNWI0M2FmZDI3NDcxNmY3OTQ3YTMxNGE0ZjFkNjcyYzFiZWM0MWE2YWUiLCJ0YWciOiIifQ%3D%3D'}}

        redirect_url:           https://serienstream.to/r?t=eyJpdiI6IlNvVkFWOURJTklBT05wTiszQkF5VVE9PSIsInZhbHVlIjoiUCtoM3JETHQxbUZVMThkY1RuT2p6TTd1aXdnYW9LNzNOb2t3QU5DV2RzUGxJWFA0WUxBaUpZd0Y4dGhJazhrRjFzT1dWVTlISFRpWTE5N0t2dWFtUEE9PSIsIm1hYyI6IjhiMzIxOTljMThlN2ZiYmZlNjJmMjYxYmE5YjhhMjY1NjI0YzM4NThhYzUzMTg3YzdiZjg5Y2U0ZmRhYjU5YmYiLCJ0YWciOiIifQ%3D%3D
        provider_url:           https://voe.sx/e/2gevxuvhffzd
        stream_url:             https://cdn-ybrlgbugcqvfwfxm.edgeon-bandwidth.com/engine/hls2-c/01/12274/32xqoasasgio_,n,.urlset/master.m3u8?t=AyZvb2TAsfUJASynb8yS1VVvV9VLR4L6iELp5QnC5NY&s=1769786403&e=14400&f=69846104&node=5oVG/75jdb0Y5X40bYOVrWQ5Z8VHd1xf5E7nxyia+5E=&i=185.213&sp=2500&asn=39351&q=n&rq=pSN9X93FqA34kNMYDUcS0wTzZ2nLYuaQH60wgnXd

        selected_path:          "Downloads"
        selected_language:      "German Dub"
        selected_provider:      "VOE"

        self._base_folder:      Downloads/American Horror Story (2011) [imdbid-tt1844624]
        self._folder_path:      Downloads/American Horror Story (2011) [imdbid-tt1844624]/Season 1
        self._file_name:        American Horror Story S1E1
        self._file_extension:   mkv
        self._episode_path:     Downloads/American Horror Story (2011) [imdbid-tt1844624]/Season 1/American Horror Story S1E1.mkv

        is_downloaded:          {'exists': False, 'video_langs': set(), 'audio_langs': set()}

        _html:                  "<!doctype html>[...]"

    Methods:
        download()
        watch()
        syncplay()

    Used by:
        mediaforge.providers (Provider(name="FilmPalast", episode_cls=FilmPalastEpisode))
        and web/routes/search.py, which imports this class directly (there is no
        FilmPalastSeries/FilmPalastSeason -- see module docstring above).
    """

    def __init__(
        self,
        url: str,
        selected_path: str = None,
        selected_language: str = None,
        selected_provider: str = None,
    ):
        if not self.__is_valid_filmpalast_episode_url(url):
            raise ValueError(f"Invalid FilmPalast episode URL: {url}")

        self.url = url
        self.__title_de = None
        self.__user_watched = None
        self.__release_year = None
        self.__runtime_min = None
        self.__genres = None
        self.__description = None
        self.__image_url = None
        self.__director = None
        self.__actors = None
        self.__imdb_rating = None

        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_provider_param = selected_provider

        self.__provider_data = None

        self.__selected_path = None
        self.__selected_language = None
        self.__selected_provider = None

        self.__redirect_url = None
        self.__provider_url = None

        # https://jellyfin.org/docs/general/server/media/shows/#organization
        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None

        self.__is_downloaded = None

        self.__html = None

    # -----------------------------
    # STATIC METHODS
    # -----------------------------

    @staticmethod
    def __is_valid_filmpalast_episode_url(url):
        return bool(FILMPALAST_EPISODE_PATTERN.match(url))

    # -----------------------------
    # PUBLIC PROPERTIES (lazy load)
    # -----------------------------

    @property
    def title_de(self):
        if self.__title_de is None:
            self.__extract_title_de()
        return self.__title_de

    @property
    def user_watched(self):
        if self.__user_watched is None:
            self.__extract_user_watched()
        return self.__user_watched

    @property
    def release_year(self):
        if self.__release_year is None:
            self.__extract_release_year()
        return self.__release_year

    @property
    def runtime_min(self):
        if self.__runtime_min is None:
            self.__extract_runtime_min()
        return self.__runtime_min

    @property
    def genres(self):
        if self.__genres is None:
            self.__extract_genres()
        return self.__genres

    @property
    def description(self):
        if self.__description is None:
            self.__extract_description()
        return self.__description

    @property
    def image_url(self):
        if self.__image_url is None:
            self.__extract_image_url()
        return self.__image_url

    @property
    def director(self):
        if self.__director is None:
            self.__extract_director()
        return self.__director

    @property
    def actors(self):
        if self.__actors is None:
            self.__extract_actors()
        return self.__actors

    @property
    def imdb_rating(self):
        if self.__imdb_rating is None:
            self.__extract_imdb_rating()
        return self.__imdb_rating

    @property
    def provider_data(self):
        if self.__provider_data is None:
            self.__provider_data = self.__extract_provider_data()
        return self.__provider_data

    @property
    def selected_path(self):
        if self.__selected_path is None:
            raw_path = self.__selected_path_param or os.getenv(
                "MEDIAFORGE_DOWNLOAD_PATH", str(Path.home() / "Downloads")
            )

            path = Path(raw_path).expanduser()

            if not path.is_absolute():
                path = Path.home() / path

            self.__selected_path = str(path)
        return self.__selected_path

    @selected_path.setter
    def selected_path(self, value):
        self.__selected_path_param = value
        self.__selected_path = None
        self.__base_folder = None
        self.__folder_path = None
        self.__episode_path = None

    @property
    def selected_language(self):
        if self.__selected_language is None:
            self.__selected_language = self.__selected_language_param or os.getenv(
                "MEDIAFORGE_LANGUAGE", "German"
            )
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None
        self.__redirect_url = None
        self.__provider_url = None
        self.__is_downloaded = None
        self.__base_folder = None
        self.__folder_path = None
        self.__episode_path = None
        self.__file_name = None

    @property
    def selected_provider(self):
        if self.__selected_provider is None:
            raw = self.__selected_provider_param or os.getenv("MEDIAFORGE_PROVIDER", "VOE")
            # Normalize: strip " HD"/" HQ" suffixes so extractor function names work
            self.__selected_provider = raw.replace(" HD", "").replace(" HQ", "").strip()
        return self.__selected_provider

    @property
    def redirect_url(self):
        if self.__redirect_url is None:
            link = self.provider_link(self.selected_language, self.selected_provider)
            if link is None:
                raise ValueError(
                    f"Language '{self.selected_language}' with provider "
                    f"'{self.selected_provider}' is not available for "
                    f"episode: {self.url}"
                )
            self.__redirect_url = link
        return self.__redirect_url

    @property
    def provider_url(self):
        if self.__provider_url is None:
            try:
                from ...config import resolve_redirect_url
            except ImportError:
                from mediaforge.config import resolve_redirect_url
            self.__provider_url = resolve_redirect_url(self.redirect_url)
        return self.__provider_url

    @property
    def stream_url(self):
        try:
            stream_url = provider_functions[
                f"get_direct_link_from_{self.selected_provider.lower()}"
            ](self.provider_url)
        except KeyError:
            raise ValueError(
                f"The provider '{self.selected_provider}' is not yet implemented."
            )

        return stream_url

    @property
    def title_cleaned(self):
        """Title with filesystem-unsafe characters removed, for use in file names."""
        t = self.title_de or ""
        t = re.sub(r'[<>:"/\\|?*]', "", t)
        t = t.strip()
        return t or "Film"

    @property
    def available_providers(self):
        """Normalized provider names available for this movie (e.g. ['VOE', 'Veev'])."""
        result = []
        for p in self.provider_data:
            name = p["name"].replace(" HD", "").replace(" HQ", "").strip()
            if name not in result:
                result.append(name)
        return result

    # Path properties — for movies we use a flat structure:
    # <download_path>/<Title> (Year).<ext>
    # No season/episode subfolder since this is a standalone film.

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
                or os.getenv("FILMPALAST_MOVIE_SUBFOLDER", "0") == "1"
                or os.getenv("MEGAKINO_MOVIE_SUBFOLDER", "0") == "1"
            )
            if use_subfolder:
                # Jellyfin local-metadata layout: <path>/<Title (Year)>/<Title (Year)>.mkv
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
            self.__episode_path = (
                self._folder_path / f"{self._file_name}.{self._file_extension}"
            )
        return self.__episode_path

    # END

    @property
    def is_downloaded(self):
        if self.__is_downloaded is None:
            self.__is_downloaded = check_downloaded(self._episode_path)
        return self.__is_downloaded

    @property
    def _html(self):
        if self.__html is None:
            if not self.url:
                raise ValueError("Episode URL is missing for HTML fetch.")
            logger.debug(f"fetching ({self.url})...")
            # Use plain requests with gzip-only Accept-Encoding.
            # GLOBAL_SESSION (niquests) negotiates Brotli which it then fails
            # to decompress automatically, so we bypass it here.
            import requests as _req
            resp = _req.get(
                self.url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=20,
            )
            resp.raise_for_status()
            self.__html = resp.text
        return self.__html

    # -----------------------------
    # PRIVATE EXTRACTION FUNCTIONS
    # -----------------------------
    # Each of these does a single regex search/findall against the raw movie
    # page HTML and caches the result on the matching private attribute; the
    # public @property wrappers above call them lazily on first access.

    def __extract_title_de(self):
        """Extract the German title from the <em itemprop="name"> tag."""
        match = re.search(r'<em itemprop="name">(.*?)</em>', self._html)
        if match:
            self.__title_de = match.group(1).strip()

    def __extract_user_watched(self):
        match = re.search(r"<strong>(\d+)</strong> Nutzer", self._html)
        if match:
            self.__user_watched = int(match.group(1))

    def __extract_release_year(self):
        match = re.search(r"Ver&ouml;ffentlicht: (\d+)", self._html)
        if match:
            self.__release_year = int(match.group(1))

    def __extract_runtime_min(self):
        match = re.search(r"Spielzeit: <em>(.*?)</em>", self._html)
        if match:
            self.__runtime_min = int(re.sub(r"[^0-9]", "", match.group(1)))

    def __extract_genres(self):
        self.__genres = re.findall(
            r'href="https://filmpalast.to/search/genre/.*?">(.*?)</a>', self._html
        )

    def __extract_description(self):
        match = re.search(
            r'<span itemprop="description">(.*?)</span>', self._html, re.DOTALL
        )
        if match:
            self.__description = match.group(1).strip()

    def __extract_image_url(self):
        match = re.search(r'itemprop="image" src="(.*?)"', self._html)
        if match:
            self.__image_url = match.group(1)

    def __extract_director(self):
        match = re.search(r'href="/search/director/.*?">(.*?)</a>', self._html)
        if match:
            self.__director = match.group(1).strip()

    def __extract_actors(self):
        actors = re.findall(r'href="/search/title/.*?>(.*?)</a>', self._html)
        self.__actors = [a.strip() for a in actors if a.strip()]

    def __extract_imdb_rating(self):
        match = re.search(r"Imdb:\s*([\d.]+)/10", self._html)
        if match:
            self.__imdb_rating = float(match.group(1))

    def __extract_provider_data(self):
        providers = []
        blocks = re.findall(
            r'<ul class="currentStreamLinks">(.*?)</ul>', self._html, re.DOTALL
        )

        for block in blocks:
            # provider name
            name = re.search(r'<p class="hostName">(.*?)</p>', block)
            provider = name.group(1).strip() if name else None

            # redirect url
            url = re.search(r'<a [^>]*?(?:data-player-url|href)="([^"]+)"', block)
            redirect = url.group(1).strip() if url else None

            if provider and redirect:
                providers.append({"name": provider, "url": redirect})

        return providers

    def provider_link(self, language, provider):
        """Return the redirect URL for the given provider.

        FilmPalast has no per-language stream separation — the language
        parameter is accepted for API compatibility but ignored.
        Provider names on the page include suffixes like " HD" or " HQ";
        these are stripped before comparison.
        """
        norm_target = provider.upper().replace(" HD", "").replace(" HQ", "").strip()
        for p in self.provider_data:
            p_norm = p["name"].upper().replace(" HD", "").replace(" HQ", "").strip()
            if p_norm == norm_target:
                return p["url"]
        # Fallback: return the first available provider URL
        if self.provider_data:
            return self.provider_data[0]["url"]
        return None

    # -----------------------------
    # PUBLIC METHODS
    # -----------------------------

    def download(self, cancel_event=None, **kwargs):
        """Download this FilmPalast episode.

        VeeV requires a dedicated curl_cffi + Playwright path because its CDN
        validates the browser TLS fingerprint.  All other providers go through
        the standard common download pipeline.
        """
        if self.selected_provider.upper().replace(" HD", "").replace(" HQ", "").strip() == "VEEV":
            try:
                from ...extractors.provider.veev import download_from_veev
            except ImportError:
                from mediaforge.extractors.provider.veev import download_from_veev

            ep_label = os.path.splitext(self._file_name)[0] if self._file_name else ""
            os.makedirs(self._folder_path, exist_ok=True)
            download_from_veev(
                self.provider_url,
                self._episode_path,
                cancel_event=cancel_event,
                label=ep_label,
            )
        else:
            episode_download(self, cancel_event=cancel_event, **kwargs)

    watch = episode_watch
    syncplay = episode_syncplay


if __name__ == "__main__":
    episode = FilmPalastEpisode("https://filmpalast.to/stream/scream-7")
    print(episode.url)
    print(episode.title_de)
    print(episode.user_watched)
    print(episode.release_year)
    print(episode.runtime_min)
    print(episode.genres)
    print(episode.description)
    print(episode.image_url)
    print(episode.director)
    print(episode.actors)
    print(episode.imdb_rating)
    print(episode.provider_data)
