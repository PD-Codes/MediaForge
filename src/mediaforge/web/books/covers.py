"""The cover of a book: the picture inside the file, cached on disk.

A shelf is a wall of covers, and until now the book shelf had almost none. It
showed a cover only when a ``cover.jpg`` happened to lie next to the file --
which is a Calibre habit, not a rule -- and served that file untouched, so a
three-megabyte press shot went over the wire in full for a card 160 pixels
wide. Everything else got a grey placeholder forever, even though an EPUB
carries its cover inside it and always has.

This module is the missing half, and it is deliberately the thin half: the
cache, the downscaling and the background worker are :mod:`web.covercache`,
shared with the comic shelf. What lives here is the one part that is genuinely
about books -- where the picture hides in each format.

Four sources, in this order:

  * **EPUB** -- the cover named by the package document. Two spellings exist
    and both are in the wild: EPUB 3's ``properties="cover-image"`` on a
    manifest item, and EPUB 2's ``<meta name="cover" content="<id>">``. Failing
    both, an image in the manifest whose href looks like a cover. All of it is
    one zip member read out of a file that is already a zip -- cheap enough to
    do for a whole library.
  * **MOBI/AZW3/AZW** -- through the EPUB that books/convert.py already
    produces. Same shape as the comic shelf's RAR handling: the shelf route
    never starts that conversion, the background worker does, and the cover
    appears on a later request.
  * **A sidecar image** (``cover.jpg`` and friends next to the file) -- which
    is what the shelf used before, now routed through the cache so it is
    downscaled once instead of sent at full size on every card.
  * **PDF** -- nothing. Rendering page one needs a rasteriser that is not a
    dependency here, and the reader draws it in the browser anyway (pdf.js).
    A PDF with a sidecar image still gets that; a PDF without one keeps its
    placeholder. Same call the comic shelf makes, for the same reason.

NOTHING HERE EVER WRITES TO A BOOK. Every path is opened read-only, and the
only thing written is the cache.
"""
from __future__ import annotations

import posixpath
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from ...logger import get_logger
from ..covercache import CoverCache, PrepareWorker
from ..media_types import BOOK_CONVERTIBLE_EXTS, BOOK_COVER_EXTS
from . import convert

logger = get_logger(__name__)

# v1: the first version of this cache. Bump when what gets STORED changes.
_CACHE = CoverCache(
    subdir="book_covers",
    version="v1",
    allowed_exts=BOOK_COVER_EXTS | {".gif", ".bmp", ".avif"},
    log_prefix="[Books]",
)

cache_key = _CACHE.cache_key
has_cover = _CACHE.has_cover
cover_mimetype = _CACHE.mimetype
purge_orphans = _CACHE.purge_orphans
cache_stats = _CACHE.stats
cleanup_covers = _CACHE.cleanup

# Sidecar images that count as "the cover of the book next to me". Same list
# the scanner uses; kept in sync by importing the same constant would be nicer,
# but the scanner's is a tuple of exact names and this is an ordered lookup.
_SIDECAR_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp")

# An EPUB is a zip, and a zip is a list of names an untrusted file controls.
# Two ceilings, both about memory rather than about pictures: one member, and
# the manifest we parse to find it.
_MAX_OPF_BYTES = 512 * 1024
_MAX_IMAGE_BYTES = 24 * 1024 * 1024

# Namespaces, because OPF is XML and every file spells its prefixes its own way.
_NS_OPF = "http://www.idpf.org/2007/opf"
_NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"

_IMAGE_EXTS = tuple(sorted(BOOK_COVER_EXTS | {".gif", ".bmp", ".avif"}))


def _tag(name: str) -> str:
    """Local name of an XML tag, prefix and namespace stripped."""
    return name.rsplit("}", 1)[-1].lower()


