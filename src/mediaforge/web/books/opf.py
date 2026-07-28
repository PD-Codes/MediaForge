"""Reading Calibre's ``metadata.opf`` sidecar.

An OPF is the small XML file Calibre writes next to every book it manages. It
is by far the best metadata source available without opening the book itself:
it is a few kilobytes, it sits in a directory we are stat-ing anyway, and it
carries the *untruncated* title, which the filename does not (Calibre shortens
the filename part of its layout, so "Warcraft 02 - Der Lord der Clans" ends up
on disk as "... der Clan").
"""
from __future__ import annotations

import html as _html
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

# Calibre stores the description as a blob of HTML -- exactly what the user
# typed into its comment field, tags and all. Rendered as text it reads as
# "<p>Das Kriminalroman-Paket</p><p><strong>von ...", so it has to be turned
# into plain text before it ever leaves this module. Escaping it (which the
# frontend does) is the right defence against injection but the wrong answer
# to markup that was never meant to be shown.
_BLOCK_END_RE = re.compile(r"(?i)</(p|div|li|h[1-6]|tr|blockquote)\s*>|<br\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """Flatten a fragment of HTML into readable plain text.

    Block ends become paragraph breaks so the original structure survives as
    blank lines instead of collapsing into one wall of words; every other tag
    is dropped and HTML entities are resolved (&uuml; -> ü, &nbsp; -> space).
    """
    if not raw:
        return ""
    text = _BLOCK_END_RE.sub("\n\n", raw)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\r", "")
    # Collapse runs of spaces/tabs, then runs of blank lines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ISO 639-2/B three-letter codes, which is what Calibre writes ("deu"), mapped
# to the two-letter code the rest of the app uses. Only the languages a German
# or English library realistically contains -- an unknown code is passed
# through rather than guessed at.
_LANG_MAP = {
    "deu": "de", "ger": "de", "eng": "en", "fra": "fr", "fre": "fr",
    "spa": "es", "ita": "it", "nld": "nl", "dut": "nl", "por": "pt",
    "rus": "ru", "jpn": "ja", "zho": "zh", "chi": "zh", "kor": "ko",
    "pol": "pl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
    "ces": "cs", "cze": "cs", "tur": "tr", "ell": "el", "gre": "el",
    "hun": "hu", "ron": "ro", "rum": "ro", "ukr": "uk", "ara": "ar",
    "heb": "he", "lat": "la",
    # Some tools write the name instead of a code.
    "german": "de", "deutsch": "de", "english": "en", "englisch": "en",
    "french": "fr", "spanish": "es", "italian": "it", "japanese": "ja",
}


def normalize_language(raw: str) -> str:
    """Return a two-letter language code for whatever the metadata carries.

    Calibre and Mobipocket both write ISO 639-2 ("deu"), the OPF standard
    allows regional tags ("de-DE"), and some files carry a plain "German".
    The UI wants one shape.
    """
    value = (raw or "").strip().lower().replace("_", "-")
    if not value:
        return ""
    base = value.split("-", 1)[0]
    if len(base) == 2:
        return base
    return _LANG_MAP.get(base, base)


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
            meta["language"] = normalize_language(_text(node))
        elif tag == _DC + "date" and "published" not in meta:
            # Calibre writes a full ISO timestamp ("2017-05-18T22:00:00+00:00").
            # A book has a publication date, not a publication second, and the
            # timezone offset actively misleads -- it is the moment Calibre
            # imported the file, not anything about the book.
            meta["published"] = _text(node)[:10]
        elif tag == _DC + "description" and "description" not in meta:
            meta["description"] = html_to_text(_text(node))
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
