"""Finding books on disk.

The video scanner in ``routes/library.py`` is positional: a directory directly
under the library root is a title, and a file only counts if its *name* carries
an ``SxxExx`` marker. Neither assumption survives contact with a book library.
In a Calibre layout the top-level directory is the **author**, the book sits one
level further down in ``Title (id)/``, and no filename anywhere matches an
episode pattern -- run the video scanner over ``X:\\_Calibre`` and it finds
exactly nothing.

So books get their own pass, and it is driven by the file extension rather than
by the shape of the tree:

    for every file under <base>, at any depth:
        if its extension is a book format -> candidate

Where the file sits then only *informs* the metadata (an ``metadata.opf`` next
to it, a parent folder that looks like an author name); it never decides whether
the file counts. That is what lets a book that exists both loose in the library
root and inside a Calibre folder merge into one entry -- see
:func:`identity.merge_groups`.
"""
from __future__ import annotations

import os
from pathlib import Path

from ...logger import get_logger
from ..media_types import (
    BOOK_ALL_EXTS,
    BOOK_EXTS,
    BOOK_SIDECAR_NAMES,
    book_format_sort_key,
)
from . import calibre_db
from .identity import (
    clean_title,
    looks_like_author_folder,
    merge_groups,
    normalize,
    split_filename,
    split_series,
)
from .opf import parse_opf

logger = get_logger(__name__)

# A runaway walk is worse than an incomplete one: the scan holds a lock the
# whole library shares. 200k book files is far beyond any real collection.
_MAX_BOOK_FILES = 200_000

# Directories never worth descending into.
_SKIP_DIRS = frozenset(
    {".calibre", ".caltrash", "__pycache__", ".git", ".svn", "$recycle.bin", "_to_delete"}
)

_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")

_DESCRIPTION_LIMIT = 1200


