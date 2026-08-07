"""aniwaves.ru model package -- English-only anime, series only.

Not re-exported from models/__init__.py, same as filmpalast_to/megakino_to/
hanime_tv/filmo_to/nineanime_to: imported directly from mediaforge.providers.
"""
from .episode import AniwavesEpisode
from .season import AniwavesSeason
from .series import AniwavesSeries

__all__ = ["AniwavesSeries", "AniwavesSeason", "AniwavesEpisode"]
