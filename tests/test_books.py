"""Tests for eBook identity: which files on disk are the same book.

The fixtures are not invented. Every awkward name below was taken from a real
Calibre library, because that is where this logic actually fails: Calibre
truncates the filename it writes, stores one record per format, and happily
keeps five folders for one novel.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaforge.web.books.identity import (  # noqa: E402
    clean_title,
    group_key,
    looks_like_author_folder,
    merge_groups,
    normalize,
    split_filename,
    split_series,
)
from mediaforge.web.media_types import (  # noqa: E402
    BOOK_ALL_EXTS,
    BOOK_EXTS,
    VIDEO_EXTS,
    book_format_sort_key,
    media_type_for,
)


# --------------------------------------------------------------------------
# media_types
# --------------------------------------------------------------------------

def test_video_and_book_extension_sets_do_not_overlap():
    """The whole safety story rests on this: a path is a video or a book, never
    both. If they ever overlapped, a book could reach the upscale worker."""
    assert not (VIDEO_EXTS & BOOK_ALL_EXTS)


def test_media_type_for():
    assert media_type_for("/x/Show S01E01.mkv") == "video"
    assert media_type_for("/x/Book.epub") == "book"
    assert media_type_for("/x/Book.KFX") == "book"
    assert media_type_for("/x/cover.jpg") == ""


def test_epub_is_preferred_over_pdf():
    assert book_format_sort_key(".epub") < book_format_sort_key(".mobi")
    assert book_format_sort_key(".mobi") < book_format_sort_key(".pdf")


# --------------------------------------------------------------------------
# normalize / clean_title
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("World of Warcraft 12 - Vol'Jin Schatten der Horde", "world of warcraft 12 vol jin schatten der horde"),
        ("Warcraft 02 - Der Lord der Clans (100)", "warcraft 02 der lord der clans"),
        ("Yunas Geisterhaus 01 (GER)(KAZE)(FG-Manga)", "yunas geisterhaus 01"),
        ("Die Shannara-Chroniken_B01BLF4IR8", "die shannara chroniken"),
        ("Bücher über Straßen", "bucher uber strassen"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_clean_title_keeps_casing():
    assert clean_title("Warcraft 02 - Der Lord der Clans (100)") == "Warcraft 02 - Der Lord der Clans"


# --------------------------------------------------------------------------
# split_filename
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stem,title,author",
    [
        # The ordinary case.
        ("Black Hat Python - Justin Seitz", "Black Hat Python", "Justin Seitz"),
        # Title contains dashes: only the LAST separator counts.
        ("Warcraft 02 - Der Lord der Clan - Christie Golden",
         "Warcraft 02 - Der Lord der Clan", "Christie Golden"),
        # Several authors stay one string; splitting them is the OPF's job.
        ("Practical Malware Analysis - Michael Sikorski & Andrew Honig",
         "Practical Malware Analysis", "Michael Sikorski & Andrew Honig"),
        # A subtitle is not an author -- the underscore gives it away.
        ("Die Shannara-Chroniken - Elfensteine_ Roman",
         "Die Shannara-Chroniken - Elfensteine_ Roman", ""),
        # Placeholder authors are dropped from BOTH halves.
        ("thelinuxcommandline - Unbekannt", "thelinuxcommandline", ""),
        ("Yunas Geisterhaus 01 (GER)(KAZE)(FG-Manga) - KCC", "Yunas Geisterhaus 01", ""),
        # No separator at all.
        ("Kindle_Users_Guide", "Kindle_Users_Guide", ""),
    ],
)
def test_split_filename(stem, title, author):
    assert split_filename(stem) == (title, author)


# --------------------------------------------------------------------------
# split_series
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,series,index",
    [
        ("World of Warcraft 08 - Weltenbeben", "World of Warcraft", 8.0),
        ("Warcraft 02 - Der Lord der Clans", "Warcraft", 2.0),
        ("Yunas Geisterhaus 01", "Yunas Geisterhaus", 1.0),
        # No number: a standalone book, not a one-volume series.
        ("Black Hat Python", "", None),
        # A year is not a volume number.
        ("Learn Kali Linux 2019", "", None),
    ],
)
def test_split_series(title, series, index):
    assert split_series(title) == (series, index)


def test_looks_like_author_folder():
    assert looks_like_author_folder("Christie Golden")
    assert looks_like_author_folder("A. P. David")
    # Collections, not people.
    assert not looks_like_author_folder("IT Stuff")
    assert not looks_like_author_folder("Books")
    assert not looks_like_author_folder("Mieruko-chan Manga Deutsch - filecrypt.cc")
    # A Calibre book folder.
    assert not looks_like_author_folder("Warcraft 02 - Der Lord der Clans (100)")


# --------------------------------------------------------------------------
# merge_groups -- the actual de-duplication
# --------------------------------------------------------------------------

def _c(title, author="", path=None, ext=".epub"):
    return {"title": title, "authors": [author] if author else [], "path": path or title + ext}


def test_calibre_one_book_five_records():
    """The motivating case: Calibre keeps one record per format, so a single
    novel is five folders. All five must collapse into one book."""
    bucket = merge_groups([
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/l/a/24/x.mobi"),
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/l/a/100/x.epub"),
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/l/a/101/x.azw3"),
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/l/a/103/x.epub"),
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/l/a/105/x.pdf"),
    ])
    assert len(bucket) == 1
    assert len(bucket[0]) == 5


def test_truncated_filename_merges_with_full_opf_title():
    """Calibre writes a shortened filename next to an OPF that has the full
    title. Both describe the same book and must not become two."""
    groups = merge_groups([
        _c("Warcraft 02 - Der Lord der Clans", "Christie Golden", "/from/opf.epub"),
        _c("Warcraft 02 - Der Lord der Clan", "Christie Golden", "/from/filename.mobi"),
    ])
    assert len(groups) == 1


def test_prefix_merge_needs_a_matching_author():
    """A prefix relation alone is not enough -- two different authors keep two
    books, even when one title starts with the other."""
    groups = merge_groups([
        _c("Der Herr der Ringe Gefaehrten", "J.R.R. Tolkien"),
        _c("Der Herr der Ringe Gefaehrten Kommentiert", "Jemand Anders"),
    ])
    assert len(groups) == 2


def test_short_titles_never_prefix_merge():
    """Below the length floor a prefix match is coincidence, not identity."""
    groups = merge_groups([_c("Dune", "Frank Herbert"), _c("Dune Messiah", "Frank Herbert")])
    assert len(groups) == 2


def test_authorless_file_joins_its_only_candidate():
    """A loose download without an author in the filename belongs to the one
    book of that title that does have one."""
    groups = merge_groups([
        _c("Python Crash Course", "Eric Matthes", "/lib/eric/pcc.epub"),
        _c("Python Crash Course", "", "/lib/pcc.mobi"),
    ])
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_authorless_file_stays_separate_when_ambiguous():
    """Two different books share a title: guessing which one the author-less
    copy belongs to would be worse than leaving it alone."""
    groups = merge_groups([
        _c("Dune Chronicles Complete", "Frank Herbert", "/a.epub"),
        _c("Dune Chronicles Complete", "Brian Herbert", "/b.epub"),
        _c("Dune Chronicles Complete", "", "/c.epub"),
    ])
    assert len(groups) == 3


def test_different_volumes_of_a_series_stay_separate():
    groups = merge_groups([
        _c("Yunas Geisterhaus 01", "", "/1.mobi"),
        _c("Yunas Geisterhaus 02", "", "/2.mobi"),
        _c("Yunas Geisterhaus 15", "", "/15.mobi"),
    ])
    assert len(groups) == 3


def test_group_key_is_stable_across_noise():
    """The same book named two ways produces one key -- that identity is what
    the whole grouping pass is built on."""
    assert group_key("Warcraft 02 - Der Lord der Clans (100)", "Christie Golden") == \
        group_key("warcraft 02 der lord der clans", "christie golden")


def test_empty_title_does_not_collapse_everything():
    """A file whose name yields no title at all must not join every other
    nameless file into one giant book."""
    groups = merge_groups([
        {"title": "", "authors": [], "path": "/a.epub"},
        {"title": "", "authors": [], "path": "/b.epub"},
    ])
    assert len(groups) == 2


def test_readable_and_unreadable_formats_are_both_kept():
    """A DRM-protected .kfx is listed, not hidden -- but it is not in the set
    of formats a reader may be handed."""
    assert ".kfx" in BOOK_ALL_EXTS
    assert ".kfx" not in BOOK_EXTS


# --------------------------------------------------------------------------
# Scanner integration -- a miniature Calibre library on disk
# --------------------------------------------------------------------------

def _write_opf(path, title, author, series=None, index=None, isbn=None):
    lines = [
        '<?xml version="1.0"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">',
        "<dc:title>%s</dc:title>" % title,
        '<dc:creator opf:role="aut">%s</dc:creator>' % author,
        "<dc:language>de</dc:language>",
    ]
    if isbn:
        lines.append('<dc:identifier opf:scheme="ISBN">%s</dc:identifier>' % isbn)
    if series:
        lines.append('<meta name="calibre:series" content="%s"/>' % series)
        lines.append('<meta name="calibre:series_index" content="%s"/>' % index)
    lines += ["</metadata></package>"]
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def calibre_library(tmp_path):
    """A library with the shapes that actually occur in the wild."""
    from mediaforge.web.books.scanner import scan_books  # noqa: F401  (import check)

    # One novel, four formats, five Calibre records -- plus a sixth copy
    # sitting loose in the library root.
    for rid, ext in ((24, "mobi"), (100, "epub"), (101, "azw3"), (103, "epub"), (105, "pdf")):
        folder = tmp_path / "Christie Golden" / ("Warcraft 02 - Der Lord der Clans (%d)" % rid)
        folder.mkdir(parents=True)
        (folder / ("Warcraft 02 - Der Lord der Clan - Christie Golden.%s" % ext)).write_bytes(b"x" * 10)
        (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        _write_opf(folder / "metadata.opf", "Warcraft 02 - Der Lord der Clans",
                   "Christie Golden", "Warcraft", "2.0", "9783833216718")
    (tmp_path / "Warcraft 02 - Der Lord der Clans - Christie Golden.azw3").write_bytes(b"x" * 10)

    # Loose files with no author anywhere, and a DRM-protected one.
    (tmp_path / "Yunas Geisterhaus 01 (GER)(KAZE)(FG-Manga) - KCC.mobi").write_bytes(b"x" * 10)
    (tmp_path / "Die Shannara-Chroniken - Elfensteine_ Roman_B01BLF4IR8.kfx").write_bytes(b"x" * 10)

    # Noise that must never become a book.
    (tmp_path / "My Clippings.txt").write_text("x")
    (tmp_path / "Some Show S01E01.mkv").write_bytes(b"x" * 10)
    return tmp_path


def test_scan_merges_a_calibre_split_book(calibre_library):
    from mediaforge.web.books.scanner import scan_books
    books = scan_books(calibre_library)
    warcraft = [b for b in books if b["title"].startswith("Warcraft 02")]
    assert len(warcraft) == 1, "five Calibre records plus a loose copy must be one book"
    book = warcraft[0]
    # Five records + the loose copy in the root.
    assert len(book["formats"]) == 6
    assert {f["ext"] for f in book["formats"]} == {"epub", "azw3", "mobi", "pdf"}
    # EPUB is offered first because it needs no conversion.
    assert book["formats"][0]["ext"] == "epub"
    assert book["authors"] == ["Christie Golden"]
    assert book["series"] == "Warcraft" and book["series_index"] == 2.0
    assert book["cover_path"].endswith("cover.jpg")
    # Regression: the alternation used to match ISBN-10 first and cut a
    # 13-digit ISBN down to its first ten digits.
    assert book["isbn"] == "9783833216718"


def test_scan_ignores_non_book_files(calibre_library):
    from mediaforge.web.books.scanner import scan_books
    titles = [b["title"] for b in scan_books(calibre_library)]
    assert not any("Clippings" in t for t in titles)
    assert not any("S01E01" in t for t in titles)


def test_library_root_never_becomes_an_author(calibre_library):
    """Regression: a file loose in the library root has no author folder above
    it, and falling back to the root's own name filed every such book under an
    "author" named after the drive the library happens to sit on."""
    from mediaforge.web.books.scanner import scan_books
    books = scan_books(calibre_library)
    root_name = calibre_library.name
    for book in books:
        assert root_name not in (book["authors"] or [])
    loose = [b for b in books if b["title"].startswith("Yunas")][0]
    assert loose["authors"] == []


def test_drm_format_is_listed_but_not_readable(calibre_library):
    from mediaforge.web.books.scanner import scan_books
    books = scan_books(calibre_library)
    shannara = [b for b in books if "Shannara" in b["title"]]
    assert len(shannara) == 1, "a KFX-only book stays visible instead of vanishing"
    assert shannara[0]["formats"][0]["readable"] is False


def test_scan_of_a_missing_path_is_empty_not_an_error(tmp_path):
    from mediaforge.web.books.scanner import scan_books
    assert scan_books(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# Making Kindle markup survive an XML parser
# ---------------------------------------------------------------------------
# An EPUB content document is handed to an XML parser, which has no error
# recovery: one `&nbsp;` and the reader shows "Entity 'nbsp' not defined"
# instead of the book. Everything below is a shape that actually came out of
# the `mobi` package for a real AZW3.

def test_named_entities_become_numeric():
    from mediaforge.web.books.convert import _numeric_entities
    out = _numeric_entities("Hallo&nbsp;Welt &mdash; ja&hellip;")
    assert "&nbsp;" not in out and "&mdash;" not in out
    assert "&#160;" in out and "&#8212;" in out


def test_the_five_xml_entities_are_left_alone():
    from mediaforge.web.books.convert import _numeric_entities
    text = "&amp; &lt; &gt; &quot; &apos; &#233; &#x2014;"
    assert _numeric_entities(text) == text


def test_a_bare_ampersand_is_escaped():
    """`AT&T` is as fatal to an XML parser as a named entity is."""
    from mediaforge.web.books.convert import _numeric_entities
    assert _numeric_entities("AT&T sells &stuff; here") == "AT&amp;T sells &amp;stuff; here"


def test_undeclared_namespace_prefixes_get_an_xmlns():
    from mediaforge.web.books.convert import _declare_namespaces
    doc = '<html xmlns="http://www.w3.org/1999/xhtml"><body><mbp:pagebreak/></body></html>'
    out = _declare_namespaces(doc)
    assert 'xmlns:mbp="http://www.mobipocket.com/ns/mbp"' in out


def test_an_already_declared_prefix_is_not_declared_twice():
    from mediaforge.web.books.convert import _declare_namespaces
    doc = ('<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:mbp="http://www.mobipocket.com/ns/mbp"><mbp:pagebreak/></html>')
    assert _declare_namespaces(doc).count("xmlns:mbp") == 1


def test_a_repaired_kindle_document_actually_parses(tmp_path):
    """The whole point, end to end: broken in, valid XML out."""
    from mediaforge.web.books.convert import _sanitize_markup, _parses_as_xml
    doc = tmp_path / "chapter1.xhtml"
    doc.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<mbp:pagebreak/><p>Ein&nbsp;Satz &mdash; mit Sonderzeichen &amp; AT&T.</p>'
        "</body></html>",
        encoding="utf-8",
    )
    opf = tmp_path / "content.opf"
    opf.write_text(
        '<package><manifest>'
        '<item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest></package>",
        encoding="utf-8",
    )
    assert not _parses_as_xml(doc.read_text(encoding="utf-8"))
    _sanitize_markup(tmp_path, opf)
    assert _parses_as_xml(doc.read_text(encoding="utf-8"))
    # Nothing was unrepairable, so the media type stays what the EPUB spec wants.
    assert "application/xhtml+xml" in opf.read_text(encoding="utf-8")


def test_a_document_that_cannot_be_repaired_is_relabelled(tmp_path):
    """The escape hatch: an unclosed tag is not worth guessing at, so the file
    goes to the browser's HTML parser instead of showing a parser error."""
    from mediaforge.web.books.convert import _sanitize_markup
    doc = tmp_path / "broken.xhtml"
    doc.write_text("<html><body><p>offen<br></body></html>", encoding="utf-8")
    opf = tmp_path / "content.opf"
    opf.write_text(
        '<package><manifest>'
        '<item id="b" href="broken.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest></package>",
        encoding="utf-8",
    )
    _sanitize_markup(tmp_path, opf)
    assert 'media-type="text/html"' in opf.read_text(encoding="utf-8")


