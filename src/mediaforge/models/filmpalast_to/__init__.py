"""FilmPalast (filmpalast.to) movie model package.

Movies only -- there is no Series/Season layer here, so the package holds a
single episode.py with FilmPalastEpisode (which represents one film).

Existed as a namespace package by accident until now: every other model
package under models/ carries an __init__.py, this one did not, which made it
the odd one out for packaging tools and for anything walking the package tree.
"""
