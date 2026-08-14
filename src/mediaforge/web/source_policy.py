"""Whether a content source counts as "on" -- the single answer.

The rule itself is one sentence: *every source is opt-out, except an adult
source, which is opt-in.* It was previously spelled out in three places that
each hardcoded the literal ``"hanime"`` with its own comparison
(``routes/browse.py`` used ``!= "0"``, ``routes/uptime.py`` used ``== "1"``,
``uptime_monitor._uptime_config()`` had a third copy for the tracking toggle).
Three copies of a rule that names one specific source id is exactly the shape
that drifts the moment a second adult source appears -- and it already had:
the UpTime page defaulted hanime's *tracking* toggle off by hardcoding the id
rather than by asking what kind of source it is.

So the id lives in :data:`ADULT_SOURCE_IDS` and everything else asks
:func:`source_enabled_default` / :func:`source_enabled`. A third-party module
source defaults to on -- it was installed on purpose -- and may name its own
settings key (see ``uptime_monitor.register_monitor_site`` and
``search.register_search_source``'s ``enabled_key``).

This module also owns :func:`search_sources`, the one list of "which content
sources exist right now", built-ins *and* module-registered ones. Before it
existed, that list was hardcoded five times in the frontend (the search
fan-out, the source chips, the Sources settings tab, the UpTime tracking
rows), which is why a module that registered a provider plus a search source
was reachable by URL but never actually asked by the search box.

Used by: routes/search.py (GET /api/search/sources + the fan-out contract),
routes/settings.py (Sources tab state), routes/browse.py (home feed),
routes/uptime.py (the enabled_source badge), uptime_monitor.py (per-source
tracking defaults).
"""

from .db import get_setting
from ..logger import get_logger

logger = get_logger(__name__)


# Source ids that are 18+ and therefore opt-in rather than opt-out. Keeping
# this a set (not an ``== "hanime"``) is the whole point: a second adult
# source is one entry here and nothing else changes.
ADULT_SOURCE_IDS = frozenset({"hanime"})

# Source ids that default to off for a reason OTHER than being an adult
# source (English-only catalogue inconsistent with the DE-first providers --
# see the NineAnime/Aniwaves Provider() comments in providers.py and the
# ANIWAVES_BASE_URL/NINEANIME_BASE_URL docstrings in config.py). Opt-in like
# an adult source (source_enabled_default() below folds both sets together),
# but deliberately kept out of ADULT_SOURCE_IDS: is_adult_source() drives the
# frontend age-confirmation gate and the age-limited/kids-mode filtering, and
# neither should trigger just because a source happens to also be opt-in.
OPT_IN_SOURCE_IDS = frozenset({"nineanime", "aniwaves"})

# Source ids whose catalogue is English-only. Purely a LABEL: the UI shows an
# "EN" marker on the chip/toggle so a user understands why a source that looks
# like every other anime site returns no German audio, and it explains why
# these sources are off by default. Deliberately a separate set from
# OPT_IN_SOURCE_IDS above even though both currently hold the same two ids --
# "defaults to off" and "has no German catalogue" are different statements,
# and a future source could be one without being the other.
ENGLISH_ONLY_SOURCE_IDS = frozenset({"nineanime", "aniwaves"})

# Values that mean "off" in app_settings. Everything the UI writes is "1"/"0",
# but a hand-edited DB row or a module's own key may carry a word, and an
# empty string must not read as enabled.
_FALSEY = frozenset({"0", "", "false", "off", "no", "none", "null"})


def is_adult_source(source_id) -> bool:
    """True if *source_id* is an 18+ source (and therefore opt-in)."""
    return str(source_id or "").lower() in ADULT_SOURCE_IDS


def is_opt_in_source(source_id) -> bool:
    """True if *source_id* defaults to off WITHOUT being an adult source --
    see OPT_IN_SOURCE_IDS. Does not imply is_adult_source()."""
    return str(source_id or "").lower() in OPT_IN_SOURCE_IDS


def is_english_only_source(source_id) -> bool:
    """True if *source_id* only offers English audio/subtitles --
    see ENGLISH_ONLY_SOURCE_IDS. Display-only; affects no gating."""
    return str(source_id or "").lower() in ENGLISH_ONLY_SOURCE_IDS


def source_enabled_key(source_id) -> str:
    """The app_settings key holding the on/off state of a built-in source."""
    return "source_enabled_" + str(source_id or "")


def source_enabled_default(source_id) -> str:
    """The default value of that key: "0" for an adult or opt-in source,
    "1" otherwise."""
    return "0" if (is_adult_source(source_id) or is_opt_in_source(source_id)) else "1"


