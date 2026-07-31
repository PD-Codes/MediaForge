"""Book covers: where they come from, and that the cache stays the books'.

The book shelf used to show a cover only when a ``cover.jpg`` happened to lie
next to the file, and served that file untouched -- so most libraries were a
wall of grey placeholders, and the ones that were not sent three-megabyte
Calibre JPEGs for a tile 160 pixels wide. An EPUB has carried its cover inside
it the whole time.

Two things are worth guarding here and they pull in opposite directions:

  * the extraction has to handle what real EPUBs actually do -- EPUB 2 and
    EPUB 3 spell "this is the cover" completely differently, and plenty of
    files say it neither way,
  * the machinery is now SHARED with the comic shelf (web/covercache.py), and
    sharing an implementation must never turn into sharing a cache. Clearing
    one library's covers may not touch the other's.
"""

import io
import zipfile

import pytest

pytest.importorskip("PIL")


def _png(width, height, colour):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "PNG")
    return buf.getvalue()


def _container(opf_path):
    return ('<?xml version="1.0"?><container xmlns='
            '"urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="{}"/></rootfiles></container>'.format(opf_path))


@pytest.fixture()
def books_dir(tmp_path, monkeypatch):
    """A throwaway library plus a throwaway cover cache pointed at tmp_path."""
    from mediaforge.web.books import covers

    root = tmp_path / "lib"
    root.mkdir()
    cache = tmp_path / "cache"
    monkeypatch.setattr(covers._CACHE, "root", lambda: _mk(cache))
    covers.reset_preparation()
    return root


