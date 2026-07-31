"""What kind of container a comic file is, and how to get pages out of it.

Every other module in this package goes through here, for one reason: a comic
archive's EXTENSION IS NOT ITS FORMAT. "CBR" became the generic word for
"comic archive" the way "Kleenex" became the word for tissue, so a large share
of the .cbr files in circulation are plain ZIPs somebody renamed. Trusting the
extension would send those down the RAR path and demand an external unrar for
a file the standard library can open. Sniffing the first bytes instead makes
them work everywhere, for free -- and it is also the only honest answer for
the reverse case, a RAR named .cbz.

Native formats (ZIP/TAR/7z) are read in place. RAR and ACE cannot be, so
comics/convert.py repacks them into a cached CBZ once and everything
downstream reads that. PDF is not an archive at all and is handed to pdf.js in
the browser, exactly as an eBook PDF is.

Extraction safety: every member name that comes out of an archive is treated
as hostile. Archives are the classic path-traversal vector (zip slip, and
CVE-2018-20250 for ACE specifically), and these files come from wherever the
user got them. Nothing here ever writes a member to disk, and `read_page`
only ever returns bytes for a name that came out of `list_pages` -- which
rejects absolute paths, parent traversal and anything that is not a page
image.
"""
from pathlib import Path
import re
import tarfile
import zipfile

from ..media_types import COMIC_PAGE_EXTS
from ...logger import get_logger

logger = get_logger(__name__)


FMT_ZIP = "zip"
FMT_TAR = "tar"
FMT_7Z = "sevenzip"
FMT_RAR = "rar"
FMT_ACE = "ace"
FMT_PDF = "pdf"

# Containers this process can open without any external help. Everything else
# has to go through comics/convert.py first.
NATIVE_FORMATS = frozenset({FMT_ZIP, FMT_TAR, FMT_7Z})

# Not an archive: the browser reads it directly, there is nothing to unpack.
DIRECT_FORMATS = frozenset({FMT_PDF})

# What a user is shown when a format cannot be opened. Kept next to the
# detection so a new format cannot be added without deciding what to say.
FORMAT_LABELS = {
    FMT_ZIP: "CBZ", FMT_TAR: "CBT", FMT_7Z: "CB7",
    FMT_RAR: "CBR", FMT_ACE: "CBA", FMT_PDF: "PDF",
}