def setting_is_on(value, default="0") -> bool:
    """Interpret an app_settings value as a boolean, tolerating word forms."""
    if value is None:
        value = default
    return str(value).strip().lower() not in _FALSEY


def source_enabled(source_id, key=None, default=None) -> bool:
    """Is *source_id* switched on?

    *key* overrides the ``source_enabled_<id>`` convention (a third-party
    source may reuse the key it already has, e.g. ``kinox_search_enabled``);
    *default* overrides what an unset key means. Both are what
    ``register_monitor_site()`` passes through for module sources.
    """
    _key = key or source_enabled_key(source_id)
    _def = default if default is not None else source_enabled_default(source_id)
    return setting_is_on(get_setting(_key, _def), _def)


# ---------------------------------------------------------------------------
# The source catalogue
# ---------------------------------------------------------------------------
# The shipped sources, in their default order. This is the *only* place the
# built-in ids and their display labels are written down -- the frontend gets
# them from GET /api/search/sources instead of repeating the list.
BUILTIN_SEARCH_SOURCES = (
    {"id": "aniworld",   "label": "AniWorld"},
    {"id": "sto",        "label": "SerienStream"},
    {"id": "filmpalast", "label": "FilmPalast"},
    {"id": "megakino",   "label": "MegaKino"},
    {"id": "filmo",      "label": "filmo.to"},
    {"id": "nineanime",  "label": "9anime"},
    {"id": "aniwaves",   "label": "Aniwaves"},
    {"id": "hanime",     "label": "hanime 18+"},
)

BUILTIN_SEARCH_SOURCE_IDS = tuple(_s["id"] for _s in BUILTIN_SEARCH_SOURCES)

# What each built-in source actually publishes. Written down here because two
# places in the WebUI need it and both used to carry their own copy: the Seerr
# page (a movie request must not be looked up on a series-only site) and the
# home feed's Movies row. static/seerr.js had three hardcoded "movie sources"
# and six hardcoded "series sources", so a source added later -- filmo.to, or
# anything a module registers -- was simply never asked.
#
# Kept next to BUILTIN_SEARCH_SOURCES rather than in routes/browse.py, which
# holds the other copy (_feed_builtin_entries): that module imports the whole
# scraping stack, and this one is deliberately cheap enough for the request
# path. Data moves here, imports do not.
#
# "adult" is a media type of its own, matching mediaforge/home_feed.py's
# FEED_TYPES -- the 18+ gate has to be able to drop a source without also
# deciding whether it is a film or a series.
BUILTIN_SOURCE_MEDIA_TYPES = {
    "aniworld":   ("series",),
    "sto":        ("series",),
    "filmpalast": ("movies",),
    # The one source that does both, which is why it is also the only one whose
    # results have to be split by the item's own is_series flag.
    "megakino":   ("movies", "series"),
    "filmo":      ("movies",),
    "nineanime":  ("series",),
    "aniwaves":   ("series",),
    "hanime":     ("adult", "series"),
}

# What a source that never declared anything is assumed to publish. Both, on
# purpose: a module source left out of a lookup is a module that silently does
# not work, while one asked about a media type it does not have simply answers
# with nothing.
DEFAULT_SOURCE_MEDIA_TYPES = ("movies", "series")


def source_media_types(source_id, declared=None) -> tuple:
    """Which media types *source_id* publishes.

    *declared* is what a module passed to ``register_search_source`` (or None).
    A built-in is looked up in :data:`BUILTIN_SOURCE_MEDIA_TYPES`; anything
    else falls back to :data:`DEFAULT_SOURCE_MEDIA_TYPES`.
    """
    if declared:
        clean = tuple(t for t in declared if t in ("movies", "series", "adult"))
        if clean:
            return clean
    return BUILTIN_SOURCE_MEDIA_TYPES.get(source_id, DEFAULT_SOURCE_MEDIA_TYPES)


def builtin_source_label(source_id) -> str:
    """Display label of a built-in source, or the id itself if unknown."""
    for _s in BUILTIN_SEARCH_SOURCES:
        if _s["id"] == source_id:
            return _s["label"]
    return str(source_id or "")


