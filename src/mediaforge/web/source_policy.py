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
source never lands here at all -- it was installed on purpose, so it defaults
to on and may name its own settings key (see
``uptime_monitor.register_monitor_site``).

Used by: routes/browse.py (home feed), routes/uptime.py (the enabled_source
badge), uptime_monitor.py (per-source tracking defaults).
"""

from .db import get_setting


# Source ids that are 18+ and therefore opt-in rather than opt-out. Keeping
# this a set (not an ``== "hanime"``) is the whole point: a second adult
# source is one entry here and nothing else changes.
ADULT_SOURCE_IDS = frozenset({"hanime"})

# Values that mean "off" in app_settings. Everything the UI writes is "1"/"0",
# but a hand-edited DB row or a module's own key may carry a word, and an
# empty string must not read as enabled.
_FALSEY = frozenset({"0", "", "false", "off", "no", "none", "null"})


def is_adult_source(source_id) -> bool:
    """True if *source_id* is an 18+ source (and therefore opt-in)."""
    return str(source_id or "").lower() in ADULT_SOURCE_IDS


def source_enabled_key(source_id) -> str:
    """The app_settings key holding the on/off state of a built-in source."""
    return "source_enabled_" + str(source_id or "")


def source_enabled_default(source_id) -> str:
    """The default value of that key: "0" for an adult source, "1" otherwise."""
    return "0" if is_adult_source(source_id) else "1"


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
