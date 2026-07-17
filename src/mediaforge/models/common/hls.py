"""HLS master-manifest helpers shared across scraper models.

Currently this exposes :func:`rendition_languages`, which the cineby model
uses to decide whether a title carries a separate German audio rendition
inside a multi-audio HLS master playlist. It was imported
(``from ..common.hls import rendition_languages``) but the module never
existed, so cineby's German-audio detection always raised ImportError and
silently fell back to English-only. This module provides it.
"""

from __future__ import annotations

import re

# Map the many spellings a manifest may use for a language onto a small,
# canonical set the callers check against ("deu", "eng", ...). Callers do
# membership tests like ``"deu" in rendition_languages(...)``, so every German
# variant must resolve to "deu" and every English variant to "eng".
_LANG_ALIASES = {
    "de": "deu",
    "deu": "deu",
    "ger": "deu",
    "german": "deu",
    "deutsch": "deu",
    "de-de": "deu",
    "de_de": "deu",
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    "en-us": "eng",
    "en_us": "eng",
    "en-gb": "eng",
    "ja": "jpn",
    "jpn": "jpn",
    "japanese": "jpn",
    "jp": "jpn",
}


def _normalize_lang(value: str) -> set:
    """Return the canonical codes a raw language/name token maps to.

    Always includes the lowercased raw token itself, plus its canonical alias
    when known, so both ``"deu" in result`` and ``"de" in result`` work.
    """
    token = (value or "").strip().lower()
    if not token:
        return set()
    out = {token}
    alias = _LANG_ALIASES.get(token)
    if alias:
        out.add(alias)
    return out


def _parse_attributes(attr_str: str) -> dict:
    """Parse an ``#EXT-X-MEDIA`` attribute list into a dict.

    Handles quoted values that may themselves contain commas
    (e.g. ``NAME="Deutsch, Stereo"``), which a naive ``split(",")`` would break.
    Keys are upper-cased; quotes are stripped from values.
    """
    attrs = {}
    # KEY=VALUE where VALUE is either a quoted string or an unquoted token.
    for m in re.finditer(r'([A-Z0-9\-]+)=("(?:[^"\\]|\\.)*"|[^,]*)', attr_str):
        key = m.group(1).upper()
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        attrs[key] = val
    return attrs


def rendition_languages(manifest_text: str, master_url: str | None = None) -> set:
    """Return the set of audio-rendition language codes in an HLS master.

    Scans ``#EXT-X-MEDIA:TYPE=AUDIO`` tags for their ``LANGUAGE`` (and, as a
    fallback, ``NAME``) attributes and returns a set of canonical codes plus the
    raw tokens, so a caller can test ``"deu" in rendition_languages(text)``
    regardless of whether the manifest labelled German as ``de``/``ger``/
    ``German``. ``master_url`` is accepted for call-site symmetry and future
    relative-URL resolution; it is not needed for language extraction.

    Returns an empty set for empty input or a manifest without audio renditions
    (a single muxed stream), which correctly reads as "no separate rendition".
    """
    langs: set = set()
    if not manifest_text:
        return langs

    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_attributes(line[len("#EXT-X-MEDIA:"):])
        if (attrs.get("TYPE") or "").upper() != "AUDIO":
            continue
        # LANGUAGE is a strict code (de/deu/ger) — match it as a whole token.
        langs |= _normalize_lang(attrs.get("LANGUAGE", ""))
        # NAME is free-form ("Deutsch, Stereo", "German (5.1)") — scan its
        # individual words so a label without a LANGUAGE attribute still counts.
        for token in re.split(r"[^a-zA-Z]+", attrs.get("NAME", "")):
            langs |= _normalize_lang(token)
    return langs
