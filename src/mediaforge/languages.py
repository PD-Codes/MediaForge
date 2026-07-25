"""Single source of truth for language labels and their mappings.

Every site model describes an episode's audio/subtitle combination with its
own ``(Audio, Subtitles)`` enum pair. Those enum *classes* are deliberately
distinct types per site (``config.Audio`` for AniWorld, ``models.s_to.episode.
Audio`` for s.to), so the only key both sides can share is a pair of raw
strings -- ``(audio.value, subtitles.value)``.

That pair -> label mapping used to be duplicated in four places
(``web/language_groups.py``, ``web/lang_folders.py``, the s.to branch of
``web/routes/search.py::api_providers`` and ``models/s_to/episode.py::
_normalize_language``). They drifted: the route's copy was missing
``("English", "German")``, so serienstream.to's "Ger-Sub" hosters were dropped
before the availability probe and the provider dropdown looked empty for
"English Dub (German Sub)". Adding a language now means editing this module
only.

This module must stay dependency-free (plain strings, no enum imports) so both
``mediaforge.models.*`` and ``mediaforge.web.*`` can import it without creating
an import cycle.
"""

# -----------------------------
# Canonical labels
# -----------------------------
# The exact strings stored in download_queue.language / autosync_jobs.language
# and shown in every language dropdown. Do not change them without a DB
# migration -- existing rows hold these values verbatim.
GERMAN_DUB = "German Dub"
ENGLISH_SUB = "English Sub"
GERMAN_SUB = "German Sub"
ENGLISH_DUB = "English Dub"
ENGLISH_DUB_GERMAN_SUB = "English Dub (German Sub)"
JAPANESE_DUB = "Japanese Dub"


# -----------------------------
# (Audio value, Subtitles value) -> label
# -----------------------------
LANG_PAIR_TO_LABEL = {
    ("German", "None"): GERMAN_DUB,
    ("Japanese", "English"): ENGLISH_SUB,
    ("Japanese", "German"): GERMAN_SUB,
    ("English", "None"): ENGLISH_DUB,
    # s.to only: English audio with German subtitles ("Ger-Sub" on the site).
    ("English", "German"): ENGLISH_DUB_GERMAN_SUB,
    # hanime: Japanese audio with burned-in subtitles -- one logical language.
    ("Japanese", "None"): JAPANESE_DUB,
}

# label -> (Audio value, Subtitles value). First pair wins, so the mapping
# stays unambiguous even if a label is ever listed twice above.
LABEL_TO_LANG_PAIR = {}
for _pair, _label in LANG_PAIR_TO_LABEL.items():
    LABEL_TO_LANG_PAIR.setdefault(_label, _pair)
del _pair, _label


# -----------------------------
# Label aliases
# -----------------------------
# Everything a caller might hand us for a language: the canonical label, the
# short form used by MEDIAFORGE_LANGUAGE / the CLI, and the German site labels.
# Keys are compared case-insensitively via normalize_label().
LABEL_ALIASES = {
    "german dub": GERMAN_DUB,
    "german": GERMAN_DUB,
    "deutsch": GERMAN_DUB,
    "english dub": ENGLISH_DUB,
    "english": ENGLISH_DUB,
    "englisch": ENGLISH_DUB,
    "english sub": ENGLISH_SUB,
    "german sub": GERMAN_SUB,
    "deutscher untertitel": GERMAN_SUB,
    "english dub (german sub)": ENGLISH_DUB_GERMAN_SUB,
    "deutsche untertitel": ENGLISH_DUB_GERMAN_SUB,
    "ger-sub": ENGLISH_DUB_GERMAN_SUB,
    "ger sub": ENGLISH_DUB_GERMAN_SUB,
    "japanese dub": JAPANESE_DUB,
    "japanese": JAPANESE_DUB,
}


# -----------------------------
# Label -> on-disk folder (MEDIAFORGE_LANG_SEPARATION)
# -----------------------------
LANG_FOLDER_MAP = {
    GERMAN_DUB: "german-dub",
    ENGLISH_SUB: "english-sub",
    GERMAN_SUB: "german-sub",
    ENGLISH_DUB: "english-dub",
    ENGLISH_DUB_GERMAN_SUB: "english-dub-german-sub",
    JAPANESE_DUB: "japanese-dub",
}

# Every folder a downloaded-detection scan must consider.
LANG_FOLDERS = list(LANG_FOLDER_MAP.values())


# -----------------------------
# Selectable language sets
# -----------------------------
# Languages a dropdown offers and a fallback group may be built from.
# Deliberately without JAPANESE_DUB: it only exists on hanime (which has no
# other track), so offering it would mean picking a language every regular
# series never has.
SELECTABLE_LANGUAGES = [
    GERMAN_DUB,
    ENGLISH_SUB,
    GERMAN_SUB,
    ENGLISH_DUB,
    ENGLISH_DUB_GERMAN_SUB,
]

# Languages an auto-sync job with "All Languages" should try to fetch.
SYNC_ALL_LANGUAGES = list(SELECTABLE_LANGUAGES)


# -----------------------------
# Helpers
# -----------------------------
def pair_of(key):
    """Normalise a ``(Audio, Subtitles)`` key to a pair of raw strings.

    Accepts enum members from any site model as well as plain strings, so
    callers never have to care which module's enum class produced the key.
    """
    try:
        audio, subtitles = key
    except (TypeError, ValueError):
        return None
    return (getattr(audio, "value", audio), getattr(subtitles, "value", subtitles))


def label_for_pair(key):
    """Canonical label for a ``(Audio, Subtitles)`` key, or None if unknown.

    Unknown is a legitimate answer (a provider with a burned-in track, a site
    label we do not model); callers treat it as "can't tell" and skip the entry
    rather than guessing.
    """
    pair = pair_of(key)
    if pair is None:
        return None
    return LANG_PAIR_TO_LABEL.get(pair)


def labels_for_provider_data(pd_data):
    """Set of labels an episode's ``provider_data`` offers.

    Accepts the raw dict keyed by ``(Audio, Subtitles)`` pairs as produced by
    every episode model. Returns an empty set for missing data or a shape this
    mapping does not cover.
    """
    labels = set()
    if not pd_data:
        return labels
    for key in pd_data:
        label = label_for_pair(key)
        if label:
            labels.add(label)
    return labels


def normalize_label(language):
    """Canonical label for any accepted spelling, or None if unknown."""
    if not isinstance(language, str):
        return None
    return LABEL_ALIASES.get(language.strip().lower())


def lang_folder_for(language):
    """Folder name for a language label, with a slugified fallback."""
    return LANG_FOLDER_MAP.get(
        language, (language or "").lower().replace(" ", "-")
    )
