"""Deciding which files on disk are the same book.

This is the part of the eBook support that has to be right, because getting it
wrong is visible immediately: too eager and two different books collapse into
one card, too timid and the same novel shows up five times.

Five times is not hypothetical. Calibre stores one *record per format*, so a
book kept as EPUB, MOBI, AZW3 and PDF is four folders, each with its own
``metadata.opf`` and ``cover.jpg``; re-adding a file makes a fifth. On top of
that the same book often also sits loose in the library root, downloaded before
it was ever imported. All of those must end up as one entry with several
formats.

Everything here is pure: strings in, strings out, no filesystem, no database.
That is deliberate -- it is the piece that most needs tests, and tests of a pure
function need no fixtures beyond the awkward real-world names themselves.
"""
from __future__ import annotations

import re
import unicodedata


# Amazon sticks its ASIN onto a downloaded filename: "Some Title_B01BLF4IR8".
_ASIN_SUFFIX_RE = re.compile(r"_B0[0-9A-Z]{8}$", re.IGNORECASE)

# Calibre appends its record id to the folder name: "Some Title (1234)".
_CALIBRE_ID_SUFFIX_RE = re.compile(r"\s*\(\d{1,6}\)\s*$")

# Release-group and scanner markers, mostly on manga rips:
# "Yunas Geisterhaus 01 (GER)(KAZE)(FG-Manga) - KCC.mobi"
_BRACKET_GROUP_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]{0,40}[\)\]\}]")

# " - KCC", " - Unbekannt", " - Unknown": a trailing pseudo-author that says
# nothing. Stripped so it cannot become the author and cannot split a group.
_EMPTY_AUTHORS = frozenset(
    {"kcc", "unbekannt", "unknown", "n a", "na", "anonym", "anonymous", "various"}
)

# Folder names that are collections, not people. Without this list
# "IT Stuff/Some Book.mobi" would be filed under the author "IT Stuff" and
# would then refuse to merge with the same book sitting somewhere else.
_NON_AUTHOR_FOLDERS = frozenset(
    {
        "books",
        "book",
        "buecher",
        "bucher",
        "calibre",
        "comics",
        "downloads",
        "ebooks",
        "it stuff",
        "manga",
        "mangas",
        "music",
        "musik",
        "new",
        "novels",
        "pdf",
        "romane",
        "temp",
        "tmp",
        "unbekannt",
        "unknown",
        "unsortiert",
    }
)

# A folder that looks like a download dump rather than an author.
_NON_AUTHOR_FOLDER_HINTS = ("filecrypt", "http", "www.", ".cc", ".com", ".to", ".org")

# Two normalised titles are merged across a prefix relation only from this
# length on. Calibre truncates the filename part of its own layout at around
# 40 characters ("Warcraft 02 - Der Lord der Clan" for "... der Clans"), which
# is what makes the rule necessary; a short prefix like "die" would merge
# everything with everything, which is what makes the floor necessary.
_PREFIX_MERGE_MIN_LEN = 18

# "World of Warcraft 08 - Weltenbeben" -> ("World of Warcraft", 8)
# "Yunas Geisterhaus 01"              -> ("Yunas Geisterhaus", 1)
#
# The number must NOT be part of a decimal: "Zukunft der Arbeit in Industrie
# 4.0" was read as series "... Industrie 4", volume 0. A negative lookahead for
# a following separator+digit is what keeps a version number from becoming a
# volume number.
_SERIES_IN_TITLE_RE = re.compile(
    r"^(?P<series>.+?)[\s._-]+(?P<index>\d{1,3})(?![.,]\d)"
    r"(?:\s*[-–:]\s*(?P<rest>.+))?$"
)


# "%f6" for "ö": a filename that has been through a web download keeps the
# percent-encoding of whatever produced it, so "In der H%f6lle der Sioux.epub"
# went onto the shelf spelled exactly like that. Decoded here rather than in
# the scanner for the same reason the comic side does it here -- two spellings
# of one title have to normalise to one, or the two files never merge into one
# card. Same implementation, deliberately: comics/identity.py:_decode_percent.
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")

# A four-digit year in brackets, at the end: "Der Report (2019)". Recovered
# rather than thrown away -- _BRACKET_GROUP_RE deletes every bracketed group,
# which is right for "(GER)(KAZE)" and wrong for the one piece of information
# in there that is worth keeping. Also matches a publisher that shares the
# brackets: "(Heyne 2019)".
_TITLE_YEAR_RE = re.compile(r"[\(\[]\s*(?:[^()\[\]]{0,40}?[\s\-])?((?:19|20)\d{2})\s*[\)\]]\s*$")


