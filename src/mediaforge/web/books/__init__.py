"""eBook support for the media library.

Books are indexed by a pass of their own (:mod:`.scanner`) and stored under a
separate ``books`` key in the library cache, so nothing in the video pipeline --
scanning, stats, calendar, auto-sync, upscaling -- can be reached by a book
entry. See :mod:`.identity` for how several files become one book.
"""
from .identity import merge_groups, normalize, split_filename, split_series
from .scanner import scan_books

__all__ = [
    "merge_groups",
    "normalize",
    "scan_books",
    "split_filename",
    "split_series",
]
