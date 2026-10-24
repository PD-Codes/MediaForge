"""Scanning one library location for comics.

Shape of the result: a list of SERIES, each carrying its issues. Books are
returned flat because a novel stands alone; a comic almost never does. A run
of "Batman" is 200 files that belong together, and a flat list of 200 cards is
not a shelf, it is a directory listing. Grouping here rather than in the
frontend also means the count on the hub tile and the count on the card come
from the same code.

Metadata precedence, highest first:
  1. ComicInfo.xml inside the archive (comicinfo.py) -- states series and
     issue number as separate fields, so nothing has to be guessed
  2. the filename (identity.py)

ComicVine is NOT part of this. The service exists (web/comicvine_service.py)
and is configurable under Integrations, but nothing here imports or calls it:
this docstring claimed a third stage that was never wired up, which is worse
than the gap itself -- it is what a reader checks against when a comic comes
out with the wrong publisher. A remote lookup also does not belong in a scan
that walks a whole library under a time budget; if it is added, it belongs
where the video side puts it, as an on-demand enrichment behind the detail
panel.

Covers are NOT extracted here. Opening every archive to pull its first image
would make the first scan of a large library take minutes for data that most
of it never displays; the cover route generates and caches on demand instead,
exactly as the video scanner defers ffprobe. ComicInfo.xml *is* read during
the scan, because it comes out of the archive's central directory in about a
millisecond and everything about the shelf depends on it -- but under a time
budget, so a library on a slow network share still returns.
"""
from __future__ import annotations

from pathlib import Path
import os
import time

from ..media_types import COMIC_ARCHIVE_EXTS
from ..media_types import COMIC_EXTS
from ...logger import get_logger
from . import archive
from . import comicinfo
from . import identity

logger = get_logger(__name__)

# Bump when the shape below changes, so routes/library.py re-reads instead of
# serving rows built by an older scanner. Same contract as BOOKS_FORMAT_VERSION.
#
# Deliberately NOT bumped when `needs_conversion` started accounting for the
# conversion cache: a bump forces a full comic rescan of every library on the
# next request, and the only rows it would correct are already corrected on
# their way out by routes/library.py's _lib_refresh_comic_conversion_state().
# Minutes of rescanning to persist a boolean that is recomputed anyway is not a
# trade worth making; the next scan that happens for its own reasons writes it.
#
# v3: rows the scan can take over the next time (see scan_comics' *previous*).
# A row now says when its ComicInfo.xml was skipped for time ("meta_pending"),
# and keeps its own "series" when that differs from the series it was grouped
# under. A v2 row says neither, so it cannot be reused -- hence the bump, which
# costs one full comic scan per location, once.
COMICS_FORMAT_VERSION = 3

# Fields that stay in an issue row even when they are falsy, because a
# consumer distinguishes "absent" from "zero" for them.
_ALWAYS_KEEP = frozenset({"path", "file", "key", "number", "size", "readable"})

# The ComicInfo-derived fields of a row, i.e. what a reused row carries over.
_META_FIELDS = ("number", "volume", "title", "year", "summary", "publisher",
                "writers", "characters", "language", "page_count", "rtl")

# Directories that never hold comics, skipped whole.
_SKIP_DIRS = frozenset({
    "__macosx", ".git", ".svn", "@eadir", "#recycle", ".@__thumb",
    "$recycle.bin", "system volume information", ".stfolder", ".stversions",
})

# Seconds this scan may spend reading ComicInfo.xml, across the whole location.
# Everything past it falls back to the filename, which is what an archive
# without a sidecar gets anyway -- so the degradation is "less metadata", not
# "missing issues". Such a row is marked "meta_pending" and read by a later
# scan; unchanged files are taken over from the previous scan without opening
# them, so the budget goes to new files and the metadata accumulates.
_COMICINFO_TIME_BUDGET = 45

# Hard ceiling on files walked, so a path pointed at a whole disk cannot turn
# one scan into an unbounded walk.
_MAX_FILES = 200000