def _read_member(zf: zipfile.ZipFile, name: str, limit: int):
    """One member of a zip, size-capped. None when it is missing or too big.

    The declared size is checked BEFORE reading: trusting the read to stop at
    the limit would still let a zip bomb decompress into memory first.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    try:
        with zf.open(info) as fh:
            return fh.read(limit + 1)[:limit]
    except (OSError, zipfile.BadZipFile, RuntimeError):
        # RuntimeError is what zipfile raises for an encrypted member.
        return None


def _opf_name(zf: zipfile.ZipFile) -> str:
    """Path of the package document, per META-INF/container.xml.

    Falling back to a search rather than to a fixed name: "content.opf" in the
    root is a convention, not a requirement, and the container file is the only
    thing the spec actually guarantees.
    """
    raw = _read_member(zf, "META-INF/container.xml", _MAX_OPF_BYTES)
    if raw:
        try:
            root = ElementTree.fromstring(raw)
            for element in root.iter():
                if _tag(element.tag) == "rootfile":
                    full = element.get("full-path") or ""
                    if full:
                        return full
        except ElementTree.ParseError:
            pass
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name
    return ""


def _resolve_href(opf_name: str, href: str) -> str:
    """A manifest href, resolved against the package document's directory.

    Hrefs are relative to the OPF, which is usually in a subdirectory
    ("OEBPS/content.opf" + "images/cover.jpg" = "OEBPS/images/cover.jpg").
    ``normpath`` also collapses the "../" that legitimately appears in some
    files -- and, because the result is only ever looked up in the zip's own
    name list, a path that escapes simply does not match anything.
    """
    href = (href or "").split("#", 1)[0].split("?", 1)[0]
    if not href:
        return ""
    base = posixpath.dirname(opf_name)
    return posixpath.normpath(posixpath.join(base, href)) if base else href


def epub_cover(path):
    """``(member name, bytes)`` for an EPUB's cover, or ``("", b"")``."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _opf_name(zf)
            if not opf_name:
                return "", b""
            raw = _read_member(zf, opf_name, _MAX_OPF_BYTES)
            if not raw:
                return "", b""
            try:
                root = ElementTree.fromstring(raw)
            except ElementTree.ParseError:
                logger.debug("[Books] Unparseable package document in %s", path.name)
                return "", b""

            items = {}
            by_property = ""
            cover_id = ""
            for element in root.iter():
                tag = _tag(element.tag)
                if tag == "item":
                    item_id = element.get("id") or ""
                    href = element.get("href") or ""
                    if item_id and href:
                        items[item_id] = href
                    # EPUB 3. The attribute is a space-separated list.
                    props = (element.get("properties") or "").split()
                    if "cover-image" in props and href:
                        by_property = href
                elif tag == "meta" and (element.get("name") or "").lower() == "cover":
                    # EPUB 2.
                    cover_id = element.get("content") or ""

            href = by_property or items.get(cover_id, "")
            if not href:
                # Neither spelling. Plenty of real files declare no cover at
                # all but ship one under an obvious name -- worth one guess
                # before giving up, and cheap, because the manifest is already
                # parsed.
                for candidate in items.values():
                    name = posixpath.basename(candidate).lower()
                    if "cover" in name and name.endswith(_IMAGE_EXTS):
                        href = candidate
                        break
            if not href:
                return "", b""

            member = _resolve_href(opf_name, href)
            if not member.lower().endswith(_IMAGE_EXTS):
                return "", b""
            data = _read_member(zf, member, _MAX_IMAGE_BYTES)
            if not data:
                return "", b""
            return member, data
    except (OSError, zipfile.BadZipFile):
        # A truncated download, a DRM-wrapped file, something that is not
        # really a zip. All of it means "no cover", none of it is an error.
        logger.debug("[Books] Could not read a cover out of %s", path.name, exc_info=True)
        return "", b""


def sidecar_cover(path):
    """``(name, bytes)`` for a ``cover.jpg`` next to *path*, or ``("", b"")``.

    Read into memory rather than pointed at, because it goes through the same
    downscaling as everything else -- which is the point: this is where the
    three-megabyte Calibre cover was coming from.
    """
    path = Path(path)
    for name in _SIDECAR_NAMES:
        candidate = path.parent / name
        try:
            if not candidate.is_file():
                continue
            if candidate.stat().st_size > _MAX_IMAGE_BYTES:
                continue
            return name, candidate.read_bytes()
        except OSError:
            continue
    return "", b""


