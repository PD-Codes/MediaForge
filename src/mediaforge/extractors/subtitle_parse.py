"""Generic, hoster-agnostic subtitle-track harvesting from player configs.

Why this exists at all: none of the supported hosters advertise their
subtitle renditions in the HLS master playlist. yt-dlp, which only ever sees
that manifest, therefore reports "no subtitles" even when the hoster's own
web player happily shows a captions menu. The player loads those tracks
out-of-band from its embedded config (a JSON blob, a JW-Player ``tracks:``
array, a REST response), which every extractor in this package already
fetches and parses -- and then throws away, keeping only the one stream URL
it was asked for. This module turns that discarded remainder into subtitle
tracks.

Deliberately schema-agnostic. Every hoster spells the same three fields
differently (``file``/``url``/``src``, ``label``/``name``, ``lang``/
``srclang``), renames them without notice, and nests them at a different
depth. Rather than a per-hoster parser that silently rots, the walk below
looks at *any* nested dict/list and accepts an entry when it either declares
a caption-ish ``kind`` or simply points at a subtitle file extension. The
cost of that looseness is the occasional false positive; the guard against
it is an explicit reject list for the non-caption text tracks players carry
(thumbnail/sprite VTTs, chapter and metadata tracks).

Failure here must never fail a download -- callers get an empty list, never
an exception.
"""

import json
import re
from urllib.parse import urljoin, urlparse

# Subtitle sidecar extensions, matched against a URL path (query/fragment may
# follow, as signed CDN links usually carry one).
_SUB_EXT_RE = re.compile(r"\.(?:vtt|srt|ass|ssa|sub|dfxp|ttml)(?:[?#]|$)", re.IGNORECASE)

# Field names a player config might use for the track's URL / language / label.
_URL_KEYS = ("file", "url", "src", "source", "link", "path", "location", "uri")
_LANG_KEYS = (
    "lang", "language", "srclang", "src_lang", "locale", "code",
    "lang_code", "langcode", "language_code", "iso",
)
_LABEL_KEYS = (
    "label", "name", "title", "display", "displayname", "caption",
    "langname", "language_name", "description",
)
_KIND_KEYS = ("kind", "type", "role")

# A ``kind`` that positively identifies a caption track.
_GOOD_KINDS = {"captions", "caption", "subtitles", "subtitle", "text", "subs"}

# Text tracks players carry that are explicitly *not* captions. The scrubber
# preview strip is the important one: it is a real .vtt file (cue text = image
# coordinates), so extension matching alone would happily mux it in as a
# subtitle full of "sprite.jpg#xywh=0,0,160,90" lines.
_BAD_KINDS = {
    "thumbnails", "thumbnail", "thumb", "sprite", "sprites", "preview",
    "previews", "chapters", "chapter", "metadata", "descriptions", "poster",
    "storyboard", "timeslide", "seek", "trickplay",
}

# Same rejection, applied to the *key* a URL sits under, for configs that give
# no ``kind`` at all -- e.g. VeeV's ``vtt_timeslide_url``.
_BAD_KEY_RE = re.compile(
    r"thumb|sprite|timeslide|storyboard|preview|chapter|poster|trickplay|seek",
    re.IGNORECASE,
)

