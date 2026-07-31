"""The cover of a comic: the first page, cached as a file.

A comic shelf is a wall of covers, and the cover of a comic archive is not a
file next to it -- it is the first image *inside* it. Producing it means
opening the container and reading one member, which is cheap once and absurd
per page view: a shelf of two thousand issues would otherwise open two
thousand archives on every scroll. So the first page is written to a small
on-disk cache and served from there afterwards.

Cached by (path, mtime, size, version), the same identity comics/convert.py
and books/convert.py use: replace the file and the key stops matching, so a
cover can never belong to a different comic than the one on screen.

The bytes are stored EXACTLY as they came out of the archive -- no resize, no
re-encode. Pillow is available and running every cover through it would be one
line, but it would cost CPU per cover, lose a generation of JPEG quality and
gain nothing: the browser is going to scale the image to a grid cell either
way, and it does that better than a server-side thumbnail of a guessed size.
The only limit applied is a size ceiling (see _MAX_COVER_BYTES), which is
about memory rather than about pictures.

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

import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

from ...logger import get_logger
from ..media_types import COMIC_PAGE_EXTS
from . import archive, convert

logger = get_logger(__name__)


# Part of the cache key. Bump it when what is stored changes -- if this ever
# does start resizing, every cached cover has to be retired rather than served
# forever at the old size.
_COVER_VERSION = "v2"   # v2: covers are downscaled, see _downscale()

# A cover is one scanned page. A 300 dpi A4 page as PNG is around 10 MB and
# that is already the pathological end; 24 MB is comfortably above anything
# real. The ceiling is not about picture quality, it is about memory: the page
# is read into RAM in one piece on a route any logged-in user can hit, and an
# archive whose "first page" is 400 MB is either malformed or hostile.
_MAX_COVER_BYTES = 24 * 1024 * 1024

# Extensions a cached cover can have, tried in a fixed order when looking one
# up. Sorted so the lookup is deterministic across platforms.
_EXT_ORDER = tuple(sorted(COMIC_PAGE_EXTS))

# What to send with the file. The page route needs it and guessing from the
# extension in three different places is how a WebP ends up labelled JPEG.
MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".avif": "image/avif",
}

_lock = threading.Lock()


def _cache_root() -> Path:
    """Where covers live: ``<config>/comic_covers/<key><ext>``.

    Separate from comic_convert/ on purpose. The two caches have completely
    different economics -- a cover is 200 KB and worth keeping for years, a
    repacked archive is 400 MB and worth dropping after a month -- so they
    must be prunable independently.

    Flat files rather than a directory per cover: a large library produces one
    entry per issue, and a directory per entry would multiply the inode cost
    of the cache by three for no gain.
    """
    try:
        from ...config import MEDIAFORGE_CONFIG_DIR
        base = Path(MEDIAFORGE_CONFIG_DIR)
    except Exception:
        base = Path(tempfile.gettempdir()) / "mediaforge"
    root = base / "comic_covers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_key(path) -> str:
    """Identity of one cover: path + mtime + size + version.

    Raises OSError when the file is gone -- callers treat that as "no cover".
    """
    path = Path(path)
    stat = path.stat()
    raw = "{}|{}|{}|{}".format(
        path.resolve(), int(stat.st_mtime), stat.st_size, _COVER_VERSION
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _cached(key: str):
    """The cached file for *key*, whatever image format it was stored in."""
    root = _cache_root()
    for ext in _EXT_ORDER:
        candidate = root / (key + ext)
        if candidate.is_file():
            return candidate
    return None


def has_cover(src) -> bool:
    """True if a cover for *src* is already cached.

    A pure lookup -- it never opens the archive and never starts a conversion,
    so a shelf renderer can call it for every issue it lists.
    """
    try:
        key = cache_key(src)
    except OSError:
        return False
    return _cached(key) is not None


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

    hit = _cached(key)
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
                return _store_page(key, src, name, data)
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
    return _store_page(key, src, name, data)


def _store_page(key: str, src: Path, name: str, data: bytes):
    """Cache page bytes as the cover for *key*. None if they are not fit to be one.

    Shared by both routes into the cache -- a native archive read in place, and
    a single member peeked out of a CBR -- so the two limits below cannot end
    up applying to one of them and not to the other.
    """
    if len(data) > _MAX_COVER_BYTES:
        logger.info("[Comics] Cover of %s is %s bytes, refusing to cache it",
                    src.name, len(data))
        return None

    ext = Path(name).suffix.lower()
    if ext not in COMIC_PAGE_EXTS:
        # Cannot happen: archive.list_pages and convert.extract_first_image
        # both only yield these. Checked anyway, because this extension becomes
        # a filename in the cache directory.
        return None

    shrunk = _downscale(data, src)
    if shrunk is not None:
        return _store(key, ".webp", shrunk)
    return _store(key, ext, data)


# A cover is drawn into a grid card a few hundred pixels wide. The page it
# comes from is a scan: 1600x2400 and two to five megabytes is ordinary. Kept
# at full size, a 5,000-issue library would spend ~15 GB of cache on thumbnails
# nobody ever sees at that resolution.
#
# 640px on the long edge covers a 320px card at 2x device pixel ratio, which is
# the largest a poster tile gets on any display worth optimising for.
_COVER_MAX_EDGE = 640

# WebP over JPEG: roughly 30% smaller at the same visual quality, it keeps
# transparency (a page image can legitimately have some), and every browser
# that can run this UI has supported it for years.
_COVER_QUALITY = 80


def _downscale(data: bytes, src: Path):
    """Re-encode a page as a small WebP thumbnail. None if that is not possible.

    None is not a failure -- the caller then stores the original bytes, which
    is exactly the old behaviour. Pillow is a hard dependency of MediaForge,
    but a page can still be a format it will not open, and a cover that is
    merely large beats no cover at all.
    """
    try:
        import io

        from PIL import Image
    except Exception:
        return None

    try:
        with Image.open(io.BytesIO(data)) as img:
            # A comic page is one frame; an animated GIF used as a page would
            # otherwise be re-encoded frame by frame for no reason.
            img.seek(0)
            if max(img.size) <= _COVER_MAX_EDGE and len(data) < 120 * 1024:
                # Already small: re-encoding would cost quality and save
                # nothing worth having.
                return None
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img.thumbnail((_COVER_MAX_EDGE, _COVER_MAX_EDGE), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=_COVER_QUALITY, method=4)
            return out.getvalue()
    except Exception:
        # Truncated image, an exotic format, a decompression-bomb refusal --
        # all of it means "keep the original", none of it is an error.
        logger.debug("[Comics] Could not downscale the cover of %s", src.name, exc_info=True)
        return None


def _store(key: str, ext: str, data: bytes):
    """Write cover bytes into the cache atomically. Returns the path or None.

    Through a uniquely named temporary file in the same directory and a
    rename, so two requests racing for the same uncached cover cannot produce
    a half-written file that the loser then serves.
    """
    root = _cache_root()
    target = root / (key + ext)
    tmp = root / "{}.{}-{}.part".format(key, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except OSError as exc:
        logger.info("[Comics] Cannot cache cover %s: %s", key, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return target


def cover_mimetype(path) -> str:
    """The Content-Type for a cached cover file."""
    return MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Background preparation
# ---------------------------------------------------------------------------
# Why this exists: a cover can only be pulled out of a RAR/ACE after
# comics/convert.py has repacked it, and the shelf route deliberately refuses
# to start that (one page load must not queue three hundred conversions). The
# result was a shelf of blank cards that would never fill in. This worker is
# the missing half -- it prepares covers deliberately, in the background, at a
# pace nobody has to wait on.
#
# It prepares ONE COVER PER SERIES, not per issue. The grid shows a card per
# series, so on a 5,230-issue library that is eight archives to open, not
# 5,230. Preparing every issue is a separate, opt-in setting.

_PREP_POLL_SECONDS = 1.5          # how often to re-check a running conversion
_PREP_PER_FILE_TIMEOUT = 900      # give up on one archive after this long
_prep_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "current": "",
    "finished_at": 0,
}
_prep_queue: list = []
_prep_seen: set = set()
_prep_lock = threading.Lock()


def preparation_status() -> dict:
    """What the background cover worker is doing, for the shelf's progress UI."""
    with _prep_lock:
        state = dict(_prep_state)
        state["pending"] = len(_prep_queue)
    return state


