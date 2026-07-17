"""Top-level re-export for the two "classic" Series/Season/Episode model
families: AniWorld (aniworld.to) and Serienstream (s.to).

FilmPalast (filmpalast_to), MegaKino (megakino_to) and hanime (hanime_tv)
are intentionally NOT re-exported here -- they are imported directly from
their own subpackages (see mediaforge.providers and web/routes/search.py)
because each has a shape that doesn't fit the plain Series/Season/Episode
trio (FilmPalast has no series concept at all; MegaKino splits movies and
series episodes into separate classes; hanime has a single-season franchise
model). See models/common/common.py for the download()/watch()/syncplay()
implementations shared across all site families.
"""
from .aniworld_to import (
    AniworldEpisode,
    AniworldSeason,
    AniworldSeries,
)
from .burningseries import (
    BurningSeriesEpisode,
    BurningSeriesSeason,
    BurningSeriesSeries,
)
from .cineby import CinebyEpisode, CinebySeason, CinebySeries
from .kinox import KinoxEpisode, KinoxSeason, KinoxSeries
from .mangafire_to import MangaFireToChapter, MangaFireToPage, MangaFireToSeries
from .s_to import SerienstreamEpisode, SerienstreamSeason, SerienstreamSeries

__all__ = [
    "AniworldSeries",
    "AniworldSeason",
    "AniworldEpisode",
    "BurningSeriesSeries",
    "BurningSeriesSeason",
    "BurningSeriesEpisode",
    "CinebySeries",
    "CinebySeason",
    "CinebyEpisode",
    "KinoxSeries",
    "KinoxSeason",
    "KinoxEpisode",
    "MangaFireToSeries",
    "MangaFireToChapter",
    "MangaFireToPage",
    "SerienstreamSeries",
    "SerienstreamSeason",
    "SerienstreamEpisode",
]
