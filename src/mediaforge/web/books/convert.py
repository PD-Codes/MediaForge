"""Turning MOBI/AZW3/AZW into something a browser can render.

No browser can display a Mobipocket file, and there is no JavaScript library
that changes that. The reader therefore asks the server for an EPUB and the
server makes one, once, and keeps it.

The conversion itself is the `mobi` package (a KindleUnpack derivative, pure
Python, GPL-3.0 -- compatible with MediaForge's own licence). It behaves
differently per input:

  * AZW3 / KF8  -> already an EPUB inside the wrapper; it comes straight out
  * MOBI / AZW  -> an unpacked folder with book.html, content.opf, toc.ncx and
                   an Images/ directory, which this module zips into an EPUB

Everything is cached by (path, mtime, size), the same identity the seek-preview
sprites use (transcoder._thumb_key): edit or replace the file and the cache
entry stops matching, so a stale conversion cannot outlive its source.

Nothing is ever written next to the user's book. The cache lives under the
config directory, which is also what makes this work when the library sits on
a read-only mount.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

from ...logger import get_logger
from ..media_types import BOOK_CONVERTIBLE_EXTS

logger = get_logger(__name__)

# At most two conversions at a time. Unpacking a 12 MB AZW3 is a fraction of a
# second, but a shelf-wide click storm should not be able to occupy every core.
_MAX_PARALLEL = 2

# Remember a failure for an hour: long enough that the client stops polling a
# book that cannot be converted, short enough that a transient problem (a NAS
# that was briefly away) is retried the same afternoon.
_FAIL_TTL = 3600.0

# A book is a few dozen megabytes. Anything beyond this is not a book, and
# unpacking it would be an easy way to fill the config volume.
_MAX_SOURCE_BYTES = 400 * 1024 * 1024
_MAX_UNPACKED_BYTES = 800 * 1024 * 1024
_MAX_ENTRIES = 20_000

# Bump whenever the conversion output changes. It is part of the cache key, so
# raising it retires every previously converted book instead of serving the old
# result forever -- the mistake that let a bad OPF survive a fix during
# development, because path, mtime and size had of course not changed.
_CONVERTER_VERSION = "v4"

_lock = threading.Lock()
_jobs: dict = {}      # key -> True while a conversion runs
_failures: dict = {}  # key -> timestamp of the failure


def _cache_root() -> Path:
    """Where conversions live: <config>/book_convert/<key>/book.epub.

    Imported lazily and with a fallback, the same way transcoder._cache_dir
    does it -- config is a package-level module (mediaforge.config, not
    mediaforge.web.config), and this module has to keep working when it is
    reached from a context that never built the app.
    """
    try:
        from ...config import MEDIAFORGE_CONFIG_DIR
        base = Path(MEDIAFORGE_CONFIG_DIR)
    except Exception:
        import tempfile
        base = Path(tempfile.gettempdir()) / "mediaforge"
    root = base / "book_convert"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_key(path: Path) -> str:
    """Identity of one conversion: path + mtime + size + format version.

    Including mtime and size is what makes a replaced file produce a different
    key instead of silently serving the previous book's text.
    """
    stat = path.stat()
    raw = "{}|{}|{}|{}".format(path.resolve(), int(stat.st_mtime), stat.st_size, _CONVERTER_VERSION)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def converted_path(key: str) -> Path:
    """Where the finished EPUB for *key* lives. Never trusts the key blindly."""
    import re
    if not re.fullmatch(r"[0-9a-f]{8,40}", key or ""):
        raise ValueError("invalid conversion key")
    target = (_cache_root() / key / "book.epub").resolve()
    # Second containment check: the regex already rules out traversal, but a
    # path that leaves the cache must never be served even if it did not.
    target.relative_to(_cache_root().resolve())
    return target


def _safe_extract_member(name: str, base: Path) -> Path:
    """Resolve an archive member against *base*, refusing to escape it."""
    target = (base / name).resolve()
    target.relative_to(base.resolve())  # raises ValueError on zip-slip
    return target


# The charset a Mobipocket file declares for itself, injected into the HTML by
# KindleUnpack: <meta http-equiv="content-type" content="text/html; charset=X">
_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""", re.IGNORECASE)

_TEXTUAL_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".opf", ".ncx", ".css"})