def _is_book_file(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(".") or lowered.startswith(".temp_"):
        return False
    if lowered.endswith(".part") or ".part" in lowered:
        return False
    if lowered in BOOK_SIDECAR_NAMES:
        return False
    return os.path.splitext(lowered)[1] in BOOK_ALL_EXTS


def _walk_book_files(base: Path):
    """Yield ``(Path, os.stat_result)`` for every book file under ``base``.

    Hand-rolled on ``os.scandir`` rather than ``Path.rglob`` because the stat
    result comes free with the directory entry here -- over a network share
    that is the difference between one syscall per file and three.
    """
    stack = [base]
    seen_dirs: set = set()
    count = 0
    while stack:
        current = stack.pop()
        try:
            # Resolving guards against a symlink loop walking forever.
            real = current.resolve()
            if real in seen_dirs:
                continue
            seen_dirs.add(real)
            entries = list(os.scandir(current))
        except (OSError, ValueError):
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and _is_book_file(entry.name):
                    count += 1
                    if count > _MAX_BOOK_FILES:
                        logger.warning(
                            "[Books] Stopped after %s files under %s", _MAX_BOOK_FILES, base
                        )
                        return
                    yield Path(entry.path), entry.stat()
            except OSError:
                continue


def _folder_cover(folder: Path) -> str:
    for name in _COVER_NAMES:
        candidate = folder / name
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def _candidate_for(path: Path, stat_result, opf_cache: dict, base: Path = None) -> dict:
    """Build one pre-merge candidate from a single file.

    *base* is the library root. It matters for exactly one decision: a file
    lying loose in the root has no author folder above it, and using the root's
    own name would file every such book under an "author" called after the
    drive or folder the library happens to live in.
    """
    folder = path.parent
    if folder not in opf_cache:
        opf_path = folder / "metadata.opf"
        try:
            opf_cache[folder] = parse_opf(opf_path) if opf_path.is_file() else {}
        except OSError:
            opf_cache[folder] = {}
    opf = opf_cache[folder]

    file_title, file_author = split_filename(path.stem)

    title = opf.get("title") or file_title or path.stem
    authors = list(opf.get("authors") or [])
    if not authors and file_author:
        authors = [file_author]
    is_root = base is not None and folder == base
    if not authors and not is_root and looks_like_author_folder(folder.name):
        authors = [folder.name]

    series = opf.get("series") or ""
    series_index = opf.get("series_index")
    if not series:
        series, series_index = split_series(title)

    return {
        "path": str(path),
        "ext": path.suffix.lower(),
        "size": int(getattr(stat_result, "st_size", 0) or 0),
        "mtime": float(getattr(stat_result, "st_mtime", 0) or 0),
        "folder": str(folder),
        "title": clean_title(title),
        "authors": authors,
        "series": series,
        "series_index": series_index,
        "opf": opf,
    }


def _pick(bucket: list, key: str, default=None):
    """First non-empty value of ``key`` across a bucket's OPF payloads."""
    for cand in bucket:
        value = (cand.get("opf") or {}).get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _build_book(bucket: list, cover_by_folder: dict, catalogue: dict) -> dict:
    """Turn one merged bucket of files into the book entry the API returns."""
    # The longest title wins: Calibre truncates the filename it writes, so the
    # OPF-derived title of one member is regularly the complete one.
    titles = sorted((c.get("title") or "" for c in bucket), key=len, reverse=True)
    title = titles[0] if titles else ""

    authors: list = []
    for cand in bucket:
        for author in cand.get("authors") or []:
            if author and author not in authors:
                authors.append(author)

    series = ""
    series_index = None
    for cand in bucket:
        if cand.get("series"):
            series = cand["series"]
            series_index = cand.get("series_index")
            break

    record = calibre_db.lookup(catalogue, title, authors[0] if authors else "")
    if record:
        # The catalogue is authoritative for the fields the OPF often omits,
        # but it never overrides a title/author we already resolved from the
        # file itself -- that is what the user sees on disk.
        series = series or record.get("series") or ""
        if series_index is None:
            series_index = record.get("series_index")
        for author in record.get("authors") or []:
            if author not in authors:
                authors.append(author)

    formats = []
    seen_paths: set = set()
    for cand in sorted(bucket, key=lambda c: book_format_sort_key(c["ext"])):
        if cand["path"] in seen_paths:
            continue
        seen_paths.add(cand["path"])
        formats.append(
            {
                "ext": cand["ext"].lstrip("."),
                "path": cand["path"],
                "size": cand["size"],
                "mtime": cand["mtime"],
                "readable": cand["ext"] in BOOK_EXTS,
            }
        )

    cover = ""
    for cand in bucket:
        candidate = cover_by_folder.get(cand["folder"])
        if candidate:
            cover = candidate
            break

    description = _pick(bucket, "description") or record.get("description") or ""
    tags = _pick(bucket, "tags") or record.get("tags") or []

    return {
        "key": "{}|{}".format(normalize(title), normalize(authors[0] if authors else "")),
        "title": title,
        "sort_title": normalize(title),
        "authors": authors[:8],
        "series": series,
        "series_index": series_index,
        "language": _pick(bucket, "language", ""),
        "isbn": _pick(bucket, "isbn") or record.get("isbn") or "",
        "published": _pick(bucket, "published") or record.get("published") or "",
        "publisher": _pick(bucket, "publisher", ""),
        "rating": _pick(bucket, "rating"),
        "description": (description or "")[:_DESCRIPTION_LIMIT],
        "tags": list(tags)[:20],
        "cover_path": cover,
        "formats": formats,
        "total_size": sum(f["size"] for f in formats),
        "added_at": max((f["mtime"] for f in formats), default=0.0),
        "media_kind": "book",
        "meta_state": "local",
    }


def scan_books(base) -> list:
    """Scan one library location for books.

    Reads the filesystem plus, where present, ``metadata.opf`` and the Calibre
    ``metadata.db``. It deliberately does **not** open any EPUB/MOBI/PDF: those
    cost tens of milliseconds each and would make the first scan of a large
    library unbearable. Embedded metadata and cover extraction happen later, in
    a background pass, exactly as ffprobe results do for video.
    """
    base = Path(base)
    try:
        if not base.is_dir():
            return []
    except OSError:
        return []

    files = list(_walk_book_files(base))
    if not files:
        return []

    # One cover per folder, and only when that folder holds a single book --
    # otherwise a stray cover.jpg in a dump directory would be handed to every
    # book in it.
    books_per_folder: dict = {}
    for path, _stat in files:
        books_per_folder[str(path.parent)] = books_per_folder.get(str(path.parent), 0) + 1
    cover_by_folder: dict = {}
    for folder, count in books_per_folder.items():
        if count == 1:
            found = _folder_cover(Path(folder))
            if found:
                cover_by_folder[folder] = found

    catalogue = calibre_db.load_catalogue(base / "metadata.db")

    opf_cache: dict = {}
    candidates = [_candidate_for(path, st, opf_cache, base) for path, st in files]
    buckets = merge_groups(candidates)
    books = [_build_book(b, cover_by_folder, catalogue) for b in buckets]
    books.sort(key=lambda b: (b["sort_title"], b["title"].lower()))

    logger.info(
        "[Books] %s files -> %s books under %s", len(files), len(books), base
    )
    return books
