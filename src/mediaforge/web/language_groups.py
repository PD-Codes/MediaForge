"""Language fallback groups — ordered language chains used in place of one language.

A fallback group is a named, ordered list of language labels, e.g.
"German Dub" → "English Dub" → "English Sub". Everywhere a single language can
be picked (the download modal, an auto-sync job, the download/sync defaults) a
group can be picked instead: MediaForge then takes, per episode, the first
language of the chain the episode is actually offered in. That is the whole
point of the feature — a series that is German-dubbed up to season 2 and
subtitled after that no longer needs two jobs.

Groups are referenced as the string ``"group:<id>"``. No column had to change
for that: ``download_queue.language`` and ``autosync_jobs.language`` keep
holding a plain string. Nothing downstream may ever see that reference though —
"group:3" is not a legal folder name on Windows and no episode model knows the
label — so the two workers resolve it to a real language label before it
reaches ``lang_folder_for()`` or an episode's ``selected_language``.
"""

import json
import os

from ..logger import get_logger

logger = get_logger(__name__)

GROUP_PREFIX = "group:"

# (Audio.value, Subtitles.value) -> language label.
#
# AniWorld (config.py) and s.to (models/s_to/episode.py) define their own
# Audio/Subtitles enum classes with identical *values* but distinct types, so a
# pair of raw strings is the only key that works for both — the same reason
# autosync_worker compares `(k[0].value, k[1].value)` instead of the enums.
LANG_PAIR_TO_LABEL = {
    ("German", "None"): "German Dub",
    ("Japanese", "English"): "English Sub",
    ("Japanese", "German"): "German Sub",
    ("English", "None"): "English Dub",
    # s.to only: English audio with German subtitles.
    ("English", "German"): "English Dub (German Sub)",
}

# Languages a group may be built from. Deliberately the same set the language
# dropdowns offer; "Japanese Dub" (hanime) has no alternative to fall back to
# and "All Languages" is not a language but its own download mode.
SELECTABLE_LANGUAGES = [
    "German Dub",
    "English Sub",
    "German Sub",
    "English Dub",
    "English Dub (German Sub)",
]


def lang_separation_enabled():
    """True when downloads are sorted into per-language folders.

    Fallback groups require this and are refused without it. The reason is not
    cosmetic: every question a group has to answer — "do I already have this
    episode in one of the chain's languages?", "in which one?", "is a better
    one available now?" — is answered by looking at which language folder a file
    sits in. Dumped into one shared folder, a file's language is simply not
    recoverable (the scan matches S01E05 in the file name, nothing else), so the
    sync would either re-download endlessly or skip forever.
    """
    return os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"


def is_group_ref(value):
    """True if `value` is a "group:<id>" reference rather than a language label."""
    return isinstance(value, str) and value.startswith(GROUP_PREFIX)


def group_ref(group_id):
    """The stored reference string for a group id."""
    return f"{GROUP_PREFIX}{group_id}"


def group_id_for(value):
    """The numeric id behind a "group:<id>" reference, or None."""
    if not is_group_ref(value):
        return None
    try:
        return int(value[len(GROUP_PREFIX):])
    except (TypeError, ValueError):
        return None


def get_group(value):
    """Look up the group behind a reference (or a bare id). None if unknown."""
    from .db import get_language_group

    group_id = value if isinstance(value, int) else group_id_for(value)
    if group_id is None:
        return None
    try:
        return get_language_group(group_id)
    except Exception as exc:  # pragma: no cover - DB errors are logged, not fatal
        logger.warning("[LangGroup] Could not read language group %s: %s", group_id, exc)
        return None


def resolve_chain(value, respect_disabled=True):
    """Ordered list of language labels behind `value`.

    A plain label resolves to a one-element chain, so callers can iterate
    uniformly and only need `is_group_ref()` where the two genuinely differ.
    An unknown group (deleted while a job still referenced it) resolves to an
    empty list — the caller decides whether that is an error or a skip.

    `respect_disabled` drops "English Sub" when the global
    MEDIAFORGE_DISABLE_ENGLISH_SUB switch is on, so a group cannot smuggle a
    language back in that the user turned off everywhere else.
    """
    if not value:
        return []
    if not is_group_ref(value):
        return [value]
    group = get_group(value)
    if not group:
        return []
    chain = [l for l in group.get("languages") or [] if l]
    if respect_disabled and os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1":
        chain = [l for l in chain if l != "English Sub"]
    return chain


def language_display(value):
    """Human-readable label for a stored language value.

    Group references become the group's name so queue rows, sync cards and the
    history don't show users the internal "group:3".
    """
    if not is_group_ref(value):
        return value or ""
    group = get_group(value)
    if not group:
        return value
    return group["name"]


def labels_from_provider_data(pd_data):
    """Language labels an episode's provider_data offers.

    Accepts the raw dict keyed by (Audio, Subtitles) enum pairs, as produced by
    every episode model. Returns an empty set when the data is missing or has a
    shape this mapping doesn't cover (hanime's single burned-in track, movie
    providers) — callers treat that as "can't tell" and fall back.
    """
    labels = set()
    if not pd_data:
        return labels
    try:
        for key in pd_data:
            label = LANG_PAIR_TO_LABEL.get(
                (getattr(key[0], "value", key[0]), getattr(key[1], "value", key[1]))
            )
            if label:
                labels.add(label)
    except Exception as exc:
        logger.debug("[LangGroup] Unreadable provider data: %s", exc)
        return set()
    return labels


def pick_language(chain, available):
    """First language of `chain` that is in `available`, else None."""
    for lang in chain:
        if lang in available:
            return lang
    return None


def group_languages_json(languages):
    """Normalise a user-supplied language list for storage.

    Keeps the given order, drops unknown labels and duplicates — the order *is*
    the fallback priority, so it must survive exactly as entered.
    """
    seen = []
    for lang in languages or []:
        lang = str(lang).strip()
        if lang in SELECTABLE_LANGUAGES and lang not in seen:
            seen.append(lang)
    return json.dumps(seen)