# What the declared name maps to, plus the two spellings that are not real
# codec names but appear in the wild.
_CODEC_ALIASES = {
    "cp1252": "cp1252", "windows-1252": "cp1252", "win-1252": "cp1252",
    "cp65001": "utf-8", "utf8": "utf-8", "utf-8": "utf-8",
    "latin1": "latin-1", "iso-8859-1": "latin-1", "iso8859-1": "latin-1",
}


def _normalize_encoding(folder: Path) -> None:
    """Re-encode the unpacked book to UTF-8.

    KindleUnpack writes the book's text **as raw bytes** and injects a meta tag
    naming the codepage the MOBI itself declared -- for anything written before
    roughly 2012 that is windows-1252, not UTF-8. epub.js then reads the file
    out of the zip as text, which assumes UTF-8, and every umlaut in the book
    turns into a different character: "Nähe" becomes "N麦", "übertragen"
    becomes "臈ertragen".

    Declaring the right charset would not help, because the reader never looks
    at the meta tag -- the decoding happens in JavaScript before the HTML is
    parsed. So the bytes themselves are converted here, once, and the meta tag
    is rewritten to match.
    """
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXTUAL_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue

        declared = ""
        found = _CHARSET_RE.search(raw[:2048])
        if found:
            declared = found.group(1).decode("ascii", "ignore").strip().lower()

        try:
            text = raw.decode("utf-8")
            # Already UTF-8. Only the label can still be wrong, and a wrong
            # label on correct bytes is what makes the NEXT tool guess wrong.
            if declared and _CODEC_ALIASES.get(declared, declared) != "utf-8":
                path.write_bytes(_rewrite_charset(raw))
            continue
        except UnicodeDecodeError:
            pass

        codec = _CODEC_ALIASES.get(declared, declared) or "cp1252"
        for candidate in (codec, "cp1252", "latin-1"):
            try:
                text = raw.decode(candidate)
                break
            except (UnicodeDecodeError, LookupError):
                text = None
        if text is None:
            # latin-1 cannot fail, so reaching this means something stranger;
            # leave the file alone rather than writing a worse version of it.
            continue
        try:
            path.write_bytes(_rewrite_charset(text.encode("utf-8")))
            logger.debug("[Books] Re-encoded %s from %s to UTF-8", path.name, codec)
        except OSError:
            continue


def _rewrite_charset(raw: bytes) -> bytes:
    """Point any charset declaration in the first 2 KB at UTF-8."""
    head, tail = raw[:2048], raw[2048:]
    head = _CHARSET_RE.sub(b"charset=utf-8", head, count=1)
    return head + tail


def _fix_opf(opf_path: Path) -> None:
    """Make the OPF the `mobi` package writes a valid EPUB 2 package document.

    What is missing is the navigation document: the spine has no ``toc``
    attribute and the manifest has no entry for the toc.ncx sitting right next
    to it, so a reader looking for it dereferences nothing (epub.js throws
    "Cannot read properties of undefined") and there is no chapter list.

    What is deliberately NOT changed is the ``text/html`` media type of the
    content document. EPUB nominally wants ``application/xhtml+xml``, and
    relabelling it is a one-line change -- but the HTML that comes out of a
    Mobipocket file is HTML 4: unquoted attributes (``filepos=0000814257``),
    named entities XHTML does not define (``&nbsp;``), and Mobipocket's own
    ``<mbp:pagebreak>`` elements with no namespace declared. Under the XHTML
    media type the browser parses it as XML and stops at the first of those
    with "Entity 'nbsp' not defined", showing a parser error instead of the
    book. Left as text/html the parser is lenient and the book renders.

    Rewritten with plain string surgery rather than an XML round-trip on
    purpose: re-serialising would reorder attributes and drop the doctype for
    no benefit, and any failure here has to leave the original file untouched.
    """
    try:
        raw = opf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    original = raw

    has_ncx_file = (opf_path.parent / "toc.ncx").is_file()
    if has_ncx_file and "application/x-dtbncx+xml" not in raw:
        raw = raw.replace(
            "<manifest>",
            '<manifest>\n<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            1,
        )
    if has_ncx_file:
        raw = re.sub(r"<spine(?![^>]*\btoc=)([^>]*)>", r'<spine toc="ncx"\1>', raw, count=1)
    if raw != original:
        try:
            opf_path.write_text(raw, encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Making the markup survive an XML parser
# ---------------------------------------------------------------------------
# An EPUB content document is declared as ``application/xhtml+xml``, and that
# label is not decoration: the reader hands the file to an XML parser, which is
# a parser with no error recovery at all. One `&nbsp;` -- an entity HTML defines
# and XML does not -- and the reader shows
#
#     error on line 11 at column 94: Entity 'nbsp' not defined
#
# instead of the book. Kindle files are full of them, because the KF8 side of an
# AZW3 was authored as HTML and only ever read by a lenient parser.
#
# Two things are wrong in practice and both are repairable without touching the
# text itself: named entities XML never defined, and namespace prefixes
# (`<mbp:pagebreak>`) used without a matching xmlns declaration. Everything else
# -- unquoted attributes, unclosed tags -- is not worth guessing at, so anything
# that still fails to parse afterwards has its media type dropped to
# ``text/html`` instead: the same escape hatch the MOBI branch relies on, where
# the browser's lenient parser renders the book fine.

# Every ampersand, with whatever reference follows it captured if there is one.
# Matching the bare ones too is deliberate: `AT&T` is as fatal to an XML parser
# as `&nbsp;` is, and both come out of the same authoring tools.
_ENTITY_RE = re.compile(r"&(#\d{1,7};|#[xX][0-9a-fA-F]{1,6};|[A-Za-z][A-Za-z0-9]{0,31};)?")

# The five XML predefines. Everything else has to become a numeric reference.
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})

