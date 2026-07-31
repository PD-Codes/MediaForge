"""Reading ComicInfo.xml, the sidecar that lives inside a comic archive.

ComicInfo.xml is ComicRack's format and became the de-facto standard: almost
every CBZ that has been through a comic manager carries one, at the archive
root. It is the best metadata available without guessing, because it states
the series and the issue number as separate fields -- which is exactly the
thing a filename smears together and identity.py then has to pull apart again.

Same threat model and the same defence as books/opf.py: ElementTree does not
resolve external entities, so the remaining XML risk is entity expansion, and
a size cap answers that. A ComicInfo.xml is a couple of kilobytes; anything
larger is not one.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree

from ...logger import get_logger
from . import archive

logger = get_logger(__name__)

# Generous by two orders of magnitude for a real file, small enough that a
# billion-laughs payload never gets read in the first place.
_MAX_COMICINFO_BYTES = 256 * 1024

# The member name is matched case-insensitively: the spec says "ComicInfo.xml",
# archives in the wild also carry "comicinfo.xml" and "ComicInfo.XML".
_COMICINFO_NAME = "comicinfo.xml"

# Credits are stored as one comma-separated string per role.
_CREDIT_SPLIT_RE = re.compile(r"\s*[,;]\s*")

# Fields copied straight through as text.
_TEXT_FIELDS = {
    "Series": "series",
    "Title": "title",
    "Number": "number",
    "Volume": "volume",
    "Publisher": "publisher",
    "Imprint": "imprint",
    "Genre": "genre",
    "LanguageISO": "language",
    "Web": "web",
    "Summary": "summary",
}

# Credit roles, in the order a cover normally lists them.
_CREDIT_FIELDS = {
    "Writer": "writers",
    "Penciller": "pencillers",
    "Inker": "inkers",
    "Colorist": "colorists",
    "Letterer": "letterers",
    "CoverArtist": "cover_artists",
    "Editor": "editors",
}

_LIST_FIELDS = {
    "Characters": "characters",
    "Teams": "teams",
    "Locations": "locations",
}


def _text(node):
    return (node.text or "").strip() if node is not None else ""


def _clean(value: str, limit: int = 400) -> str:
    """Collapse whitespace and cap length.

    These strings come out of a file the user downloaded and end up in the
    library cache, which is JSON in SQLite and then a row in a grid. A
    ComicInfo.xml with a 2 MB Summary is not malicious, just badly generated,
    but it would bloat every cache read for one card nobody expands.
    """
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def parse_bytes(raw: bytes) -> dict:
    """Parse ComicInfo.xml content. Returns {} for anything unusable."""
    if not raw or len(raw) > _MAX_COMICINFO_BYTES:
        return {}
    try:
        root = ElementTree.fromstring(raw)
    except Exception:
        # A malformed sidecar is common and means "no metadata", not an error:
        # the scanner falls back to the filename and the issue still shows up.
        logger.debug("[Comics] Unparsable ComicInfo.xml", exc_info=True)
        return {}

    out = {}
    for tag, key in _TEXT_FIELDS.items():
        val = _clean(_text(root.find(tag)), 4000 if key == "summary" else 400)
        if val:
            out[key] = val

    for tag, key in _CREDIT_FIELDS.items():
        names = [_clean(n, 120) for n in _CREDIT_SPLIT_RE.split(_text(root.find(tag))) if n.strip()]
        if names:
            out[key] = names[:20]

    for tag, key in _LIST_FIELDS.items():
        items = [_clean(n, 120) for n in _CREDIT_SPLIT_RE.split(_text(root.find(tag))) if n.strip()]
        if items:
            out[key] = items[:40]

    # Year/Month/Day are separate elements; only the year is worth keeping.
    year = _text(root.find("Year"))
    if year.isdigit() and 1000 <= int(year) <= 2999:
        out["year"] = int(year)

    count = _text(root.find("Count"))
    if count.lstrip("-").isdigit() and int(count) > 0:
        out["issue_count"] = int(count)

    pages = _text(root.find("PageCount"))
    if pages.isdigit() and int(pages) > 0:
        out["page_count"] = int(pages)

    # "Manga" is a yes/no that also encodes reading direction:
    # "YesAndRightToLeft" means the reader should page right-to-left.
    manga = _text(root.find("Manga"))
    if manga:
        out["manga"] = manga.lower().startswith("yes")
        if "righttoleft" in manga.lower().replace(" ", ""):
            out["rtl"] = True

    return out


def read(path, fmt=None) -> dict:
    """The ComicInfo.xml inside `path`, parsed. {} if there is none.

    Only looks at native containers: a RAR or ACE has to be converted before
    anything can be read out of it, and doing that during a scan would turn
    indexing a library into unpacking it. The scanner reads the sidecar of a
    converted archive on the next pass instead.
    """
    fmt = fmt or archive.sniff(path)
    if not archive.is_native(fmt):
        return {}
    raw = _read_member(path, fmt)
    return parse_bytes(raw) if raw else {}


def _read_member(path, fmt):
    """The raw ComicInfo.xml bytes, found case-insensitively at any depth."""
    try:
        names = _member_names(path, fmt)
    except Exception:
        logger.debug("[Comics] Cannot list %s for ComicInfo", path, exc_info=True)
        return None

    # Prefer the one at the archive root; some archives carry a per-page copy
    # in a subfolder, and the root one is the issue's own.
    candidates = [n for n in names if n.rsplit("/", 1)[-1].lower() == _COMICINFO_NAME]
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n.count("/"), len(n)))
    return _read_raw(path, candidates[0], fmt)


def _member_names(path, fmt):
    import tarfile
    import zipfile
    if fmt == archive.FMT_ZIP:
        with zipfile.ZipFile(path) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()]
    if fmt == archive.FMT_TAR:
        with tarfile.open(path) as tf:
            return [m.name for m in tf.getmembers() if m.isfile()]
    if fmt == archive.FMT_7Z:
        py7zr = archive._py7zr()
        if py7zr is None:
            return []
        with py7zr.SevenZipFile(path, mode="r") as zf:
            return list(zf.getnames())
    return []


def _read_raw(path, name, fmt):
    """Bytes of one member. Deliberately NOT archive.read_page(): that one
    only returns page images, and this is the one member that is not one."""
    import tarfile
    import zipfile
    try:
        if fmt == archive.FMT_ZIP:
            with zipfile.ZipFile(path) as zf:
                info = zf.getinfo(name)
                if info.file_size > _MAX_COMICINFO_BYTES:
                    return None
                return zf.read(name)
        if fmt == archive.FMT_TAR:
            with tarfile.open(path) as tf:
                member = tf.getmember(name)
                if not member.isfile() or member.size > _MAX_COMICINFO_BYTES:
                    return None
                fh = tf.extractfile(member)
                return fh.read(_MAX_COMICINFO_BYTES) if fh else None
        if fmt == archive.FMT_7Z:
            return archive._read_7z_member(path, name)
    except Exception:
        logger.debug("[Comics] Cannot read %s from %s", name, path, exc_info=True)
    return None