def _walk_comic_files(base: Path):
    """(path, stat) for every comic file below `base`."""
    seen = 0
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() not in COMIC_EXTS:
                continue
            seen += 1
            if seen > _MAX_FILES:
                logger.warning("[Comics] %s holds more than %d comic files -- stopping the walk",
                               base, _MAX_FILES)
                return
            full = Path(root) / name
            try:
                st = full.stat()
            except OSError:
                continue
            if not st.st_size:
                continue                      # a zero-byte file is a failed copy
            yield full, st


def _read_container(path: Path, ext: str, deadline: float):
    """``(fmt, ComicInfo dict, meta_pending)`` for one file, with ONE open of it.

    The first bytes say what the container is, and the same handle is then
    read as that archive for its ComicInfo.xml. It used to be opened three
    times (sniff, list, read), each one a round trip on a network share and
    the last two a read of the archive's whole directory.

    *meta_pending* is True when a ComicInfo.xml could have been read but the
    time budget was spent: the next scan reads it instead of reusing the row.
    """
    try:
        with open(path, "rb") as fh:
            fmt = archive.sniff_head(fh.read(512))
            if ext not in COMIC_ARCHIVE_EXTS or not archive.is_native(fmt):
                return fmt, {}, False
            if time.monotonic() >= deadline:
                return fmt, {}, True
            return fmt, comicinfo.read_open(fh, fmt, path) or {}, False
    except OSError:
        # Unreadable: what archive.sniff() answers for it, and no metadata.
        return "", {}, False


def _issue_from(path: Path, st, base: Path, deadline: float) -> dict:
    """One issue row: ComicInfo if affordable, filename otherwise."""
    ext = path.suffix.lower()
    fmt, meta, meta_pending = _read_container(path, ext, deadline)

    guessed = identity.parse(path.stem)

    # Precedence for the SERIES name, highest first:
    #
    #   1. ComicInfo.xml   -- states it as a field, nothing to infer
    #   2. the parent FOLDER -- when the file sits in one
    #   3. the filename
    #
    # The folder outranking the filename is the important part, and it took
    # real data to see why. A library laid out as "Comics/<Series>/<issue>"
    # states the series name ONCE, deliberately, in the folder. The filenames
    # in it are 500 chances to drift: the same run arrives as "Die tollsten
    # Geschichten von Donald Duck 388" and "... mit Donald Duck 457", as
    # "Bessy 001 - ..." and "Bessy_326", with percent-encoded umlauts in some
    # and not others. Grouping on the filename turned 5,230 files into 861
    # shelves; grouping on the folder turns them into the eight the user
    # actually filed.
    #
    # The immediate parent, not the top-level folder: "Publisher/Series/issue"
    # is at least as common a layout as "Series/Volume/issue", and this is the
    # reading that gets the first one right.
    number = meta.get("number") or guessed["number"]
    volume = meta.get("volume") or guessed["volume"]
    title = meta.get("title") or guessed["title"]
    year = meta.get("year") or guessed["year"]

    folder_series = ""
    try:
        if path.parent != base and path.parent != base.parent:
            folder_series = path.parent.name
    except (OSError, ValueError):
        folder_series = ""

    series = meta.get("series") or folder_series or guessed["series"] or path.stem

    # A story title that only repeats the series name carries nothing: the
    # card already says it. "Asterix 001 - Asterix der Gallier" keeps its
    # title, "Bessy 001 - Bessy" does not.
    if title and identity.normalize(title) == identity.normalize(series):
        title = ""

    fields = {
        "series": series,
        "number": number,
        "volume": volume,
        "title": title,
        "year": year,
        "summary": meta.get("summary", ""),
        "publisher": meta.get("publisher", ""),
        "writers": meta.get("writers", []),
        "characters": meta.get("characters", []),
        "language": meta.get("language", ""),
        "page_count": meta.get("page_count", 0),
        "rtl": bool(meta.get("rtl")),
    }
    return _issue_row(path, st, fmt, fields, meta_pending)


def _issue_reused(path: Path, st, row: dict, series: str) -> dict:
    """An unchanged file's row, taken over from the previous scan.

    Everything in a row follows from the file's bytes and its place below the
    location, and neither has changed (same path, size and mtime) -- except
    whether a RAR/ACE has been repacked since, which _issue_row() asks the
    conversion cache again, exactly as for a new file.
    """
    fields = {k: row.get(k) for k in _META_FIELDS}
    fields["series"] = series
    return _issue_row(path, st, row.get("format") or "", fields, False)