def prepare_async(paths) -> int:
    """Queue cover preparation for these comic files. Returns the number queued.

    Safe to call repeatedly: anything already cached, already queued or
    already handled this run is skipped, so a rescan does not redo the work.
    """
    queued = 0
    with _prep_lock:
        for raw in paths or ():
            path = str(raw or "")
            if not path or path in _prep_seen:
                continue
            _prep_seen.add(path)
            _prep_queue.append(path)
            queued += 1
        if not queued and not _prep_state["running"]:
            return 0
        _prep_state["total"] += queued
        if _prep_state["running"]:
            return queued
        _prep_state["running"] = True
        _prep_state["finished_at"] = 0

    threading.Thread(target=_prep_loop, name="comic-covers", daemon=True).start()
    return queued


def _prep_loop() -> None:
    """Drain the queue. Never raises -- a failure is one missing cover."""
    try:
        while True:
            with _prep_lock:
                if not _prep_queue:
                    break
                path = _prep_queue.pop(0)
                _prep_state["current"] = Path(path).name
            ok = False
            try:
                ok = _prepare_one(Path(path))
            except Exception:
                logger.debug("[Comics] Cover preparation failed for %s", path, exc_info=True)
            with _prep_lock:
                _prep_state["done"] += 1
                if not ok:
                    _prep_state["failed"] += 1
    finally:
        with _prep_lock:
            _prep_state["running"] = False
            _prep_state["current"] = ""
            _prep_state["finished_at"] = int(time.time())


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


