"""URL classification and provider registry.

Maps a stream/series/season URL to the site it belongs to (AniWorld,
SerienStream, FilmPalast, MegaKino, hanime.tv) and to the model classes
that know how to scrape that site. :func:`resolve_provider` is the single
entry point everything else in the app uses to turn a raw URL into a
concrete ``*Episode``/``*Season``/``*Series`` model class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Pattern, Type
from urllib.parse import urlparse, urlunparse

import re as _re

from .config import (
    MEDIAFORGE_EPISODE_PATTERN,
    MEDIAFORGE_SEASON_PATTERN,
    MEDIAFORGE_SERIES_PATTERN,
    HANIME_EPISODE_PATTERN,
    HANIME_SERIES_PATTERN,
    MEGAKINO_EPISODE_PATTERN,
    MEGAKINO_MOVIE_PATTERN,
    MEGAKINO_SERIES_PATTERN,
    SERIENSTREAM_EPISODE_PATTERN,
    SERIENSTREAM_SEASON_PATTERN,
    SERIENSTREAM_SERIES_PATTERN,
    BURNINGSERIES_SERIES_PATTERN,
    BURNINGSERIES_SEASON_PATTERN,
    BURNINGSERIES_EPISODE_PATTERN,
    KINOX_SERIES_PATTERN,
    CINEBY_SERIES_PATTERN,
    CINEBY_EPISODE_PATTERN,
    MANGA_FIRE_SERIES_PATTERN,
    MANGA_FIRE_CHAPTER_PATTERN,
)
from .models import (
    AniworldEpisode,
    AniworldSeason,
    AniworldSeries,
    SerienstreamEpisode,
    SerienstreamSeason,
    SerienstreamSeries,
    BurningSeriesSeries,
    BurningSeriesSeason,
    BurningSeriesEpisode,
    KinoxSeries,
    KinoxSeason,
    KinoxEpisode,
    CinebySeries,
    CinebySeason,
    CinebyEpisode,
    MangaFireToSeries,
    MangaFireToChapter,
)
from .models.filmpalast_to.episode import FilmPalastEpisode
from .models.megakino_to.episode import MegakinoEpisode
from .models.megakino_to.movie import MegakinoMovie
from .models.megakino_to.season import MegakinoSeason
from .models.megakino_to.series import MegakinoSeries
from .models.hanime_tv.episode import HanimeEpisode
from .models.hanime_tv.season import HanimeSeason
from .models.hanime_tv.series import HanimeSeries

# FilmPalast episode URLs: https://filmpalast.to/stream/<slug>
FILMPALAST_EPISODE_PATTERN = _re.compile(
    r"^https?://filmpalast\.to/stream/[a-zA-Z0-9\-]+/?$"
)


@dataclass(frozen=True)
class Provider:
    """A streaming site: the regexes that recognize its URLs and the model
    classes that scrape each URL kind. Any field can be None -- e.g.
    FilmPalast has no series/season concept, only episode_pattern/episode_cls."""

    name: str
    series_pattern: Optional[Pattern[str]] = None
    season_pattern: Optional[Pattern[str]] = None
    episode_pattern: Optional[Pattern[str]] = None

    series_cls: Optional[Type] = None
    season_cls: Optional[Type] = None
    episode_cls: Optional[Type] = None


PROVIDERS = [
    Provider(
        name="AniWorld",
        series_pattern=MEDIAFORGE_SERIES_PATTERN,
        season_pattern=MEDIAFORGE_SEASON_PATTERN,
        episode_pattern=MEDIAFORGE_EPISODE_PATTERN,
        series_cls=AniworldSeries,
        season_cls=AniworldSeason,
        episode_cls=AniworldEpisode,
    ),
    Provider(
        name="SerienStream",
        series_pattern=SERIENSTREAM_SERIES_PATTERN,
        season_pattern=SERIENSTREAM_SEASON_PATTERN,
        episode_pattern=SERIENSTREAM_EPISODE_PATTERN,
        series_cls=SerienstreamSeries,
        season_cls=SerienstreamSeason,
        episode_cls=SerienstreamEpisode,
    ),
    Provider(
        name="BurningSeries",
        series_pattern=BURNINGSERIES_SERIES_PATTERN,
        season_pattern=BURNINGSERIES_SEASON_PATTERN,
        episode_pattern=BURNINGSERIES_EPISODE_PATTERN,
        series_cls=BurningSeriesSeries,
        season_cls=BurningSeriesSeason,
        episode_cls=BurningSeriesEpisode,
    ),
    Provider(
        name="Kinox",
        series_pattern=KINOX_SERIES_PATTERN,
        season_pattern=KINOX_SERIES_PATTERN,
        episode_pattern=KINOX_SERIES_PATTERN,
        series_cls=KinoxSeries,
        season_cls=KinoxSeason,
        episode_cls=KinoxEpisode,
    ),
    Provider(
        name="Cineby",
        series_pattern=CINEBY_SERIES_PATTERN,
        episode_pattern=CINEBY_EPISODE_PATTERN,
        series_cls=CinebySeries,
        season_cls=CinebySeason,
        episode_cls=CinebyEpisode,
    ),
    Provider(
        name="MangaFire",
        series_pattern=MANGA_FIRE_SERIES_PATTERN,
        season_pattern=MANGA_FIRE_SERIES_PATTERN,
        episode_pattern=MANGA_FIRE_CHAPTER_PATTERN,
        series_cls=MangaFireToSeries,
        season_cls=MangaFireToSeries,
        episode_cls=MangaFireToChapter,
    ),
    # FilmPalast: movies only — no series/season structure.
    # The "episode" URL is the movie page itself.
    Provider(
        name="FilmPalast",
        episode_pattern=FILMPALAST_EPISODE_PATTERN,
        episode_cls=FilmPalastEpisode,
    ),
    # MegaKino series episodes: synthetic <watch-post>?episode=N URLs.
    # Movies and series share the plain /watch/<slug>/<id> URL, so it is only
    # routed here for the ?episode form; the plain form falls through to
    # MegakinoFilm below. Series/season handling goes through the dedicated
    # api_* branches (which pick the type from the JSON API), not resolve_provider.
    Provider(
        name="Megakino",
        episode_pattern=MEGAKINO_EPISODE_PATTERN,
        series_cls=MegakinoSeries,
        season_cls=MegakinoSeason,
        episode_cls=MegakinoEpisode,
    ),
    # MegaKino movies (and the plain /watch landing): the page is the "episode".
    Provider(
        name="MegakinoFilm",
        episode_pattern=MEGAKINO_MOVIE_PATTERN,
        episode_cls=MegakinoMovie,
    ),
    # hanime.tv (adult / 18+): a "series" is a franchise; episode URLs are
    # synthetic (<series-slug>?ep=N).  Single season per franchise.
    Provider(
        name="Hanime",
        series_pattern=HANIME_SERIES_PATTERN,
        season_pattern=HANIME_SERIES_PATTERN,
        episode_pattern=HANIME_EPISODE_PATTERN,
        series_cls=HanimeSeries,
        season_cls=HanimeSeason,
        episode_cls=HanimeEpisode,
    ),
]


def normalize_url(url: str) -> str:
    """Normalize a URL to the canonical form the provider regexes expect:
    resolves the /serie/stream/<slug> alias and strips a trailing slash."""
    if not url:
        return url

    url = url.strip()

    parsed = urlparse(url)
    path = parsed.path

    # --- SerienStream alias handling ---
    # Some endpoints use /serie/stream/<slug>; normalize to /serie/<slug>.
    if path.startswith("/serie/stream/"):
        slug = path[len("/serie/stream/") :].strip("/")
        if slug:
            path = f"/serie/{slug}"

    # remove trailing slash
    path = path.rstrip("/")

    return urlunparse(parsed._replace(path=path))


def resolve_provider(url: str) -> Provider:
    """Return the Provider whose series/season/episode pattern matches *url*.

    Raises ValueError if no provider recognizes the URL.
    Used by: ``web/routes/{browse,search,stream}.py``, ``web/queue_worker.py``
    and ``web/autosync_worker.py`` to pick the right scraper model for a URL.
    """
    url = normalize_url(url)

    for provider in PROVIDERS:
        if provider.series_pattern and provider.series_pattern.fullmatch(url):
            return provider
        if provider.season_pattern and provider.season_pattern.fullmatch(url):
            return provider
        if provider.episode_pattern and provider.episode_pattern.fullmatch(url):
            return provider

    raise ValueError(f"Unsupported URL: {url}")