def decode_percent(value: str) -> str:
    """Undo percent-escapes in a filename. Unchanged when that is not possible.

    latin-1 first: these names come from Windows tooling, where the escaped
    byte is cp1252/latin-1 far more often than UTF-8. A wrong guess would turn
    one mojibake into another, so anything that does not decode cleanly is
    left exactly as it was.
    """
    if "%" not in (value or ""):
        return value or ""
    try:
        return _PERCENT_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    except (ValueError, OverflowError):
        return value


def extract_year(raw: str):
    """``(text without the trailing bracketed year, year)`` -- year may be None.

    Only a TRAILING bracketed group is considered. A year in the middle of a
    title is part of the title ("1984", "Sommer 1944"), and guessing otherwise
    costs more than the year is worth.
    """
    text = raw or ""
    match = _TITLE_YEAR_RE.search(text)
    if not match:
        return text, None
    year = int(match.group(1))
    return text[:match.start()].rstrip(" -_.") , year


def strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Fold a title or author down to its comparable core.

    Lowercase, without diacritics, without release-group brackets, Calibre ids
    or ASIN suffixes, and with every run of non-alphanumerics collapsed to one
    space. "Vol'Jin: Schatten der Horde (2)" and "voljin schatten der horde"
    end up identical.
    """
    value = strip_diacritics(decode_percent(text or ""))
    # "Fire & Blood" and "Fire and Blood" are one book. Taken from
    # comics/identity.py, which has needed it for the same reason.
    value = value.replace("&", " and ")
    value = _ASIN_SUFFIX_RE.sub("", value)
    value = _CALIBRE_ID_SUFFIX_RE.sub("", value)
    value = _BRACKET_GROUP_RE.sub(" ", value)
    value = value.lower()
    # German umlaut transliteration happens after diacritic stripping, so
    # "Bücher" is already "bucher" here; map the ligature separately.
    value = value.replace("ß", "ss")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_title(raw: str) -> str:
    """Human-facing title: keep the original casing, drop only the noise."""
    value = _ASIN_SUFFIX_RE.sub("", decode_percent(raw or ""))
    value = _CALIBRE_ID_SUFFIX_RE.sub("", value)
    value = _BRACKET_GROUP_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_.")
    return value


def looks_like_author_folder(name: str) -> bool:
    """Is this folder name plausibly a person rather than a collection?"""
    if not name:
        return False
    lowered = name.lower()
    if normalize(name) in _NON_AUTHOR_FOLDERS:
        return False
    if any(hint in lowered for hint in _NON_AUTHOR_FOLDER_HINTS):
        return False
    # "Some Title (1234)" is a Calibre *book* folder, never an author.
    if _CALIBRE_ID_SUFFIX_RE.search(name):
        return False
    # A name that is mostly digits is a volume folder, not a person.
    letters = sum(ch.isalpha() for ch in name)
    return letters >= 3


def split_filename(stem: str) -> tuple[str, str]:
    """Split "Title - Author" into its two halves.

    Only the *last* " - " is treated as the separator, because titles contain
    dashes far more often than author lists do ("Warcraft 02 - Der Lord der
    Clans - Christie Golden"). A trailing part that is a known non-author
    ("- KCC", "- Unbekannt") is dropped instead of becoming the author.
    """
    cleaned = _ASIN_SUFFIX_RE.sub("", (stem or "").strip())
    title, author = cleaned, ""
    if " - " in cleaned:
        head, tail = cleaned.rsplit(" - ", 1)
        if head.strip():
            title, author = head, tail
    if normalize(author) in _EMPTY_AUTHORS:
        # A placeholder author ("- Unbekannt", "- KCC"): drop the fragment
        # entirely, it is noise in the title as much as in the author.
        author = ""
    elif not _plausible_author(author):
        # Not an author at all but a subtitle -- give it back to the title.
        title, author = cleaned, ""
    return clean_title(title), author.strip()


def _plausible_author(candidate: str) -> bool:
    """Reject a trailing fragment that is clearly still part of the title.

    These libraries are full of names like
    ``Die Shannara-Chroniken - Elfensteine_ Roman_B01BLF4IR8``, where the part
    after the dash is a subtitle, not a person. Two markers are decisive: an
    underscore (what a colon becomes when a title is written to a filesystem)
    and digits. Neither occurs in a real author name, both occur constantly in
    subtitles.
    """
    if not candidate or not candidate.strip():
        return False
    if "_" in candidate or ":" in candidate:
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    if not any(ch.isalpha() for ch in candidate):
        return False
    # "Peter Rising" is two words, "Mike Chapple & David Seidl" is five. A
    # seven-word tail is a sentence.
    return len(candidate.split()) <= 6


def split_series(title: str) -> tuple[str, float | None]:
    """Guess series name and volume number from a title.

    Used only when no OPF or Calibre database says otherwise. Returns
    ("", None) when the title carries no volume number, which is the common
    case for standalone books.
    """
    match = _SERIES_IN_TITLE_RE.match((title or "").strip())
    if not match:
        return "", None
    series = clean_title(match.group("series"))
    try:
        index = float(match.group("index"))
    except (TypeError, ValueError):
        return "", None
    # A single leading word plus a number is too thin to call a series
    # ("Band 2"), and a four-digit number is a year, not a volume.
    if len(series) < 3 or index > 999:
        return "", None
    # Volume 0 does not exist. Where it appears it is the tail of something
    # else -- a version ("Industrie 4.0"), a chapter marker, a stray digit --
    # and a "#0" badge on the shelf is a visible symptom of a wrong guess.
    if index < 1:
        return "", None
    # A series name that ends in a digit is usually a number that got split in
    # the wrong place ("Der Hobbit 1 2" or a title carrying its own version).
    if series[-1].isdigit():
        return "", None
    return series, index


def group_key(title: str, author: str) -> str:
    """The exact-match key. Author-less entries get their own namespace so
    they can be attached later (see :func:`merge_groups`) rather than being
    force-merged with a same-titled book by a different author."""
    norm_title = normalize(title)
    norm_author = normalize(author)
    if not norm_title:
        return ""
    if norm_author:
        return f"{norm_title}|{norm_author}"
    return f"{norm_title}|"


def _first_author(authors) -> str:
    if not authors:
        return ""
    if isinstance(authors, str):
        authors = [authors]
    for entry in authors:
        if entry and str(entry).strip():
            return str(entry).strip()
    return ""


def merge_groups(candidates: list[dict]) -> list[list[dict]]:
    """Group candidate files into books.

    ``candidates`` are dicts carrying at least ``title`` and ``authors``; every
    other key is passed through untouched. Returns a list of buckets, each
    bucket being the files of one book.

    Three passes, each strictly more permissive than the last, so the risky
    ones only ever see what the safe ones could not place:

    1. exact ``normalize(title)|normalize(author)``
    2. entries without an author join a title-identical group -- but only when
       there is exactly one such group, so an author-less "Dune" does not have
       to guess between two different Dunes
    3. same author, and one normalised title is a prefix of the other -- this
       is what reunites Calibre's truncated filenames with the full titles
       from its ``metadata.opf``
    """
    exact: dict[str, list[dict]] = {}
    order: list[str] = []
    for cand in candidates:
        key = group_key(cand.get("title", ""), _first_author(cand.get("authors")))
        if not key:
            key = "|" + (cand.get("path") or "")
        if key not in exact:
            exact[key] = []
            order.append(key)
        exact[key].append(cand)

    # Pass 2 -- adopt the author-less groups.
    by_title: dict[str, list[str]] = {}
    for key in order:
        norm_title = key.split("|", 1)[0]
        if key.endswith("|"):
            continue
        by_title.setdefault(norm_title, []).append(key)

    for key in list(order):
        if not key.endswith("|"):
            continue
        hosts = by_title.get(key[:-1]) or []
        if len(hosts) == 1:
            exact[hosts[0]].extend(exact.pop(key))
            order.remove(key)

    # Pass 3 -- prefix merge within one author.
    merged: dict[str, list[dict]] = {}
    final_order: list[str] = []
    for key in order:
        norm_title, _, norm_author = key.partition("|")
        target = None
        if len(norm_title) >= _PREFIX_MERGE_MIN_LEN:
            for other in final_order:
                other_title, _, other_author = other.partition("|")
                if other_author != norm_author:
                    continue
                if len(other_title) < _PREFIX_MERGE_MIN_LEN:
                    continue
                shorter, longer = sorted((norm_title, other_title), key=len)
                if longer.startswith(shorter):
                    target = other
                    break
        if target is None:
            merged[key] = list(exact[key])
            final_order.append(key)
        else:
            merged[target].extend(exact[key])

    return [merged[key] for key in final_order]