def _issue_row(path: Path, st, fmt: str, fields: dict, meta_pending: bool) -> dict:
    """The row for one issue, from its container format and its metadata --
    the one place a row is put together, for new and for reused files alike,
    so the two cannot differ."""
    ext = path.suffix.lower()
    series = fields["series"]
    number = fields["number"]
    native = archive.is_native(fmt)
    direct = fmt in archive.DIRECT_FORMATS
    # A RAR/ACE that has already been repacked is readable and does NOT need
    # preparing again. Asked here (one cache lookup, and only for the formats
    # that could have one) so a fresh scan does not hand the shelf a "!" badge
    # for issues the user prepared minutes ago. Cached rows are corrected on
    # the way out instead -- see routes/library.py's
    # _lib_refresh_comic_conversion_state().
    converted = False
    if fmt and not native and not direct:
        try:
            from . import convert as _convert
            converted = _convert.is_converted(path)
        except Exception:
            # A cache that cannot be read is not a reason to fail a scan of
            # thousands of files; the issue simply keeps its "to prepare" state.
            converted = False

    row = {
        # Stable identity for reading progress and bookmarks. Built from what
        # the issue IS, not from where it sits, so moving a run into a
        # different folder does not reset how far the user has read. The file
        # name is the tie-breaker: a series can legitimately hold two files
        # with the same number (a variant cover, a re-scan) and they must not
        # share a bookmark.
        "key": "comic|{}|{}|{}".format(
            identity.normalize(series), (number or "?"), path.name.lower()
        ),
        "path": str(path),
        "file": path.name,
        "ext": ext,
        "format": fmt or "",
        "format_label": archive.FORMAT_LABELS.get(fmt, ext.lstrip(".").upper()),
        # Can the page routes serve this file as it is on disk right now?
        # RAR/ACE say no until convert.py has repacked them, and the shelf
        # shows that as a state rather than as a broken card.
        "readable": bool(native or direct or converted),
        "direct": direct,
        "needs_conversion": bool(fmt and not native and not direct and not converted),
        # Consumed by _series_key() during grouping; scan_comics() then drops
        # it wherever the series object above the issue already says it.
        "series": series,
        "number": str(number or ""),
        "volume": str(fields["volume"] or ""),
        "title": fields["title"],
        "year": fields["year"],
        "summary": fields["summary"],
        "publisher": fields["publisher"],
        "writers": fields["writers"],
        "characters": fields["characters"],
        "language": fields["language"],
        "page_count": fields["page_count"],
        "rtl": bool(fields["rtl"]),
        "size": int(getattr(st, "st_size", 0) or 0),
        "added_at": int(getattr(st, "st_mtime", 0) or 0),
        # ComicInfo.xml not read yet (time budget) -- see _read_container().
        "meta_pending": bool(meta_pending),
    }

    # Drop what carries no information. This whole dict is stored as JSON in
    # the library cache and shipped to the browser on every shelf load, and at
    # library scale the FIELD NAMES cost as much as the values: on a real
    # 5,230-issue library, ~1.4 MB of the 2.8 MB payload was keys like
    # "characters": [] and "needs_conversion": false repeated five thousand
    # times. The frontend reads every one of these with `issue.x || default`,
    # so an absent key and an empty one are already the same thing to it.
    # (`series` is dropped later, in scan_comics(), where the series object
    # that holds the issue is known.)
    return {k: v for k, v in row.items()
            if v not in (None, "", 0, False, []) or k in _ALWAYS_KEEP}


def _series_key(issue) -> str:
    """Group key. Volume is part of it on purpose: a 2011 "Batman" relaunch is
    a different run from the 1940 one, and merging them produces a shelf with
    two issue #1s and no way to tell them apart."""
    return identity.normalize(issue.get("series") or "") + "|" + (issue.get("volume") or "")