def test_the_converter_version_is_part_of_the_cache_key(tmp_path):
    """Twice now a fix reached nobody because path, mtime and size had not
    changed and the old conversion was served forever."""
    from mediaforge.web.books import convert
    book = tmp_path / "b.azw3"
    book.write_bytes(b"x" * 32)
    before = convert.cache_key(book)
    original = convert._CONVERTER_VERSION
    try:
        convert._CONVERTER_VERSION = original + "-next"
        assert convert.cache_key(book) != before
    finally:
        convert._CONVERTER_VERSION = original


# ---------------------------------------------------------------------------
# Reader preferences
# ---------------------------------------------------------------------------

def test_every_reader_preference_the_client_sends_is_accepted():
    """/api/user/preferences rejects the WHOLE call on one unknown key, so a
    single unregistered reader setting silently threw away all the others."""
    from mediaforge.web.db import USER_UI_PREF_KEYS
    for key in ("reader_font", "reader_theme", "reader_flow",
                "reader_face", "reader_lead", "reader_width"):
        assert key in USER_UI_PREF_KEYS, key


def test_the_reader_size_range_matches_the_clients_clamp():
    """static/reader.js clamps to 70..220; a value the client will produce and
    the server refuses does not fail quietly, it drops the whole batch."""
    from mediaforge.web.db import USER_UI_PREF_KEYS
    valid = USER_UI_PREF_KEYS["reader_font"]
    assert valid("70") and valid("220")
    assert not valid("69") and not valid("221")


