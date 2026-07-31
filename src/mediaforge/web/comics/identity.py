"""Turning a comic filename into a series, an issue number and a volume.

This runs only when ComicInfo.xml did not answer (comicinfo.py), which is
often enough to matter: anything ripped or downloaded outside a comic manager
arrives as a bare filename, and the filename is all there is.

The naming conventions are informal but remarkably consistent, because they
all descend from the same scene tools:

    Batman 001 (2011).cbz
    Batman #1 (2011).cbz
    Batman v2 001 (of 12).cbz
    Batman (2011) 001 - The Beginning.cbz
    Batman - 001 - The Beginning.cbz
    Batman Annual 01 (2012).cbz
    Batman 001 (2011) (Digital) (Zone-Empire).cbz

The parts are: a series name, an issue number, optionally a volume (either
"v2" or a bracketed year), optionally a story title, and a tail of bracketed
scene tags that carry no meaning for us.

Everything here is pure -- strings in, strings out, no filesystem. Same reason
as books/identity.py: it is the piece most in need of tests, and a pure
function needs no fixture beyond the awkward real name itself.
"""
from __future__ import annotations

import re
import unicodedata


# Bracketed groups: years, scan tags, release groups, "(of 12)".
_BRACKET_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]{0,60})[\)\]\}]")

# A four-digit year, either bracketed or standing alone at the end.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# "v2", "vol. 2", "volume 2" -- the other meaning of volume.
_VOLUME_RE = re.compile(r"\bv(?:ol(?:ume)?)?\.?\s*(\d{1,4})\b", re.IGNORECASE)

# The issue number, at the end of what is left after the brackets come off.
# Ordered alternation, most explicit first:
#   "#12", "#12.5"    -- unambiguous
#   "Annual 3"        -- a named special, kept as part of the number
#   " 012"            -- the common bare form, needs at least one leading space
_ISSUE_RE = re.compile(
    r"(?:"
    r"#\s*(?P<hash>\d{1,5}(?:\.\d{1,2})?)"
    r"|\b(?P<special>Annual|Special|Extra|Omnibus|TPB|HC|One[- ]?Shot)\s*(?P<specialnum>\d{1,5})?\b"
    r"|(?<=[\s_.\-])(?P<bare>\d{1,5}(?:\.\d{1,2})?)"
    r")\s*$",
    re.IGNORECASE,
)

# A stem that is nothing but a number: "001", "12.5".
_BARE_NUM_RE = re.compile(r"\d{1,5}(?:\.\d{1,2})?")

# Separator runs left behind once a part has been removed.
_SEP_TAIL_RE = re.compile(r"[\s\-_–—:.]+$")
_SEP_HEAD_RE = re.compile(r"^[\s\-_–—:.]+")

# Scene tags that are never part of a series name. Only used to decide whether
# a bracketed group is worth keeping as a title candidate.
_SCENE_TAGS = frozenset({
    "digital", "webrip", "scan", "c2c", "f", "fixed", "repack", "reprint",
    "covers", "noads", "empire", "zone-empire", "minutemen", "dcp", "phillywilly",
    "theproletariat", "yoink", "darkness-empire", "ttr", "gotham", "novus",
    "rescan", "re-scan", "hd", "requested", "incomplete", "complete",
})


# "%f6" for "ö": filenames that have been through a web download keep the
# percent-encoding of whatever produced them. Decoded here rather than in the
# scanner so the shelf shows "In der Hölle der Sioux" and, more importantly,
# so two spellings of the same series name normalise to one.
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def _decode_percent(value: str) -> str:
    if "%" not in value:
        return value
    try:
        # latin-1 first: these names come from Windows tooling, where the
        # escaped byte is cp1252/latin-1 far more often than UTF-8. A wrong
        # guess here would turn one mojibake into another, so anything that
        # does not decode cleanly is left exactly as it was.
        raw = _PERCENT_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
        return raw
    except (ValueError, OverflowError):
        return value


def _strip_seps(value: str) -> str:
    return _SEP_HEAD_RE.sub("", _SEP_TAIL_RE.sub("", value or "")).strip()


def normalize(value: str) -> str:
    """Casefolded, accent-free, punctuation-free form used for grouping.

    Two files belong to the same series when this matches. Accents are folded
    because the same series is written "Pokémon" and "Pokemon" depending on who
    packed it, and an "&"/"and" split would otherwise create two shelves for
    one run.
    """
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def issue_sort_key(number: str):
    """Sort key that orders 1, 1.5, 2, 10 -- and puts specials last.

    Issue numbers are not integers: "0" (a prologue), "1.5" (an interlude) and
    "Annual 2" are all real, and the last one has no place on the numeric line
    at all, so it sorts after the run rather than pretending to be issue 2.
    """
    raw = (number or "").strip()
    if not raw:
        return (2, 0.0, "")
    m = re.match(r"^(\d{1,5}(?:\.\d{1,2})?)$", raw)
    if m:
        return (0, float(m.group(1)), "")
    m = re.search(r"(\d{1,5}(?:\.\d{1,2})?)\s*$", raw)
    if m:
        return (1, float(m.group(1)), raw.lower())
    return (1, 0.0, raw.lower())


