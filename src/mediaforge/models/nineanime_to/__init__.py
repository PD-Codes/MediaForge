"""9anime (9anime.or.at) model package -- English-only anime, series only.

Not re-exported from models/__init__.py, same as filmpalast_to/megakino_to/
hanime_tv/filmo_to: imported directly from mediaforge.providers.
"""
from .episode import NineAnimeEpisode
from .season import NineAnimeSeason
from .series import NineAnimeSeries

__all__ = ["NineAnimeSeries", "NineAnimeSeason", "NineAnimeEpisode"]