_MARKUP_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".opf", ".ncx", ".svg"})

# Prefixes seen in Kindle output, mapped to the namespace they are meant to be.
# An unknown prefix gets a private URI: the point is only that the document
# parses, and a made-up namespace nobody queries is harmless.
_KNOWN_NS = {
    "mbp": "http://www.mobipocket.com/ns/mbp",
    "idx": "http://www.mobipocket.com/ns/indx",
    "epub": "http://www.idpf.org/2007/ops",
    "ops": "http://www.idpf.org/2007/ops",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "m": "http://www.w3.org/1998/Math/MathML",
    "mml": "http://www.w3.org/1998/Math/MathML",
    "math": "http://www.w3.org/1998/Math/MathML",
    "calibre": "http://calibre.kovidgoyal.net/2009/metadata",
}

# xml/xmlns are bound by the specification and must never be redeclared.
_RESERVED_PREFIXES = frozenset({"xml", "xmlns"})

_TAG_PREFIX_RE = re.compile(r"</?([A-Za-z][\w.-]*):[A-Za-z]")
_ATTR_PREFIX_RE = re.compile(r"[\s\"']([A-Za-z][\w.-]*):[\w.-]+\s*=")
_ROOT_TAG_RE = re.compile(r"<(html|svg|package|ncx)\b[^>]*>", re.IGNORECASE)


def _numeric_entities(text: str) -> str:
    """Replace named entities with numeric ones an XML parser accepts."""
    import html.entities

    def replace(match):
        ref = match.group(1)
        if not ref:
            return "&amp;"          # a bare ampersand: "AT&T"
        if ref.startswith("#"):
            return match.group(0)   # already numeric, nothing to do
        name = ref[:-1]
        if name in _XML_ENTITIES:
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        if codepoint is None:
            # Not an entity at all, just an ampersand followed by a word and a
            # semicolon. Escaping it is what the author meant anyway.
            return "&amp;{}".format(ref)
        return "&#{};".format(codepoint)

    return _ENTITY_RE.sub(replace, text)


def _declare_namespaces(text: str) -> str:
    """Add an xmlns for every prefix the document uses but never declared."""
    used = set(_TAG_PREFIX_RE.findall(text)) | set(_ATTR_PREFIX_RE.findall(text))
    missing = [
        prefix for prefix in sorted(used)
        if prefix not in _RESERVED_PREFIXES and 'xmlns:{}='.format(prefix) not in text
    ]
    if not missing:
        return text
    root = _ROOT_TAG_RE.search(text)
    if not root:
        return text
    declarations = "".join(
        ' xmlns:{}="{}"'.format(p, _KNOWN_NS.get(p, "urn:x-mediaforge:ns:{}".format(p)))
        for p in missing
    )
    tag = root.group(0)
    patched = tag[:-1].rstrip("/") + declarations + ("/>" if tag.endswith("/>") else ">")
    return text[:root.start()] + patched + text[root.end():]


def _parses_as_xml(text: str) -> bool:
    from xml.etree import ElementTree

    try:
        ElementTree.fromstring(text)
        return True
    except Exception:
        return False


