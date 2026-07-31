"""Which file extensions MediaForge treats as which kind of media.

Before this module the answer lived in three different places that had drifted
apart: ``routes/library.py`` scanned ``{.mkv, .mp4, .ts}``, ``library_watcher``
reacted to nine extensions, and ``dupecheck`` used a seventh set. A file could
therefore trigger a rescan that then refused to index it. This module is the
single answer, and every one of those call sites now imports from here.

The split into VIDEO/BOOK is deliberately *not* a widening of one set. The
library's path guard (``lib_resolve_library_file``) answers "may the caller
touch this file?", and several callers behind it -- the ffprobe media-info
route, the upscale worker, the encoding worker -- assume that a path which
passed the guard is a video they may probe, re-encode and overwrite. Handing
them an .epub would at best waste an 8 s ffprobe timeout and at worst destroy
the file. So the guard takes the permitted set as an argument and each caller
names the kind it actually wants.
"""
from pathlib import Path


# Containers the scanner indexes and the player can be asked to stream.
VIDEO_EXTS = frozenset({".mkv", ".mp4", ".ts"})

# The wider set the filesystem watcher reacts to. A rescan is cheap and a file
# that turns out not to be indexable simply produces no entry, so it is better
# to wake up too often than to miss a download that lands as .avi or .mov.
VIDEO_WATCH_EXTS = frozenset(
    VIDEO_EXTS | {".avi", ".webm", ".flv", ".mov", ".wmv", ".m4v"}
)

# Book formats the reader can open. Order matters nowhere here, but see
# BOOK_FORMAT_RANK below for the preference used when one book exists several
# times over.
#   .epub  open standard, read natively in the browser
#   .azw3  Amazon's KF8 -- an EPUB in a different wrapper
#   .mobi  the old Mobipocket format
#   .azw   Mobipocket with an Amazon header; same parser as .mobi
#   .pdf   fixed layout, read with pdf.js
BOOK_EXTS = frozenset({".epub", ".azw3", ".mobi", ".azw", ".pdf"})

# Recognised, listed, but not openable. .kfx is Amazon's current format and is
# DRM-protected. Showing it as an unreadable format beats hiding it: a book
# that exists only as .kfx would otherwise look like a scanning failure.
BOOK_UNREADABLE_EXTS = frozenset({".kfx"})

BOOK_ALL_EXTS = frozenset(BOOK_EXTS | BOOK_UNREADABLE_EXTS)

# Formats no browser can render, but which the server can turn into an EPUB
# (see web/books/convert.py). Kept separate from BOOK_EXTS so the reader can
# tell "open this directly" from "ask for a conversion first".
BOOK_CONVERTIBLE_EXTS = frozenset({".mobi", ".azw3", ".azw"})

# Formats the browser reads as they are.
BOOK_DIRECT_EXTS = frozenset({".epub", ".pdf"})

# Which file to offer first when the same book exists in several formats.
# EPUB first because it needs no conversion, PDF last because it is a fixed
# layout that cannot reflow to a phone screen.
BOOK_FORMAT_RANK = {".epub": 0, ".azw3": 1, ".mobi": 2, ".azw": 3, ".pdf": 4, ".kfx": 9}

# ---------------------------------------------------------------------------
# Comics
#
# Six extensions, five different containers, and the extension is NOT to be
# trusted: a large share of the .cbr files in circulation are plain ZIPs that
# someone renamed, because "CBR" became the generic word for "comic archive".
# comics/archive.py therefore sniffs the magic bytes and treats the extension
# as a hint only -- which is also what makes those mislabelled files readable
# without any RAR tooling at all.
#
#   .cbz  ZIP    -- stdlib zipfile
#   .cbt  TAR    -- stdlib tarfile
#   .cb7  7-Zip  -- py7zr, pure Python, no binary
#   .cbr  RAR    -- needs an external unrar/bsdtar; converted to CBZ once
#   .cba  ACE    -- needs an external unace; see comics/convert.py for why
#                   this one is best-effort and deliberately not installed
COMIC_ARCHIVE_EXTS = frozenset({".cbz", ".cbt", ".cb7", ".cbr", ".cba"})

# Containers a comic can be read from with no external tool.
COMIC_NATIVE_EXTS = frozenset({".cbz", ".cbt", ".cb7"})

# PDF is deliberately in BOTH the book and the comic set. The file itself does
# not say which it is -- a scanned graphic novel and a novel are the same
# container -- so the answer comes from the library the path is assigned to
# (see web/media_kinds.py). A PDF in a comics-only path is a comic; in a
# books-only path it is a book; in a path assigned to both it is listed on
# both shelves, which is the honest answer when nothing on disk decides it.
COMIC_EXTS = frozenset(COMIC_ARCHIVE_EXTS | {".pdf"})

# Page images served out of a comic archive. Narrow on purpose: this set gates
# what the page route will hand to a browser, so it must not grow into
# "anything that might be an image".
COMIC_PAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"})

# Files that sit INSIDE a comic archive but are not pages.
COMIC_SIDECAR_NAMES = frozenset({"comicinfo.xml"})


# Image formats accepted when a cover is served straight from the library
# folder. Deliberately narrow: this set gates a route that reads a file the
# client named, so it must not grow into "anything the browser might render".
BOOK_COVER_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

# Sidecar files that live *next to* a book and must never become one themselves.
BOOK_SIDECAR_NAMES = frozenset(
    {"cover.jpg", "cover.jpeg", "cover.png", "metadata.opf", "metadata.db"}
)


def is_comic_file(path) -> bool:
    """True if this path is a comic container, by extension alone.

    Only a first filter: the scanner still sniffs the container, because the
    extension routinely lies (see COMIC_ARCHIVE_EXTS above).
    """
    return Path(path).suffix.lower() in COMIC_EXTS


def media_type_for(path) -> str:
    """Return "video", "book" or "" for a path, by extension alone.

    Callers use this to branch; it deliberately does not touch the filesystem
    so it stays usable on a path that no longer exists (a delete event).
    """
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTS:
        return "video"
    if suffix in BOOK_ALL_EXTS:
        return "book"
    return ""


def is_watchable(path) -> bool:
    """True if a filesystem event for this path should trigger a rescan."""
    suffix = Path(path).suffix.lower()
    return (suffix in VIDEO_WATCH_EXTS
            or suffix in BOOK_ALL_EXTS
            or suffix in COMIC_EXTS)


def book_format_sort_key(ext: str) -> int:
    return BOOK_FORMAT_RANK.get((ext or "").lower(), 8)