# ── filename parsing brought in line with the comic side ─────────────────

def test_percent_escapes_are_decoded():
    """A filename that has been through a web download keeps the encoding of
    whatever produced it, and "In der H%f6lle der Sioux" went onto the shelf
    spelled exactly like that."""
    from mediaforge.web.books.identity import clean_title, normalize
    assert clean_title("In der H%f6lle der Sioux") == "In der Hölle der Sioux"
    # And, more importantly, the two spellings merge into one card.
    assert normalize("In der H%f6lle der Sioux") == normalize("In der Hölle der Sioux")


def test_an_undecodable_escape_is_left_alone():
    """Better one odd title than a second kind of mojibake."""
    from mediaforge.web.books.identity import clean_title
    assert "100%" in clean_title("100% Sicherheit")


def test_ampersand_and_and_are_the_same_book():
    from mediaforge.web.books.identity import normalize
    assert normalize("Fire & Blood") == normalize("Fire and Blood")


def test_a_trailing_bracketed_year_is_recovered():
    """clean_title() deletes every bracketed group -- right for "(GER)(KAZE)",
    wrong for the only date most downloads carry."""
    from mediaforge.web.books.identity import extract_year
    assert extract_year("Der Report (2019)") == ("Der Report", 2019)
    assert extract_year("Der Report (Heyne 2019)") == ("Der Report", 2019)


def test_a_year_inside_the_title_stays_in_the_title():
    """"1984" is a novel, not a date."""
    from mediaforge.web.books.identity import extract_year
    assert extract_year("1984") == ("1984", None)
    assert extract_year("Sommer 1944 im Osten") == ("Sommer 1944 im Osten", None)
    assert extract_year("Some Title (GER)(KAZE)")[1] is None


def test_a_trailing_year_survives_clean_title():
    """extract_year() has to run on the RAW title.

    clean_title() deletes every bracketed group, so running the year search on
    its output finds nothing -- which is exactly the bug this guards: the
    scanner did that at first and every "(2019)" was silently dropped instead
    of becoming the published date.
    """
    from mediaforge.web.books.identity import clean_title, extract_year
    assert extract_year(clean_title("Der Wuestenplanet (2019)")) == ("Der Wuestenplanet", None)
    raw_without_year, year = extract_year("Der Wuestenplanet (2019)")
    assert year == 2019
    assert clean_title(raw_without_year) == "Der Wuestenplanet"
