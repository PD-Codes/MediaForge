"""URL classification and provider registry.

Maps a stream/series/season URL to the site it belongs to (AniWorld,
SerienStream, FilmPalast, MegaKino, hanime.tv) and to the model classes
that know how to scrape that site. :func:`resolve_provider` is the single
entry point everything else in the app uses to turn a raw URL into a
concrete ``*Episode``/``*Season``/``*Series`` model class.

Third-party modules can add another content source (a new streaming site)
without touching this file -- see :func:`register_provider` below and
".examples/thirdparties/README.md", section "Content sources
(register_provider / register_search_source)".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Pattern, Type
from urllib.parse import urlparse, urlunparse

import re as _re

from .logger import get_logger

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
)
from .models import (
    AniworldEpisode,
    AniworldSeason,
    AniworldSeries,
    SerienstreamEpisode,
    SerienstreamSeason,
    SerienstreamSeries,
)
from .models.filmpalast_to.episode import FilmPalastEpisode
from .models.megakino_to.episode import MegakinoEpisode
from .models.megakino_to.movie import MegakinoMovie
from .models.megakino_to.season import MegakinoSeason
from .models.megakino_to.series import MegakinoSeries
from .models.hanime_tv.episode import HanimeEpisode
from .models.hanime_tv.season import HanimeSeason
from .models.hanime_tv.series import HanimeSeries

logger = get_logger(__name__)

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


# ---------------------------------------------------------------------------
# Third-party content sources
# ---------------------------------------------------------------------------
# A separate, append-only registry -- PROVIDERS above stays exactly the fixed
# built-in list it always was (nothing in this file needed to change to add
# this). resolve_provider() below checks both, built-ins first, so a
# third-party provider can never accidentally shadow a shipped one; the name
# uniqueness check in register_provider() enforces the same the other way
# around.
_EXTRA_PROVIDERS: dict[str, "Provider"] = {}  # item_id -> Provider


def register_provider(item_id: str, provider: "Provider") -> None:
    """Register an additional content source (streaming site) from a
    third-party module's ``register(app)``.

    ``item_id`` should be the same id the module already passed to
    ``register_thirdparty()`` for this source -- see
    ``web/thirdparties/registry.py``'s ``unregister_module()``, which calls
    :func:`unregister_provider` for every ``item_id`` a module owns when it is
    disabled/uninstalled, so a stale provider never outlives its module.

    ``provider`` is a plain :class:`Provider` -- the same dataclass the
    built-in sites use: URL regexes plus the model classes (or thin adapter
    classes) that implement series/season/episode scraping for that site.
    Nothing about the model classes is special-cased for third parties; they
    only need to expose whatever interface the rest of the app already calls
    on ``series_cls``/``season_cls``/``episode_cls`` for a built-in provider
    (see any ``models/<site>/`` package for the shape).

    Raises ``ValueError`` if the name collides with a built-in or another
    registered provider -- provider names double as a stable key elsewhere
    (search site ids, settings, logs), so silently shadowing one would be
    worse than failing loudly at registration time.
    """
    if not isinstance(provider, Provider):
        raise TypeError("register_provider expects a Provider instance")
    existing = {p.name for p in PROVIDERS} | {p.name for p in _EXTRA_PROVIDERS.values()}
    if provider.name in existing:
        raise ValueError(f"register_provider: name already registered: {provider.name!r}")
    _EXTRA_PROVIDERS[item_id] = provider
    logger.info("[Providers] Registered third-party content source: %s (%s)", provider.name, item_id)


def unregister_provider(item_id: str) -> None:
    """Drop a third-party provider previously added via :func:`register_provider`.
    A no-op if *item_id* never registered one. Never touches PROVIDERS."""
    removed = _EXTRA_PROVIDERS.pop(item_id, None)
    if removed:
        logger.info("[Providers] Unregistered third-party content source: %s (%s)", removed.name, item_id)


def all_providers() -> list["Provider"]:
    """Every provider resolve_provider() considers, built-ins first."""
    return list(PROVIDERS) + list(_EXTRA_PROVIDERS.values())


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

    Checks every built-in provider first (PROVIDERS, in declaration order),
    then any third-party ones added via :func:`register_provider`, in
    registration order -- a built-in site can never be shadowed by a
    third-party one.

    Raises ValueError if no provider recognizes the URL.
    Used by: ``web/routes/{browse,search,stream}.py``, ``web/queue_worker.py``
    and ``web/autosync_worker.py`` to pick the right scraper model for a URL.
    """
    url = normalize_url(url)

    for provider in all_providers():
        if provider.series_pattern and provider.series_pattern.fullmatch(url):
            return provider
        if provider.season_pattern and provider.season_pattern.fullmatch(url):
            return provider
        if provider.episode_pattern and provider.episode_pattern.fullmatch(url):
            return provider

    raise ValueError(f"Unsupported URL: {url}")