def _mk(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def _epub3(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", _container("OEBPS/content.opf"))
        zf.writestr("OEBPS/content.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                    '<item id="c" href="images/front.png" media-type="image/png"'
                    ' properties="cover-image"/></manifest></package>')
        zf.writestr("OEBPS/images/front.png", _png(1400, 2100, "red"))
    return path


def _epub2(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", _container("content.opf"))
        zf.writestr("content.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                    '<meta name="cover" content="theid"/></metadata><manifest>'
                    '<item id="theid" href="cover.jpeg" media-type="image/jpeg"/>'
                    '</manifest></package>')
        zf.writestr("cover.jpeg", _png(900, 1200, "blue"))
    return path


# ── extraction ───────────────────────────────────────────────────────────

def test_epub3_cover_image_property(books_dir):
    """EPUB 3 marks the cover with properties="cover-image" on the item."""
    from mediaforge.web.books import covers
    name, data = covers.epub_cover(_epub3(books_dir / "three.epub"))
    # The href is relative to the OPF, which is in a subdirectory.
    assert name == "OEBPS/images/front.png"
    assert data


def test_epub2_meta_cover(books_dir):
    """EPUB 2 names an id in <meta name="cover">, resolved via the manifest."""
    from mediaforge.web.books import covers
    name, data = covers.epub_cover(_epub2(books_dir / "two.epub"))
    assert name == "cover.jpeg"
    assert data


def test_an_undeclared_but_obviously_named_image_is_used(books_dir):
    """Plenty of real files declare no cover and ship one anyway."""
    from mediaforge.web.books import covers
    path = books_dir / "guess.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", _container("content.opf"))
        zf.writestr("content.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                    '<item id="t" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="i" href="img/Cover.PNG" media-type="image/png"/>'
                    '</manifest></package>')
        zf.writestr("img/Cover.PNG", _png(800, 1000, "green"))
    name, data = covers.epub_cover(path)
    assert name == "img/Cover.PNG" and data


def test_a_book_with_no_image_is_not_an_error(books_dir):
    from mediaforge.web.books import covers
    path = books_dir / "bare.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", _container("content.opf"))
        zf.writestr("content.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                    '<item id="t" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest></package>')
        zf.writestr("ch1.xhtml", "<html/>")
    assert covers.epub_cover(path) == ("", b"")
    assert covers.cover_path(path) is None


def test_something_that_is_not_a_zip_is_not_an_error(books_dir):
    """A truncated download or a DRM-wrapped file. Both mean "no cover"."""
    from mediaforge.web.books import covers
    path = books_dir / "broken.epub"
    path.write_bytes(b"this is not a zip")
    assert covers.epub_cover(path) == ("", b"")
    assert covers.cover_path(path) is None


def test_a_sidecar_is_the_fallback_for_every_format(books_dir):
    """Including PDF, which is the one format nothing can be extracted from."""
    from mediaforge.web.books import covers
    (books_dir / "book.pdf").write_bytes(b"%PDF-1.4 not really")
    (books_dir / "cover.jpg").write_bytes(_png(1200, 1600, "orange"))
    got = covers.cover_path(books_dir / "book.pdf")
    assert got is not None and got.is_file()


# ── the cache ────────────────────────────────────────────────────────────

def test_the_cover_is_downscaled_to_webp(books_dir):
    """The whole point of caching it rather than streaming the original."""
    from mediaforge.web.books import covers
    src = _epub3(books_dir / "big.epub")
    got = covers.cover_path(src)
    assert got is not None
    assert got.suffix == ".webp"
    assert covers.cover_mimetype(got) == "image/webp"
    _name, original = covers.epub_cover(src)
    assert got.stat().st_size < len(original)


def test_a_second_call_is_a_cache_hit(books_dir):
    from mediaforge.web.books import covers
    src = _epub3(books_dir / "hit.epub")
    first = covers.cover_path(src)
    assert covers.has_cover(src)
    assert covers.cover_path(src) == first


def test_editing_the_book_retires_its_cover(books_dir):
    """The key carries mtime and size, so a replaced file cannot keep showing
    the previous book's cover."""
    from mediaforge.web.books import covers
    src = _epub3(books_dir / "swap.epub")
    before = covers.cache_key(src)
    _epub2(src)                     # same name, different contents
    assert covers.cache_key(src) != before
    assert not covers.has_cover(src)


def test_purge_orphans_keeps_what_is_still_there(books_dir):
    from mediaforge.web.books import covers
    keep = _epub3(books_dir / "keep.epub")
    gone = _epub2(books_dir / "gone.epub")
    covers.cover_path(keep)
    covers.cover_path(gone)
    assert covers.cache_stats()["files"] == 2
    gone.unlink()
    assert covers.purge_orphans([str(keep), str(gone)]) == 1
    assert covers.has_cover(keep)


# ── separate from the comic cache ────────────────────────────────────────

def test_books_and_comics_do_not_share_a_directory():
    """They share an implementation (web/covercache.py) and nothing else.

    Clearing one library's covers must not throw away the other's work, and
    the two can be very different sizes -- so they are two instances with two
    subdirectories, not one cache with two callers.
    """
    from mediaforge.web.books import covers as book_covers
    from mediaforge.web.comics import covers as comic_covers
    assert book_covers._CACHE.subdir != comic_covers._CACHE.subdir
    assert book_covers._CACHE.root() != comic_covers._CACHE.root()


def test_the_two_workers_are_separate_queues():
    from mediaforge.web.books import covers as book_covers
    from mediaforge.web.comics import covers as comic_covers
    assert book_covers._WORKER is not comic_covers._WORKER
    assert book_covers._WORKER.name != comic_covers._WORKER.name


def test_clearing_the_book_cache_leaves_the_comic_cache_alone(tmp_path, monkeypatch):
    from mediaforge.web.books import covers as book_covers
    from mediaforge.web.comics import covers as comic_covers

    book_root = _mk(tmp_path / "books")
    comic_root = _mk(tmp_path / "comics")
    monkeypatch.setattr(book_covers._CACHE, "root", lambda: book_root)
    monkeypatch.setattr(comic_covers._CACHE, "root", lambda: comic_root)
    (book_root / "aaa.webp").write_bytes(b"x")
    (comic_root / "bbb.webp").write_bytes(b"y")

    book_covers.cleanup_covers(max_age_days=0)

    assert book_covers.cache_stats()["files"] == 0
    assert comic_covers.cache_stats()["files"] == 1
