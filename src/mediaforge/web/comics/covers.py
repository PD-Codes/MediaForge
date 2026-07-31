"""The cover of a comic: the first page, cached as a file.

A comic shelf is a wall of covers, and the cover of a comic archive is not a
file next to it -- it is the first image *inside* it. Producing it means
opening the container and reading one member, which is cheap once and absurd
per page view: a shelf of two thousand issues would otherwise open two
thousand archives on every scroll. So the first page is written to a small
on-disk cache and served from there afterwards.

The cache itself, the downscaling and the background worker are NOT here any
more -- they are web/covercache.py, shared with the book shelf. Nothing in
them was ever comic-specific except the directory name and the log prefix, and
a third shelf would have made it a third copy. What stays here is the only
part that is genuinely about comics: how you get the first page out of a
CBZ/CBR/CB7, and what "no cover is possible" means for each container.

Cached by (path, mtime, size, version), the same identity comics/convert.py
and books/convert.py use: replace the file and the key stops matching, so a
cover can never belong to a different comic than the one on screen.

Two formats deliberately produce no cover:

  * PDF, because pdf.js already renders page one in the browser; extracting
    an embedded image here would need a PDF rasteriser for a picture the
    client can draw itself.
  * RAR/ACE on a machine with no external extractor at all, because nothing
    in this process can read them. A machine without unrar is not an error.

For RAR/ACE where an extractor IS present, the cover is pulled straight out of
the archive by comics.convert.extract_first_image -- one listing and one
member, no repacking. That matters at library scale: the alternative is
unpacking every issue in full and keeping a second copy of it in the
conversion cache, five thousand times over, to produce five thousand
thumbnails. Only when the peek cannot deliver (a tool that cannot extract a
single member, a refused archive) does the background repack get started, and
the cover then appears on a later request.

Nothing here writes anywhere except the cache. THE COMIC FILE ITSELF IS ONLY
EVER OPENED FOR READING.
"""
from __future__ import annotations

import time
from pathlib import Path

from ...logger import get_logger
from ..covercache import MIME_TYPES  # noqa: F401  (re-exported: routes import it from here)
from ..covercache import CoverCache, PrepareWorker
from ..media_types import COMIC_PAGE_EXTS
from . import archive, convert

logger = get_logger(__name__)


# v2: covers are downscaled. Bump this whenever what gets STORED changes --
# every existing entry then stops matching and is produced again rather than
# being served forever in the old shape.
_CACHE = CoverCache(
    subdir="comic_covers",
    version="v2",
    allowed_exts=COMIC_PAGE_EXTS,
    log_prefix="[Comics]",
)

# The thin public surface. Kept as module-level names because routes/comics.py
# and routes/library.py already import them, and because "the comic cover
# cache" reads better at a call site than "_CACHE, which happens to be one".
cache_key = _CACHE.cache_key
has_cover = _CACHE.has_cover
cover_mimetype = _CACHE.mimetype
purge_orphans = _CACHE.purge_orphans
cache_stats = _CACHE.stats
cleanup_covers = _CACHE.cleanup


def cover_path(src, start_conversion: bool = True):
    """The cached cover for *src*, extracting it once if needed. None if there
    is none to have.

    None is a normal answer, not a failure: a PDF (pdf.js draws its own first
    page), a CBR on a machine with no extractor, an archive holding no images,
    a truncated download. The shelf shows a placeholder and moves on.

    *start_conversion* is what licenses this call to do real work on a
    RAR/ACE: with it true the first page is peeked out of the archive (see
    comics.convert.extract_first_image) and, only if that cannot deliver, a
    background repack is started -- in which case this call still returns None
    and the cover appears on a later request. Pass False from anything that
    walks a whole library, so opening the comics page neither runs two hundred
    extractors nor queues two hundred conversions; the background worker in
    this module is what fills those in afterwards.
    """
    src = Path(src)
    try:
        key = cache_key(src)
    except OSError:
        return None

    hit = _CACHE.cached(key)
    if hit is not None:
        return hit

    fmt = archive.sniff(src)
    if fmt in archive.DIRECT_FORMATS:
        return None                      # PDF: the browser renders page one
    if not fmt:
        logger.debug("[Comics] Unrecognised container, no cover: %s", src)
        return None

    read_from, read_fmt = src, fmt
    if not archive.is_native(fmt):
        # RAR/ACE. A cover is one page, so ask for one page: this pulls the
        # first image out of the archive without unpacking the rest of it and
        # without leaving a second copy of the file in the conversion cache.
        # Gated on start_conversion for the reason that flag exists -- it runs
        # an external process, so rendering a whole shelf must not do it.
        if start_conversion:
            name, data = convert.extract_first_image(src, fmt)
            if name and data:
                # Page one is page one: if these bytes cannot be cached (too
                # large, an extension the page route would not serve) then a
                # full conversion would arrive at exactly the same answer.
                return _CACHE.store_image(key, src, name, data)
        # Nothing peekable -- no extractor, or a tool that cannot pull a single
        # member. The long way round, unchanged: only readable through the
        # repacked CBZ, and this never blocks on it. If it is not done, there
        # is no cover yet.
        status = convert.conversion_status(src, start=bool(start_conversion))
        if not status.get("ready"):
            return None
        try:
            read_from = convert.converted_path(status["key"])
        except (KeyError, ValueError):
            return None
        if not read_from.is_file():
            return None
        read_fmt = archive.FMT_ZIP

    name, data = archive.first_page(read_from, read_fmt)
    if not name or not data:
        logger.debug("[Comics] No first page in %s", src)
        return None
    return _CACHE.store_image(key, src, name, data)


_PREP_POLL_SECONDS = 1.5          # how often to re-check a running conversion
_PREP_PER_FILE_TIMEOUT = 900      # give up on one archive after this long

# Covers are prepared ONE PER SERIES by default, not per issue: the grid shows
# a card per series, so on a 5,230-issue library that is eight archives to
# open, not 5,230. Preparing every issue is a separate, opt-in setting.
_WORKER = PrepareWorker("comic-covers", lambda src: _prepare_one(src), "[Comics]")

preparation_status = _WORKER.status
prepare_async = _WORKER.submit
reset_preparation = _WORKER.reset


def _prepare_one(src: Path) -> bool:
    """Make sure one file has a cached cover. True if it now has one.

    For a native container this is a single read. For a RAR/ACE it starts the
    conversion and then WAITS for it -- which is exactly what the shelf route
    must not do and what a background worker is for.
    """
    if has_cover(src):
        return True

    got = cover_path(src, start_conversion=True)
    if got is not None:
        return True

    fmt = archive.sniff(src)
    if fmt in archive.DIRECT_FORMATS or archive.is_native(fmt) or not fmt:
        # Nothing a conversion could fix: a PDF, an unreadable file, or a
        # native archive that simply holds no images.
        return False

    deadline = time.monotonic() + _PREP_PER_FILE_TIMEOUT
    while time.monotonic() < deadline:
        status = convert.conversion_status(src, start=False)
        if status.get("ready"):
            return cover_path(src, start_conversion=False) is not None
        if not status.get("ok") or not status.get("pending"):
            # no_extractor, extract_failed, unsupported -- all terminal, and
            # all of them already logged at debug by convert.py.
            return False
        time.sleep(_PREP_POLL_SECONDS)
    logger.info("[Comics] Gave up waiting for %s to be prepared", src.name)
    return False
