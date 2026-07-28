"""Reading Calibre's ``metadata.opf`` sidecar.

An OPF is the small XML file Calibre writes next to every book it manages. It
is by far the best metadata source available without opening the book itself:
it is a few kilobytes, it sits in a directory we are stat-ing anyway, and it
carries the *untruncated* title, which the filename does not (Calibre shortens
the filename part of its layout, so "Warcraft 02 - Der Lord der Clans" ends up
on disk as "... der Clan").
"""
from __future__ import annotations

import re
from xml.etree import ElementTree

from ...logger import get_logger

logger = get_logger(__name__)

# An OPF is a handful of kilobytes. Anything larger is not an OPF, and refusing
# to read it keeps a malformed or hostile file from turning a library scan into
# an XML bomb. ElementTree does not resolve external entities, so the remaining
# risk is expansion, which a size cap addresses.
_MAX_OPF_BYTES = 512 * 1024

_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"

# ISBN-13 first: the alternation is ordered, and the 10-digit branch would
# otherwise match the first ten digits of a 13-digit number and silently
# truncate it ("9783833216718" -> "9783833216").
_ISBN_RE = re.compile(r"(\d{13}|\d{9}[\dXx])")


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def parse_opf(path) -> dict:
    """Return the metadata of one ``metadata.opf``.

    Never raises: a missing, oversized or malformed file yields ``{}`` and the
    caller falls back to the filename. A scan must not die on one bad sidecar
    somewhere in a library of thousands.
    """
    try:
        if path.stat().st_size > _MAX_OPF_BYTES:
            return {}
        root = ElementTree.parse(str(path)).getroot()
    except Exception:
        logger.debug("[Books] Unreadable OPF: %s", path, exc_info=True)
        return {}

    meta: dict = {}
    title = ""
    authors: list[str] = []
    for node in root.iter():
        tag = node.tag
        if tag == _DC + "title" and not title:
            title = _text(node)
        elif tag == _DC + "creator":
            role = node.get(_OPF + "role") or node.get("role") or ""
            # opf:role="aut" is the author; "edt"/"ill" are editor/illustrator
            # and must not end up as the author or the grouping key changes.
            if role in ("", "aut"):
                name = _text(node)
                if name and name not in authors:
                    authors.append(name)
        elif tag == _DC + "language" and "language" not in meta:
            meta["language"] = _text(node)[:16]
        elif tag == _DC + "date" and "published" not in meta:
            meta["published"] = _text(node)[:32]
        elif tag == _DC + "description" and "description" not in meta:
            meta["description"] = _text(node)
        elif tag == _DC + "publisher" and "publisher" not in meta:
            meta["publisher"] = _text(node)[:120]
        elif tag == _DC + "subject":
            value = _text(node)
            if value:
                meta.setdefault("tags", [])
                if value not in meta["tags"] and len(meta["tags"]) < 20:
                    meta["tags"].append(value)
        elif tag == _DC + "identifier":
            scheme = (node.get(_OPF + "scheme") or "").lower()
            value = _text(node)
            if scheme == "isbn" or (value.lower().startswith("isbn:")):
                found = _ISBN_RE.search(value)
                if found:
                    meta["isbn"] = found.group(1)
            elif scheme == "uuid" or value.lower().startswith("urn:uuid:"):
                meta.setdefault("uuid", value.rsplit(":", 1)[-1])
        elif tag == _OPF + "meta" or tag == "meta":
            name = (node.get("name") or "").lower()
            content = node.get("content") or ""
            if name == "calibre:series" and content:
                meta["series"] = content.strip()
            elif name == "calibre:series_index" and content:
                try:
                    meta["series_index"] = float(content)
                except ValueError:
                    pass
            elif name == "calibre:rating" and content:
                try:
                    # Calibre stores 0-10 (half stars); the UI shows 0-5.
                    meta["rating"] = round(float(content) / 2.0, 1)
                except ValueError:
                    pass

    if title:
        meta["title"] = title
    if authors:
        meta["authors"] = authors
    return meta