def search_sources(include_adult: bool = True) -> list:
    """Every content source the search can currently ask, built-ins first.

    Returns a list of plain dicts, one per source::

        {"id", "label", "adult", "thirdparty", "enabled", "css_class"}

    - ``enabled`` is the *effective* on/off state (opt-out, except an adult
      source, except a module source's own ``enabled_key``) -- resolved here
      so no caller has to re-implement the ``"0"`` vs ``"1"`` vs empty-string
      reading of :func:`setting_is_on`.
    - ``css_class`` is the per-source header/chip class the WebUI already
      ships for built-ins (``browse-provider-<id>``); module sources share one
      neutral ``browse-provider-thirdparty`` class, because the app cannot
      know a colour for a source it has never seen.
    - ``include_adult=False`` drops 18+ sources entirely rather than marking
      them off. Used for an age-limited session: "this source does not exist
      for you" is the correct answer there, and it also keeps the search from
      firing a request that ``api_search()`` would only answer with 403.

    A module source appears here as soon as it called
    ``search.register_search_source()`` and disappears the moment the module
    is disabled or uninstalled (``thirdparties/registry.unregister_module()``
    calls ``unregister_search_source()``), so no restart is involved either
    way.
    """
    out = []
    for _s in BUILTIN_SEARCH_SOURCES:
        adult = is_adult_source(_s["id"])
        if adult and not include_adult:
            continue
        out.append({
            "id": _s["id"],
            "label": _s["label"],
            "adult": adult,
            "opt_in": is_opt_in_source(_s["id"]),
            "english_only": is_english_only_source(_s["id"]),
            "thirdparty": False,
            "enabled_key": source_enabled_key(_s["id"]),
            "enabled": source_enabled(_s["id"]),
            "css_class": "browse-provider-" + _s["id"],
            # So a caller can ask only the sources that could possibly have the
            # thing it is looking for -- see source_media_types().
            "media_types": list(source_media_types(_s["id"])),
        })

    # Imported lazily: mediaforge.search pulls in the scraping stack, and this
    # module is imported by request-path code that must stay cheap.
    try:
        from ..search import thirdparty_search_sources
        extra = thirdparty_search_sources()
    except Exception:
        # A broken module registry must never take the search down -- the
        # built-ins above are still a complete, usable answer.
        logger.warning("[Sources] Could not list third-party search sources",
                       exc_info=True)
        extra = []

    seen_ids = {_s["id"] for _s in BUILTIN_SEARCH_SOURCES}
    for entry in extra:
        site_id = entry.get("site_id")
        adult = bool(entry.get("adult"))
        if not site_id or (adult and not include_adult):
            continue
        seen_ids.add(site_id)
        out.append({
            "id": site_id,
            "label": entry.get("label") or site_id,
            "adult": adult,
            "opt_in": False,
            # A module knows its own catalogue; the core cannot guess it.
            "english_only": False,
            "thirdparty": True,
            # A module source is opt-out like any built-in, but may point at a
            # settings key it already owns instead of source_enabled_<id>.
            # Reported so the Sources tab writes back to the *same* key it
            # read -- otherwise a module with a custom key would show a switch
            # that flips visually and changes nothing.
            "enabled_key": entry.get("enabled_key") or source_enabled_key(site_id),
            "enabled": source_enabled(site_id, key=entry.get("enabled_key"),
                                      default="1"),
            "css_class": "browse-provider-thirdparty",
            "media_types": list(source_media_types(
                site_id, entry.get("media_types"))),
        })

    # A module that only called providers.register_provider() (URL resolution)
    # without also calling search.register_search_source() (keyword search)
    # used to be entirely invisible here -- not defaulted-off, just absent, so
    # it could never get a toggle/order row on the Sources tab or a chip on
    # the home-v2 feed even though nothing stops it from being selected.
    # Surface it the same way an unconfigured module source is: opt-out,
    # enabled by default, generic styling.
    try:
        from ..providers import thirdparty_provider_ids, _EXTRA_PROVIDERS
        for item_id in thirdparty_provider_ids():
            if item_id in seen_ids:
                continue
            provider = _EXTRA_PROVIDERS.get(item_id)
            if provider is None:
                continue
            out.append({
                "id": item_id,
                "label": provider.name or item_id,
                "adult": False,
                "opt_in": False,
                "english_only": False,
                "thirdparty": True,
                "enabled_key": source_enabled_key(item_id),
                "enabled": source_enabled(item_id, default="1"),
                "css_class": "browse-provider-thirdparty",
                "media_types": list(source_media_types(item_id)),
            })
    except Exception:
        logger.warning("[Sources] Could not list third-party providers",
                       exc_info=True)

    return out


def search_source_ids(include_adult: bool = True) -> list:
    """Just the ids from :func:`search_sources`, in the same order."""
    return [s["id"] for s in search_sources(include_adult=include_adult)]
