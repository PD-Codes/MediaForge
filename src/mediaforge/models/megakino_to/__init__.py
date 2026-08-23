"""MegaKino model package.

Content lives at ``/<category>/<id>-<slug>.html``. The category segment
varies per title (``/films/``, ``/serials/``, but also ``/crime/``,
``/action/``, ...) and carries no meaning here -- only the numeric post id is
stable. See config.py's MEGAKINO_* patterns.

Movies and series episodes share that URL family and are told apart purely by
a query param: ``?episode=N`` means a series episode (MegakinoEpisode); the
bare URL means a movie (MegakinoMovie). These are two independent classes --
MegakinoEpisode is NOT a subclass of MegakinoMovie -- so callers distinguish
them with e.g. ``isinstance(ep, MegakinoMovie)`` (see web/routes/search.py)
rather than an `is_movie` attribute. One post equals one season (no separate
MegakinoSeries with multiple seasons); see series.py/season.py.

The site moved from a React SPA with a JSON API to DataLife Engine; the model
classes were unaffected because scraper.py kept its public shape. See its
module docstring for the site structure and the cookie gate.
"""
from .episode import MegakinoEpisode
from .movie import MegakinoMovie
from .season import MegakinoSeason
from .series import MegakinoSeries

__all__ = ["MegakinoSeries", "MegakinoSeason", "MegakinoEpisode", "MegakinoMovie"]