# A key that marks its value as subtitle data, for the shorthand shapes that
# skip per-track dicts entirely ({"subtitles": {"en": "https://..."}} or
# {"subtitles": ["https://....vtt"]}).
_SUB_KEY_RE = re.compile(r"sub(?:title)?s?$|captions?$|^tracks?$|^text_?tracks?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Language mapping
# ---------------------------------------------------------------------------
# ISO 639-1 -> ISO 639-2/B (what Matroska language tags use). Kept in step with
# models/common/subtitles.py's table; duplicated rather than imported because
# ``extractors`` must not pull the ``models`` package (and Flask with it) in at
# import time.
_LANG_2_TO_3 = {
    "de": "deu", "en": "eng", "ja": "jpn", "fr": "fra", "es": "spa",
    "it": "ita", "pt": "por", "nl": "nld", "pl": "pol", "ru": "rus",
    "tr": "tur", "ar": "ara", "zh": "zho", "ko": "kor", "sv": "swe",
    "da": "dan", "fi": "fin", "no": "nor", "cs": "ces", "hu": "hun",
    "el": "ell", "he": "heb", "hi": "hin", "ro": "ron", "uk": "ukr",
    "bg": "bul", "hr": "hrv", "sr": "srp", "sk": "slk", "sl": "slv",
    "th": "tha", "vi": "vie", "id": "ind", "ms": "msa", "fa": "fas",
}

# ISO 639-2/T (and legacy) spellings normalised onto /B, so a "ger" track and a
# "deu" track are not treated as two different languages.
_LANG_3_ALIASES = {
    "ger": "deu", "fre": "fra", "dut": "nld", "gre": "ell", "chi": "zho",
    "cze": "ces", "ice": "isl", "mac": "mkd", "may": "msa", "per": "fas",
    "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym", "alb": "sqi",
    "arm": "hye", "baq": "eus", "bur": "mya", "geo": "kat",
}

# Human-readable labels, since a caption menu usually carries "Deutsch" or
# "English (CC)" rather than a code. Both the English and the endonym spelling
# appear in the wild on the same hoster.
_LANG_NAMES = {
    "german": "deu", "deutsch": "deu", "germany": "deu", "ger": "deu",
    "english": "eng", "englisch": "eng", "eng": "eng",
    "japanese": "jpn", "japanisch": "jpn", "nihongo": "jpn",
    "french": "fra", "francais": "fra", "franzosisch": "fra",
    "spanish": "spa", "espanol": "spa", "spanisch": "spa", "castellano": "spa",
    "italian": "ita", "italiano": "ita", "italienisch": "ita",
    "portuguese": "por", "portugues": "por", "brazilian": "por",
    "dutch": "nld", "nederlands": "nld", "niederlandisch": "nld",
    "polish": "pol", "polski": "pol", "polnisch": "pol",
    "russian": "rus", "russkij": "rus", "russisch": "rus",
    "turkish": "tur", "turkce": "tur", "turkisch": "tur",
    "arabic": "ara", "arabisch": "ara",
    "chinese": "zho", "mandarin": "zho", "chinesisch": "zho",
    "korean": "kor", "hangul": "kor", "koreanisch": "kor",
    "swedish": "swe", "svenska": "swe", "danish": "dan", "dansk": "dan",
    "finnish": "fin", "suomi": "fin", "norwegian": "nor", "norsk": "nor",
    "czech": "ces", "cestina": "ces", "hungarian": "hun", "magyar": "hun",
    "greek": "ell", "hebrew": "heb", "hindi": "hin", "romanian": "ron",
    "ukrainian": "ukr", "bulgarian": "bul", "croatian": "hrv",
    "serbian": "srp", "slovak": "slk", "slovenian": "slv", "thai": "tha",
    "vietnamese": "vie", "indonesian": "ind", "malay": "msa",
    "persian": "fas", "farsi": "fas",
}

# Accent-stripping for the endonyms above ("Français" -> "francais"), done with
# a small table instead of unicodedata to keep the match predictable.
_ACCENT_MAP = str.maketrans({
    "à": "a", "á": "a", "â": "a", "ä": "a", "ã": "a", "å": "a",
    "è": "e", "é": "e", "ê": "e", "ë": "e",
    "ì": "i", "í": "i", "î": "i", "ï": "i",
    "ò": "o", "ó": "o", "ô": "o", "ö": "o", "õ": "o",
    "ù": "u", "ú": "u", "û": "u", "ü": "u",
    "ç": "c", "ñ": "n", "ß": "s", "ø": "o", "å": "a",
})

_WORD_RE = re.compile(r"[a-z]{2,}")


def lang_from_label(label):
    """ISO 639-2/B code for a caption label or language code, or ``und``.

    Accepts what a captions menu actually contains: a bare code (``de``,
    ``de-DE``, ``ger``), an English name (``German``), an endonym
    (``Deutsch``) or a decorated label (``German (Forced)``, ``English CC``).
    Unknown input yields ``und`` rather than a guess -- a mis-tagged track is
    worse than an untagged one, because players hide it behind the wrong
    language filter.
    """
    if not label:
        return "und"
    text = str(label).strip().lower().translate(_ACCENT_MAP)
    if not text:
        return "und"

    # Bare code first: "de", "de-DE", "deu", "ger_forced".
    base = re.split(r"[-_\s.,()\[\]]+", text)[0]
    if len(base) == 2 and base in _LANG_2_TO_3:
        return _LANG_2_TO_3[base]
    if len(base) == 3 and (base in _LANG_NAMES or base in _LANG_3_ALIASES):
        return _LANG_NAMES.get(base) or _LANG_3_ALIASES.get(base, base)

    # Then any word in the label that names a language, so "German (Forced)"
    # and "Audio Description - English" both resolve.
    for word in _WORD_RE.findall(text):
        if word in _LANG_NAMES:
            return _LANG_NAMES[word]
        if word in _LANG_3_ALIASES:
            return _LANG_3_ALIASES[word]
        if len(word) == 3 and word in _LANG_2_TO_3.values():
            return word
    if len(base) == 3 and base.isalpha():
        return _LANG_3_ALIASES.get(base, base)
    return "und"


def _lang_from_url(url):
    """Last-resort language guess from a subtitle URL's own file name.

    Hosters that ship a bare URL with no label almost always encode the
    language in the path (``/subs/eng.vtt``, ``…_German.vtt``), so this
    recovers a tag that would otherwise be ``und``.
    """
    try:
        path = urlparse(url).path
    except Exception:
        return "und"
    stem = path.rsplit("/", 1)[-1]
    stem = _SUB_EXT_RE.sub("", stem)
    for chunk in re.split(r"[^A-Za-z]+", stem):
        code = lang_from_label(chunk)
        if code != "und":
            return code
    return "und"


# ---------------------------------------------------------------------------
# Track extraction
# ---------------------------------------------------------------------------
def _first_str(mapping, keys):
    """First non-empty string value among *keys* in *mapping*."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_url(value):
    """True for a string plausibly usable as a track URL (absolute or relative)."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 2048 or "\n" in value:
        return False
    return bool(
        value.startswith(("http://", "https://", "//", "/"))
        or _SUB_EXT_RE.search(value)
    )


def _make_track(url, lang="", label=""):
    """Build a normalised track dict, filling in the language as best we can."""
    code = lang_from_label(lang) if lang else "und"
    if code == "und" and label:
        code = lang_from_label(label)
    if code == "und":
        code = _lang_from_url(url)
    return {"url": url.strip(), "lang": code, "label": (label or lang or "").strip()}


def _track_from_dict(node, key_hint=""):
    """Interpret a single dict as a subtitle track, or return None.

    *key_hint* is the key this dict hung off in its parent, used only to
    reject non-caption text tracks that carry no ``kind`` of their own.
    """
    kind = _first_str(node, _KIND_KEYS).lower()
    if kind and any(bad in kind for bad in _BAD_KINDS):
        return None
    if key_hint and _BAD_KEY_RE.search(key_hint):
        return None

    url = _first_str(node, _URL_KEYS)
    if not _looks_like_url(url):
        return None
    if _BAD_KEY_RE.search(url):
        return None

    # Accept on an explicit caption kind, or -- for the many configs that omit
    # ``kind`` entirely -- on the file extension alone.
    if not (kind in _GOOD_KINDS or _SUB_EXT_RE.search(url)):
        return None

    return _make_track(url, _first_str(node, _LANG_KEYS), _first_str(node, _LABEL_KEYS))


def _walk(node, out, seen, key_hint="", depth=0):
    """Recursively collect subtitle tracks from a parsed config structure."""
    if depth > 12:  # Configs are shallow; the cap only guards against cycles.
        return
    if isinstance(node, dict):
        track = _track_from_dict(node, key_hint)
        if track and track["url"] not in seen:
            seen.add(track["url"])
            out.append(track)
        for key, value in node.items():
            key_str = str(key)
            if _BAD_KEY_RE.search(key_str):
                continue
            # Shorthand shape: a subtitle-ish key mapping a language code
            # straight onto a URL string, with no track dict in between.
            if isinstance(value, str):
                if _looks_like_url(value) and _SUB_EXT_RE.search(value) and value not in seen:
                    seen.add(value)
                    hint = key_str if not _SUB_KEY_RE.search(key_str) else ""
                    out.append(_make_track(value, hint, hint))
                continue
            _walk(value, out, seen, key_str, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            if isinstance(value, str):
                if _looks_like_url(value) and _SUB_EXT_RE.search(value) and value not in seen:
                    seen.add(value)
                    out.append(_make_track(value))
                continue
            _walk(value, out, seen, key_hint, depth + 1)


def dedupe_by_language(tracks):
    """Keep one track per language, preserving order.

    A player routinely lists the same language twice — a normal and a "forced"
    variant, or a plain and an SDH/CC one. Both are the same language to a
    player's track menu, and muxing both in gives the viewer two entries called
    "German" with no way to tell them apart. ``und`` is exempt: two
    unidentified tracks really are two different languages, we just cannot say
    which, so dropping one would lose content.
    """
    out, seen = [], set()
    for track in tracks or []:
        lang = (track or {}).get("lang") or "und"
        if lang != "und" and lang in seen:
            continue
        seen.add(lang)
        out.append(track)
    return out


def tracks_from_config(obj):
    """Subtitle tracks found anywhere inside a parsed player config.

    *obj* is whatever the extractor already decoded -- a dict from a REST
    response, a decrypted playback blob, a JSON-decoded inline script. Returns
    ``[{"url", "lang", "label"}]``, possibly empty; never raises.
    """
    out, seen = [], set()
    try:
        _walk(obj, out, seen)
    except Exception:
        return dedupe_by_language(out)
    return dedupe_by_language(out)


# Object literals inside a JW-Player-style ``tracks:``/``captions:`` array.
# These are raw JS, so keys are usually unquoted and strings single-quoted --
# json.loads cannot read them, hence the pair-wise regex below.
_TRACKS_BLOCK_RE = re.compile(
    r"(?:tracks|captions|subtitles|textTracks)\s*[:=]\s*(\[[^\]]*\])",
    re.IGNORECASE | re.DOTALL,
)
_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_PAIR_RE = re.compile(
    r"""["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*:\s*["']([^"']*)["']""", re.DOTALL
)
# Bare subtitle URLs anywhere in the text, as the fallback when no structured
# track array is present.
_BARE_URL_RE = re.compile(
    r"""["'](https?://[^"'\s]+?\.(?:vtt|srt|ass|ssa)(?:\?[^"'\s]*)?)["']""",
    re.IGNORECASE,
)


def tracks_from_text(text):
    """Subtitle tracks scraped out of raw JS/HTML.

    For extractors that never get a parseable config object: the player's
    track list lives in inline script source. Tries structured
    ``tracks: [{...}]`` arrays first (which carry labels and language codes),
    then falls back to bare subtitle URLs anywhere in the text. Never raises.
    """
    out, seen = [], set()
    if not text or not isinstance(text, str):
        return out
    try:
        for block in _TRACKS_BLOCK_RE.findall(text):
            # A well-formed array may just be JSON; use it when it is, since
            # that preserves nesting the pair-wise regex would flatten.
            try:
                parsed = json.loads(block)
            except Exception:
                parsed = None
            if parsed is not None:
                for track in tracks_from_config(parsed):
                    if track["url"] not in seen:
                        seen.add(track["url"])
                        out.append(track)
                continue
            for obj_text in _OBJ_RE.findall(block):
                node = {k.lower(): v for k, v in _PAIR_RE.findall(obj_text)}
                track = _track_from_dict(node)
                if track:
                    if track["url"] not in seen:
                        seen.add(track["url"])
                        out.append(track)
                else:
                    # Mark rejects as seen too. A thumbnail track carries a real
                    # .vtt URL, so the bare-URL sweep below would otherwise
                    # re-admit exactly what the structured parse just threw out —
                    # and without the ``kind`` it would then guess a language
                    # from the file name ("th.vtt" -> Thai).
                    rejected = _first_str(node, _URL_KEYS)
                    if rejected:
                        seen.add(rejected)

        # Only guess from bare URLs when the config carried no track array at
        # all. A structured list is authoritative; adding unlabelled extras
        # next to it means muxing in files the player never offered.
        if not out:
            for url in _BARE_URL_RE.findall(text):
                if url in seen or _BAD_KEY_RE.search(url):
                    continue
                seen.add(url)
                out.append(_make_track(url))
    except Exception:
        return dedupe_by_language(out)
    return dedupe_by_language(out)


def absolutize(tracks, base_url):
    """Resolve relative track URLs against *base_url*, in place.

    Player configs routinely give ``/subs/en.vtt`` or a protocol-relative
    ``//cdn.host/en.vtt``; a downloader handed either of those fails. Returns
    the same list for call-site convenience. Never raises.
    """
    if not tracks:
        return tracks or []
    try:
        for track in tracks:
            url = (track.get("url") or "").strip()
            if not url:
                continue
            if url.startswith("//"):
                scheme = urlparse(base_url or "").scheme or "https"
                track["url"] = f"{scheme}:{url}"
            elif not url.startswith(("http://", "https://")):
                if base_url:
                    track["url"] = urljoin(base_url, url)
    except Exception:
        pass
    return tracks