def sniff(path) -> str:
    """The container format of `path`, from its first bytes. "" if unknown.

    Reads 512 bytes: enough for every signature below, including TAR's, which
    sits at offset 257 rather than at the start.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return ""
    if len(head) < 4:
        return ""

    # ZIP: local file header, or an empty/spanned archive.
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return FMT_ZIP
    # RAR 4.x is "Rar!\x1a\x07\x00"; RAR 5 adds a byte. The shared prefix is enough.
    if head[:6] == b"Rar!\x1a\x07":
        return FMT_RAR
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return FMT_7Z
    if head[:5] == b"%PDF-":
        return FMT_PDF
    # ACE keeps its signature at offset 7.
    if head[7:14] == b"**ACE**":
        return FMT_ACE
    # TAR has no leading magic at all -- "ustar" lives in the first header block.
    if head[257:262] == b"ustar":
        return FMT_TAR
    return ""


def is_native(fmt) -> bool:
    """True if pages can be read straight out of this container."""
    return fmt in NATIVE_FORMATS


def _natural_key(name: str):
    """Sort key that orders page2 before page10.

    Comic archives are a directory listing, and a plain lexical sort puts
    "page10" between "page1" and "page2" -- which silently reorders the story.
    Splitting digit runs out and comparing them as numbers is the whole fix.
    """
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _is_page_name(name: str) -> bool:
    """True if an archive member is a page image we may serve.

    Rejects, in order: directory entries, anything that escapes the archive
    root, macOS resource forks, hidden files and any extension outside
    COMIC_PAGE_EXTS. The traversal checks are not theoretical -- a crafted
    archive naming a member "../../etc/passwd" is the standard zip-slip
    payload, and while nothing here writes members to disk, letting such a
    name reach a caller that later does would be handing over the loaded gun.
    """
    if not name or name.endswith("/") or name.endswith("\\"):
        return False
    norm = name.replace("\\", "/")
    if norm.startswith("/") or ".." in norm.split("/"):
        return False
    if re.match(r"^([A-Za-z]:)?[\\/]", norm):        # absolute, incl. "C:\..."
        return False
    base = norm.rsplit("/", 1)[-1]
    if not base or base.startswith("."):
        return False
    if norm.startswith("__MACOSX/"):
        return False
    return Path(base).suffix.lower() in COMIC_PAGE_EXTS


def list_pages(path, fmt=None) -> list:
    """Every page image in the archive, in reading order.

    Returns member names, not bytes -- the caller decides which pages it
    actually needs, so opening a 200 MB archive never means reading 200 MB.
    """
    fmt = fmt or sniff(path)
    try:
        if fmt == FMT_ZIP:
            with zipfile.ZipFile(path) as zf:
                names = [i.filename for i in zf.infolist() if not i.is_dir()]
        elif fmt == FMT_TAR:
            with tarfile.open(path) as tf:
                names = [m.name for m in tf.getmembers() if m.isfile()]
        elif fmt == FMT_7Z:
            py7zr = _py7zr()
            if py7zr is None:
                return []
            with py7zr.SevenZipFile(path, mode="r") as zf:
                names = [n for n in zf.getnames()]
        else:
            return []
    except Exception:
        # A truncated download, a password-protected archive, a container the
        # library rejects -- all of it means "no pages", never a 500. The
        # shelf shows the issue as unreadable and the rest of it keeps working.
        logger.debug("[Comics] Cannot list %s (%s)", path, fmt, exc_info=True)
        return []
    return sorted((n for n in names if _is_page_name(n)), key=_natural_key)


def read_page(path, name, fmt=None):
    """The bytes of one page, or None.

    `name` is re-validated rather than trusted: it makes the round trip
    through an HTTP request between list_pages() and here, so by the time it
    comes back it is user input again, whatever it was when it left.
    """
    if not _is_page_name(name):
        return None
    fmt = fmt or sniff(path)
    try:
        if fmt == FMT_ZIP:
            with zipfile.ZipFile(path) as zf:
                if name not in zf.namelist():
                    return None
                return zf.read(name)
        if fmt == FMT_TAR:
            with tarfile.open(path) as tf:
                member = tf.getmember(name)
                if not member.isfile():
                    return None
                fh = tf.extractfile(member)
                return fh.read() if fh else None
        if fmt == FMT_7Z:
            return _read_7z_member(path, name)
    except Exception:
        logger.debug("[Comics] Cannot read %s from %s", name, path, exc_info=True)
    return None


def page_count(path, fmt=None) -> int:
    """How many pages the archive holds."""
    return len(list_pages(path, fmt))


def first_page(path, fmt=None):
    """(name, bytes) of the first page -- the cover. (None, None) if there is none.

    "The cover is the first image" is the convention every comic reader uses,
    and with _natural_key above "first" means first in reading order rather
    than first in whatever order the archive happens to store its entries.
    """
    pages = list_pages(path, fmt)
    if not pages:
        return None, None
    return pages[0], read_page(path, pages[0], fmt)


# Largest single page py7zr may decode into memory. A page is an image; a
# "page" that is 64 MB is a malformed or hostile archive, and decoding it
# would be a memory-exhaustion vector on a route any logged-in user can call.
_SEVENZIP_MEMBER_LIMIT = 64 * 1024 * 1024


def _read_7z_member(path, name):
    """One member out of a .cb7, as bytes.

    py7zr's extraction API has changed shape across releases -- older versions
    had SevenZipFile.read(targets=...) returning a dict, current ones removed
    it in favour of extract(factory=...). Both are tried, newest first,
    because MediaForge does not pin py7zr (it is an optional extra) and a
    hard dependency on one spelling turns a routine `pip install -U` into
    "comics stopped opening".
    """
    py7zr = _py7zr()
    if py7zr is None:
        return None

    try:                                   # py7zr >= 1.0
        from py7zr.io import BytesIOFactory
        factory = BytesIOFactory(limit=_SEVENZIP_MEMBER_LIMIT)
        with py7zr.SevenZipFile(path, mode="r") as zf:
            zf.extract(targets=[name], factory=factory)
        buf = factory.get(name)
        if buf is None:
            return None
        buf.seek(0)
        return buf.read()
    except ImportError:
        pass

    with py7zr.SevenZipFile(path, mode="r") as zf:   # py7zr < 1.0
        got = zf.read(targets=[name]) or {}
        buf = got.get(name)
        if buf is None:
            return None
        buf.seek(0)
        return buf.read()


_PY7ZR = False          # False = not looked for yet, None = not installed


def _py7zr():
    """py7zr, or None. Imported lazily and remembered.

    Optional on purpose: it is pure Python and pip-installable, but a .cb7 is
    rare enough that a missing py7zr has to degrade to "this one issue cannot
    be opened" rather than stop the app from starting.
    """
    global _PY7ZR
    if _PY7ZR is False:
        try:
            import py7zr
            _PY7ZR = py7zr
        except ImportError:
            logger.info("[Comics] py7zr is not installed -- .cb7 files are listed but not readable")
            _PY7ZR = None
    return _PY7ZR