def parse(stem: str) -> dict:
    """Best-effort {series, number, volume, year, title} from a filename stem.

    Never raises and never returns None: an unparseable name still has to
    produce a shelf entry, so the whole stem becomes the series and the issue
    number stays empty.
    """
    original = _strip_seps(_decode_percent(stem or ""))
    if not original:
        return {"series": "", "number": "", "volume": "", "year": None, "title": ""}

    year = None
    volume = ""
    leftovers = []

    def _take_bracket(m):
        inner = (m.group(1) or "").strip()
        low = inner.lower()
        nonlocal year, volume
        if _YEAR_RE.match(inner):
            if year is None:
                year = int(inner)
            return " "
        # "(Ehapa 1965)", "(Tigerpress 06-2008)": a publisher and a year in one
        # bracket. The year is the part worth keeping; the rest is an imprint
        # or a scan date and must not become the story title.
        trailing_year = re.search(r"(?:^|[\s\-])((?:19|20)\d{2})\s*$", inner)
        if trailing_year:
            if year is None:
                year = int(trailing_year.group(1))
            return " "
        if re.match(r"^of\s*\d{1,5}$", low):          # "(of 12)": run length
            return " "
        vm = _VOLUME_RE.fullmatch(inner)
        if vm:
            volume = vm.group(1)
            return " "
        if low in _SCENE_TAGS or re.match(r"^[a-z0-9\-]{1,20}$", low) and low in _SCENE_TAGS:
            return " "
        leftovers.append(inner)
        return " "

    work = _BRACKET_RE.sub(_take_bracket, original)

    # "v2" outside brackets.
    vm = _VOLUME_RE.search(work)
    if vm and not volume:
        volume = vm.group(1)
        work = work[:vm.start()] + " " + work[vm.end():]
    elif vm:
        work = work[:vm.start()] + " " + work[vm.end():]

    work = re.sub(r"\s+", " ", work).strip()

    # A trailing " - Story Title" is split off before the number is read, so
    # "Batman - 001 - The Beginning" does not read "Beginning" as the issue.
    title = ""
    parts = re.split(r"\s+[-–—]\s+", work)

    # "038 - Donald hier - Donald da...": the number LEADS and the story title
    # contains a dash of its own. Splitting off the last chunk (below) would
    # take "Donald da..." as the title and leave "038 - Donald hier" behind,
    # where the trailing-number search finds nothing at all. When the first
    # chunk is nothing but a number, everything after it is the title.
    if len(parts) >= 2 and _BARE_NUM_RE.fullmatch(_strip_seps(parts[0])):
        number = _strip_seps(parts[0])
        title = _strip_seps(" - ".join(parts[1:]))
        return {
            "series": "", "number": number, "volume": volume,
            "year": year, "title": title,
        }

    if (len(parts) >= 2
            and _ISSUE_RE.search(_strip_seps(parts[-1])) is None
            # "Gespenster Geschichten - 0001": the last chunk is the ISSUE
            # NUMBER, not a story title. _ISSUE_RE cannot see that on its own
            # because its bare-number branch needs a separator in front of the
            # digits and here the chunk *starts* with them.
            and not _BARE_NUM_RE.fullmatch(_strip_seps(parts[-1]))):
        # last chunk holds no number -> it is a story title
        title = _strip_seps(parts[-1])
        work = _strip_seps(" - ".join(parts[:-1]))

    number = ""
    m = _ISSUE_RE.search(work)
    if m:
        if m.group("hash"):
            number = m.group("hash")
        elif m.group("special"):
            special = m.group("special").title()
            number = f"{special} {m.group('specialnum')}".strip() if m.group("specialnum") else special
        else:
            number = m.group("bare")
        work = _strip_seps(work[:m.start()])

    series = _strip_seps(work)

    # A stem that is nothing BUT a number carries no series name at all --
    # "Lucky Luke/001.cbr" keeps the series in the folder, which is a layout
    # at least as common as putting it in the filename. Two ways to land here:
    #
    #   "001"        -- no issue pattern matched (the bare-number branch needs
    #                   whitespace in front of it, precisely so that a title
    #                   ending in a number is not read as an issue number), so
    #                   the whole stem fell through to `series`
    #   "#003"       -- the number WAS matched and consumed everything, and
    #                   `series` came out empty
    #
    # Both used to end up reporting the number as the series name, which made
    # every issue its own one-shot shelf named "001", "002", "003". Leaving
    # series empty is the honest answer: scanner.py then falls back to the
    # folder name, and that fallback only ever runs when this is empty.
    if series and not number and _BARE_NUM_RE.fullmatch(series) and not _YEAR_RE.match(series):
        number = series
        series = ""

    # A bare year that ended up as the series ("2011") means the name was only
    # a number; keep the original rather than shelving under a year.
    if series and _YEAR_RE.match(series):
        series = original
    elif not series and not number:
        # Genuinely unparseable -- better the raw stem than nothing.
        series = original

    if not title and leftovers:
        # A single leftover bracket that is not a scene tag is usually the
        # story title: "Batman 001 (The Beginning)".
        candidate = _strip_seps(leftovers[0])
        if candidate and candidate.lower() not in _SCENE_TAGS and not candidate.isdigit():
            title = candidate

    return {
        "series": series,
        "number": number,
        "volume": volume,
        "year": year,
        "title": title,
    }
