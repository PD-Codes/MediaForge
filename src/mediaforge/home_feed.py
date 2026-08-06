"""Home feed source registry (the "new home page", Settings -> General).

The classic home page renders one block per built-in source, with every row
hardcoded in the template. The new home page groups rows by *question*
("New this week", "Popular right now", "Movies") and mixes every enabled
source into them -- which only works if the set of sources is something the
app can *ask for* at runtime instead of something a template spells out.

This module is that list. It holds nothing but registrations: the built-in
sources are declared in ``web/routes/browse.py`` (they need the browse cache
and the scrapers, both of which live on the web side), and third-party
modules add theirs through :func:`register_home_feed_source` -- the same
``item_id`` convention every other secondary registry uses, so
``web/thirdparties/registry.py``'s ``unregister_module()`` cleans them up
when a module is disabled or uninstalled.

Deliberately free of any ``mediaforge.web`` import: this is core, and the
web layer imports it, never the other way round.
"""

from __future__ import annotations

from .logger import get_logger

logger = get_logger(__name__)

# Row ids the feed knows. "movies" is not in here on purpose -- it is derived
# from an entry's media_type, not registered separately, so a module cannot
# put a series into the movie row by accident.
FEED_ROWS = ("new", "popular")

# Values accepted for media_type. "adult" is its own type (rather than a flag
# on "series") because the 18+ chip has to be able to switch a source off
# without touching the source filter.
FEED_TYPES = ("series", "movies", "adult")

# Source ids the built-in feed already owns.
RESERVED_SOURCE_IDS = frozenset({"aniworld", "sto", "filmpalast", "megakino", "hanime"})

# item_id -> entry dict
_EXTRA_HOME_FEED_SOURCES: dict = {}


def register_home_feed_source(item_id, source_id, label, fetchers,
                              media_type="series", color=None):
    """Add a source to the new home page's discovery rows.

    - ``item_id``: the id the module already passed to ``register_thirdparty()``.
      Registrations are keyed by it so ``unregister_module()`` can drop them
      automatically -- a source registered under any other id keeps showing up
      after the module is gone.
    - ``source_id``: the id used in the chip row and in the API payload. Must
      not collide with a built-in (:data:`RESERVED_SOURCE_IDS`) or with
      another registered source. Conventionally the same id the module passed
      to ``register_provider()`` / ``register_search_source()``, so a card's
      click-through resolves through that provider.
    - ``label``: what the chip says. Shown as-is, so it should already be in a
      form that works in every UI language (a brand name usually is).
    - ``fetchers``: ``{"new": fn}`` and/or ``{"popular": fn}``. Each ``fn()``
      takes no arguments and returns a list of
      ``{"title", "url", "poster_url", "genre"}`` dicts -- the same card shape
      every built-in browse list returns. ``url`` should be resolvable by
      :func:`mediaforge.providers.resolve_provider`. Returning ``None``
      signals "upstream failed", which makes the row report the source as
      unavailable instead of silently empty; returning ``[]`` means "nothing
      new", which is a different thing.
    - ``media_type``: ``"series"``, ``"movies"`` or ``"adult"``. Decides which
      type chip filters the cards, and whether they also feed the Movies row.
      An ``"adult"`` source is only ever fetched when the user turned the 18+
      chip on.
    - ``color``: optional CSS color for the chip dot (e.g. ``"#7c5cff"``).
      Anything that is not a plain ``#rgb``/``#rrggbb`` literal is dropped --
      the value ends up in a style attribute.

    Results are cached by the caller (``/api/home-feed``) exactly like the
    built-in lists, so ``fn`` is called at most once an hour per row and may
    do real network work.
    """
    if not isinstance(fetchers, dict) or not fetchers:
        raise ValueError("register_home_feed_source: fetchers must be a non-empty dict")
    unknown = set(fetchers) - set(FEED_ROWS)
    if unknown:
        raise ValueError("register_home_feed_source: unknown row(s): %s" % sorted(unknown))
    for row, fn in fetchers.items():
        if not callable(fn):
            raise ValueError("register_home_feed_source: fetcher for %r is not callable" % row)
    if media_type not in FEED_TYPES:
        raise ValueError("register_home_feed_source: media_type must be one of %s" % (FEED_TYPES,))
    if source_id in RESERVED_SOURCE_IDS:
        raise ValueError("register_home_feed_source: %r is a built-in source id" % source_id)
    for existing_id, entry in _EXTRA_HOME_FEED_SOURCES.items():
        if entry["source_id"] == source_id and existing_id != item_id:
            raise ValueError("register_home_feed_source: source id already registered: %r" % source_id)

    _EXTRA_HOME_FEED_SOURCES[item_id] = {
        "source_id": source_id,
        "label": str(label or source_id),
        "fetchers": dict(fetchers),
        "media_type": media_type,
        "color": _safe_color(color),
    }
    logger.info("[HomeFeed] Registered third-party feed source: %s (%s, rows=%s)",
                source_id, item_id, ",".join(sorted(fetchers)))


def unregister_home_feed_source(item_id) -> None:
    """Drop a source previously added via :func:`register_home_feed_source`."""
    removed = _EXTRA_HOME_FEED_SOURCES.pop(item_id, None)
    if removed:
        logger.info("[HomeFeed] Unregistered third-party feed source: %s (%s)",
                    removed["source_id"], item_id)


def thirdparty_home_feed_source_ids() -> set:
    """item_ids that currently own a home feed source. Read-only counterpart of
    :func:`unregister_home_feed_source`, used by the Modulmanager's capability
    list so it can report what a module added without reaching into this
    module's private dict."""
    return set(_EXTRA_HOME_FEED_SOURCES)


def iter_home_feed_sources() -> list:
    """Every registered third-party source, as a list of copies.

    Copies, because the caller merges these with the built-ins and tags them
    with per-request state (enabled, reachable) -- mutating the registry from
    inside a request would leak that state into the next one.

    Sources belonging to a switched-off module are left out -- see
    module_gate.py for why the enabled check lives at the point of use.
    """
    from .module_gate import filter_enabled

    return [
        {
            "source_id": entry["source_id"],
            "label": entry["label"],
            "fetchers": dict(entry["fetchers"]),
            "media_type": entry["media_type"],
            "color": entry["color"],
            "item_id": item_id,
        }
        for item_id, entry in filter_enabled(_EXTRA_HOME_FEED_SOURCES).items()
    ]


def _safe_color(value) -> str:
    """Allow only a literal hex colour through -- the value is rendered into a
    style attribute, and a module is not a trusted source of CSS."""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) not in (4, 7) or not text.startswith("#"):
        return ""
    if any(c not in "0123456789abcdefABCDEF" for c in text[1:]):
        return ""
    return text