def reset_preparation() -> None:
    """Forget what has been attempted. For tests and for a forced rescan."""
    with _prep_lock:
        _prep_seen.clear()
        del _prep_queue[:]
        _prep_state.update({"total": 0, "done": 0, "failed": 0, "current": "",
                            "finished_at": 0})


def purge_orphans(known_paths) -> int:
    """Remove cached covers that no longer belong to anything.

    *known_paths* is every comic the library currently holds. Each existing
    one is turned into its cache key, and every cache file whose name is not
    one of those keys goes -- which covers three cases at once: the source was
    deleted, the source left the library, and the source was edited (its mtime
    changed, so its old key is no longer produced by anything).

    Note that this trusts the caller: an empty *known_paths* means an empty
    library and empties the cache. That is deliberate and harmless -- covers
    are derived data and regenerate on the next visit -- but it does mean this
    must be called with a complete list, never with a partial scan.
    """
    keep = set()
    for candidate in known_paths or ():
        try:
            path = Path(candidate)
            if path.is_file():
                keep.add(cache_key(path))
        except OSError:
            continue

    root = _cache_root()
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file():
            continue
        # "<key>.jpg" and a leftover "<key>.1234-5678.part" both reduce to the
        # key, so an interrupted write is swept up by the same pass. Split
        # rather than Path.stem: stem strips one suffix and would leave
        # "<key>.1234-5678". A key is hexadecimal and contains no dot, so
        # everything before the first one is exactly the key.
        if entry.name.split(".", 1)[0] in keep:
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("[Comics] Removed %s orphaned cover(s)", removed)
    return removed


def cache_stats() -> dict:
    """How much room the cover cache is taking up: ``{"files": n, "bytes": n}``.

    Purely informational -- it is what the settings page shows next to the
    button that empties this cache, so that pressing it is an informed
    decision rather than a leap in the dark. Flat directory, one stat() per
    entry, and it never raises: a cache directory that cannot be listed reads
    as empty, which is also what it is worth to the caller.
    """
    files = 0
    total = 0
    try:
        entries = list(_cache_root().iterdir())
    except OSError:
        return {"files": 0, "bytes": 0}
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            total += entry.stat().st_size
            files += 1
        except OSError:
            continue
    return {"files": files, "bytes": total}


def cleanup_covers(max_age_days: int = 180) -> int:
    """Drop covers nothing has asked for in a long time.

    Much more patient than the conversion cache: a cover is a couple of
    hundred kilobytes and rebuilding it means opening the archive again, so
    the cheap thing is to keep it. This exists mainly so a library that was
    unmounted for half a year does not leave its covers behind forever.
    """
    root = _cache_root()
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_atime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("[Comics] Removed %s stale cover(s)", removed)
    return removed