def _pick(issues, field):
    """First non-empty value of `field` across a series' issues.

    Series-level facts (publisher, summary) are stored per issue in
    ComicInfo.xml, and typically only some issues carry them.
    """
    for issue in issues:
        val = issue.get(field)
        if val:
            return val
    return "" if field != "year" else None


def _reuse_index(previous) -> dict:
    """``{(path, size, int(mtime)): (row, series)}`` of the previous scan's rows
    that may be taken over: everything but the rows whose ComicInfo.xml was
    skipped for time, which are read again instead. *series* is the row's own
    series spelling -- kept in the row when it differs from its group's,
    otherwise the group's (see scan_comics)."""
    index = {}
    for group in previous or ():
        name = group.get("series") or ""
        for row in group.get("issues") or ():
            if not row.get("path") or row.get("meta_pending"):
                continue
            key = (row["path"], row.get("size") or 0, row.get("added_at") or 0)
            index[key] = (row, row.get("series") or name)
    return index


def scan_comics(base, previous=None) -> list:
    """Index one library location. Returns a list of series dicts.

    *previous* is this location's result of the last scan (same
    COMICS_FORMAT_VERSION; anything else must not be passed). A file whose
    path, size and mtime are unchanged is taken over from it instead of being
    opened again -- that used to happen to every file on every scan, and the
    ComicInfo time budget was spent on the same first files each time, so the
    rest of a large library never got its metadata. Now the budget goes to new
    and changed files only, and what it could not reach is read next time.
    """
    base = Path(base)
    try:
        if not base.is_dir():
            return []
    except OSError:
        return []

    reuse = _reuse_index(previous)
    deadline = time.monotonic() + _COMICINFO_TIME_BUDGET
    buckets: dict = {}
    for path, st in _walk_comic_files(base):
        try:
            old = reuse.get((str(path), int(st.st_size), int(st.st_mtime)))
            if old is not None:
                issue = _issue_reused(path, st, *old)
            else:
                issue = _issue_from(path, st, base, deadline)
        except Exception:
            # One unreadable file must not cost the whole location.
            logger.debug("[Comics] Skipping %s", path, exc_info=True)
            continue
        buckets.setdefault(_series_key(issue), []).append(issue)

    if not buckets:
        return []

    out = []
    for key, issues in buckets.items():
        # "series" survived this far so grouping could use it; from here the
        # series object carries it and 5,000 copies of the same string do not.
        # An issue keeps its own only where it is spelled differently from the
        # group's ("BATMAN" in a "Batman" group): the next scan needs that
        # spelling to take the row over unchanged (see _reuse_index).

        issues.sort(key=lambda i: (identity.issue_sort_key(i.get("number") or ""),
                                   (i.get("file") or "").lower()))
        first = issues[0]
        series_name = _pick(issues, "series") or first.get("series") or ""
        out.append({
            "key": key,
            "series": series_name,
            "sort_series": identity.normalize(series_name),
            "volume": first.get("volume") or "",
            "publisher": _pick(issues, "publisher"),
            "summary": _pick(issues, "summary"),
            "year": _pick(issues, "year"),
            "writers": _pick(issues, "writers") or [],
            "language": _pick(issues, "language"),
            "issue_count": len(issues),
            "total_size": sum(i.get("size") or 0 for i in issues),
            # The shelf shows one cover per series, and the first issue in
            # reading order is the one a reader recognises.
            "cover_source": first.get("path") or "",
            "readable_count": sum(1 for i in issues if i.get("readable")),
            "needs_conversion_count": sum(1 for i in issues if i.get("needs_conversion")),
            "added_at": max((i.get("added_at") or 0) for i in issues),
            "issues": [{k: v for k, v in i.items() if k != "series" or v != series_name}
                       for i in issues],
        })

    out.sort(key=lambda s: (s["sort_series"], s["volume"]))
    logger.info("[Comics] %s: %d series, %d issue(s)",
                base, len(out), sum(s["issue_count"] for s in out))
    return out


def iter_issue_paths(series_list):
    """Every issue path in a scan result -- what the cache purges compare against."""
    for series in series_list or []:
        for issue in series.get("issues") or []:
            if issue.get("path"):
                yield issue["path"]