def cover_path(src, start_conversion: bool = True):
    """The cached cover for *src*, producing it once if needed. None if there
    is none to have.

    None is a normal answer, not a failure: a PDF, a DRM-wrapped AZW, an EPUB
    that ships no image. The shelf draws its placeholder and moves on.

    *start_conversion* is what licenses this call to do real work on a
    MOBI/AZW3: with it true a conversion may be started, in which case this
    call still returns None and the cover appears on a later request. Pass
    False from anything that walks a whole library, so opening the books page
    does not queue two hundred conversions -- the background worker below is
    what fills those in.
    """
    src = Path(src)
    try:
        key = cache_key(src)
    except OSError:
        return None

    hit = _CACHE.cached(key)
    if hit is not None:
        return hit

    ext = src.suffix.lower()
    name, data = "", b""

    if ext == ".epub":
        name, data = epub_cover(src)
    elif ext in BOOK_CONVERTIBLE_EXTS:
        # Only readable once books/convert.py has turned it into an EPUB.
        # Never blocked on here -- if it is not ready, there is no cover yet.
        status = convert.conversion_status(src) if start_conversion else _peek_conversion(src)
        if status.get("ready"):
            try:
                converted = convert.converted_path(status["key"])
            except (KeyError, ValueError):
                converted = None
            if converted is not None and converted.is_file():
                name, data = epub_cover(converted)

    if not data:
        # The sidecar is the fallback for every format, PDF included: it is
        # the one source that does not depend on opening the book at all.
        name, data = sidecar_cover(src)

    if not name or not data:
        return None
    return _CACHE.store_image(key, src, name, data)


def _peek_conversion(src: Path) -> dict:
    """Conversion state WITHOUT starting one.

    books/convert.py's conversion_status() has no "report only" flag -- its
    comic counterpart does (``start=False``) and the shelf route depends on
    that distinction, because a route that starts work when polled turns a
    page render into a job queue. Until the book side grows the same flag,
    this asks the cache directly instead.
    """
    try:
        key = convert.cache_key(src)
        path = convert.converted_path(key)
        return {"ready": path.is_file(), "key": key}
    except Exception:
        return {"ready": False}


# ---------------------------------------------------------------------------
# Background preparation
# ---------------------------------------------------------------------------
# Same job as the comic worker: the shelf route refuses to start conversions,
# so something has to, or a shelf of MOBI files stays blank forever. Unlike
# comics, most of a book library is EPUB and needs no conversion at all -- for
# those this is just "open the zip once", and the queue drains fast.

_PREP_POLL_SECONDS = 1.5
_PREP_PER_FILE_TIMEOUT = 900


def _prepare_one(src: Path) -> bool:
    """Make sure one book has a cached cover. True if it now has one."""
    if has_cover(src):
        return True

    if cover_path(src, start_conversion=True) is not None:
        return True

    if src.suffix.lower() not in BOOK_CONVERTIBLE_EXTS:
        # An EPUB with no image, a PDF, a sidecar-less anything: nothing a
        # conversion could change.
        return False

    deadline = time.monotonic() + _PREP_PER_FILE_TIMEOUT
    while time.monotonic() < deadline:
        status = _peek_conversion(src)
        if status.get("ready"):
            return cover_path(src, start_conversion=False) is not None
        # A conversion that has already given up will never turn ready, and a
        # DRM-protected book never converts at all. Waiting out the full
        # per-file timeout for either meant a shelf of Kindle purchases held
        # the worker for 15 minutes each, on every scan.
        reason = convert.failure_reason(src)
        if reason:
            if reason == "drm":
                logger.info("[Books] %s is DRM-protected -- no cover", src.name)
            return False
        time.sleep(_PREP_POLL_SECONDS)
    logger.info("[Books] Gave up waiting for %s to be prepared", src.name)
    return False


_WORKER = PrepareWorker("book-covers", lambda src: _prepare_one(src), "[Books]")

preparation_status = _WORKER.status
prepare_async = _WORKER.submit
reset_preparation = _WORKER.reset
