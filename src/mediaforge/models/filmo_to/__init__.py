"""Filmo (filmo.to) model package -- movies only, no series/season concept.

Not re-exported from models/__init__.py, same as filmpalast_to/megakino_to/
hanime_tv: imported directly from mediaforge.providers (see that module's
docstring for why).
"""
from .movie import FilmoMovie

__all__ = ["FilmoMovie"]