def _sanitize_markup(folder: Path, opf_path: Path = None) -> None:
    """Repair every markup file in *folder*, downgrading what cannot be repaired.

    Runs after :func:`_normalize_encoding`, so everything on disk is already
    UTF-8 and can simply be read as text.
    """
    unfixable: set = set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _MARKUP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        repaired = _declare_namespaces(_numeric_entities(text))
        if repaired != text:
            try:
                path.write_text(repaired, encoding="utf-8")
            except OSError:
                continue
        if path.suffix.lower() in (".html", ".htm", ".xhtml") and not _parses_as_xml(repaired):
            unfixable.add(path.name)
    if unfixable and opf_path is not None:
        _downgrade_media_types(opf_path, unfixable)


_ITEM_RE = re.compile(r"<item\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.IGNORECASE)


def _downgrade_media_types(opf_path: Path, filenames: set) -> None:
    """Relabel content documents that still do not parse as XML.

    ``text/html`` sends the file through the browser's HTML parser, which
    recovers from anything. It costs nothing a reader can see -- epub.js
    renders both the same way -- and it is the difference between a book and a
    parser error page.
    """
    try:
        raw = opf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    def rewrite(match):
        item = match.group(0)
        href = _HREF_RE.search(item)
        if not href or os.path.basename(href.group(1)) not in filenames:
            return item
        return item.replace("application/xhtml+xml", "text/html")

    patched = _ITEM_RE.sub(rewrite, raw)
    if patched != raw:
        try:
            opf_path.write_text(patched, encoding="utf-8")
            logger.info("[Books] Relabelled %s unparsable document(s) as text/html", len(filenames))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

_CONTAINER_ROOT_RE = re.compile(r'full-path\s*=\s*"([^"]+)"', re.IGNORECASE)


def _unzip_epub(src: Path, dest: Path) -> None:
    """Unpack an EPUB, refusing anything that tries to escape *dest*."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError("book has implausibly many entries")
        for info in infos:
            if info.is_dir():
                continue
            total += info.file_size
            if total > _MAX_UNPACKED_BYTES:
                raise ValueError("book unpacks to an implausible size")
            target = _safe_extract_member(info.filename, dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)


def _epub_opf_path(folder: Path) -> Path:
    """The package document of an unpacked EPUB, per META-INF/container.xml."""
    container = folder / "META-INF" / "container.xml"
    try:
        found = _CONTAINER_ROOT_RE.search(container.read_text(encoding="utf-8", errors="replace"))
        if found:
            candidate = _safe_extract_member(found.group(1), folder)
            if candidate.is_file():
                return candidate
    except (OSError, ValueError):
        pass
    for candidate in sorted(folder.rglob("*.opf")):
        return candidate
    return None


def _rezip_epub(folder: Path, out: Path) -> None:
    """Repackage an unpacked EPUB, keeping its own META-INF intact."""
    tmp_out = out.with_suffix(".part")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        for root, _dirs, files in os.walk(folder):
            for name in sorted(files):
                src = Path(root) / name
                rel = str(src.relative_to(folder)).replace(os.sep, "/")
                if rel == "mimetype":
                    continue  # already written, uncompressed and first
                zf.write(src, rel)
    tmp_out.replace(out)


def _zip_folder_as_epub(folder: Path, opf_rel: str, out: Path) -> None:
    """Package an unpacked Mobipocket folder as a valid EPUB.

    The `mobi` package already writes a content.opf and a toc.ncx next to the
    HTML, so the only things missing are the two files that make a zip an
    EPUB: an uncompressed `mimetype` first entry, and META-INF/container.xml
    pointing at the OPF.
    """
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="{}" media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>\n"
    ).format(opf_rel)

    total = 0
    count = 0
    tmp_out = out.with_suffix(".part")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf:
        # The mimetype entry has to be first and stored uncompressed -- that is
        # what lets a reader sniff the format without unzipping.
        zf.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED
        )
        zf.writestr("META-INF/container.xml", container)
        for root, _dirs, files in os.walk(folder):
            for name in sorted(files):
                src = Path(root) / name
                try:
                    size = src.stat().st_size
                except OSError:
                    continue
                total += size
                count += 1
                if total > _MAX_UNPACKED_BYTES or count > _MAX_ENTRIES:
                    raise ValueError("converted book is implausibly large")
                zf.write(src, str(src.relative_to(folder.parent)).replace(os.sep, "/"))
    tmp_out.replace(out)


def _convert(src: Path, key: str) -> None:
    """Do one conversion. Runs on a worker thread; never raises to the caller."""
    import mobi

    out_dir = _cache_root() / key
    work = out_dir / "work"
    out = out_dir / "book.epub"
    tmpdir = None
    chatter = io.StringIO()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # KindleUnpack narrates to stdout -- a banner, per-record progress and
        # warnings like "Bad key, size, value combination detected in EXTH".
        # None of it is actionable and all of it would land in the app log (and
        # in the console of anyone running MediaForge in a terminal), so it is
        # captured and only kept if the conversion actually fails.
        with contextlib.redirect_stdout(chatter):
            tmpdir, produced = mobi.extract(str(src))
        produced_path = Path(produced)
        if produced_path.suffix.lower() == ".epub":
            # AZW3/KF8: already an EPUB -- but "already an EPUB" only means the
            # container is right. The content documents inside are Kindle HTML
            # carrying an XHTML label, so handing the file straight to the
            # reader produced "Entity 'nbsp' not defined" instead of a book.
            # Unpack, repair, repack.
            work.mkdir(parents=True, exist_ok=True)
            staged = work / "epub"
            _unzip_epub(produced_path, staged)
            _normalize_encoding(staged)
            _sanitize_markup(staged, _epub_opf_path(staged))
            _rezip_epub(staged, out)
        else:
            # MOBI/AZW: an unpacked folder. Its parent is the "mobi7" directory
            # that holds book.html, content.opf, toc.ncx and Images/.
            folder = produced_path.parent
            opf = folder / "content.opf"
            if not opf.is_file():
                raise ValueError("no content.opf in the unpacked book")
            work.mkdir(parents=True, exist_ok=True)
            staged = work / folder.name
            shutil.copytree(folder, staged, dirs_exist_ok=True)
            _normalize_encoding(staged)
            _fix_opf(staged / "content.opf")
            _sanitize_markup(staged, staged / "content.opf")
            _zip_folder_as_epub(staged, "{}/content.opf".format(folder.name), out)
        (out_dir / "done.json").write_text(
            json.dumps({"key": key, "source": str(src), "created": int(time.time())}),
            encoding="utf-8",
        )
        logger.info("[Books] Converted %s -> EPUB (%s)", src.name, key)
    except Exception:
        logger.exception("[Books] Conversion of %s failed", src)
        tail = (chatter.getvalue() or "").strip().splitlines()[-6:]
        if tail:
            logger.info("[Books] Converter output: %s", " | ".join(tail))
        with _lock:
            _failures[key] = time.time()
        shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        with _lock:
            _jobs.pop(key, None)


def conversion_status(src: Path) -> dict:
    """Ask for a book's EPUB, starting the conversion if it is not there yet.

    Returns one of:
        {"ready": True, "key": ...}
        {"pending": True}
        {"failed": True, "reason": ...}

    Shaped exactly like transcoder.thumbs_status(), and for the same reason:
    the client polls, and a failure has to be a terminal answer rather than an
    endless spinner.
    """
    suffix = src.suffix.lower()
    if suffix not in BOOK_CONVERTIBLE_EXTS:
        return {"failed": True, "reason": "unsupported"}
    try:
        if src.stat().st_size > _MAX_SOURCE_BYTES:
            return {"failed": True, "reason": "too_large"}
        key = cache_key(src)
    except OSError:
        return {"failed": True, "reason": "unreadable"}

    if (_cache_root() / key / "done.json").is_file():
        return {"ready": True, "key": key}

    with _lock:
        failed_at = _failures.get(key)
        if failed_at and time.time() - failed_at < _FAIL_TTL:
            return {"failed": True, "reason": "conversion_failed"}
        if key in _jobs:
            return {"pending": True}
        if len(_jobs) >= _MAX_PARALLEL:
            # Not an error: the client keeps polling and gets a slot shortly.
            return {"pending": True, "queued": True}
        _failures.pop(key, None)
        _jobs[key] = True

    threading.Thread(target=_convert, args=(src, key), daemon=True, name="book-convert").start()
    return {"pending": True}


def cleanup_converted(max_age_days: int = 30) -> int:
    """Drop conversions nobody has opened in a while.

    Access time rather than creation time, so a book you re-read every month
    survives while a one-off preview does not. Called from the same daily
    worker that prunes the image cache.
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
            if not entry.is_dir():
                continue
            book = entry / "book.epub"
            when = book.stat().st_atime if book.is_file() else entry.stat().st_mtime
            if when < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("[Books] Removed %s stale conversion(s)", removed)
    return removed
