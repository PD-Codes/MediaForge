"""Tests for the comic caches: repacking and covers.

Two things are being defended here, and only one of them is a feature.

The feature is the caching identity: a cache entry belongs to one exact file
at one exact moment, and when the file changes or disappears the entry has to
stop being used and stop taking up room. Getting that wrong means serving one
comic's pages under another comic's name.

The other thing is path traversal. Repacking a CBR means letting an external
program write files from a hostile archive to disk (CVE-2018-20250 is exactly
this, for ACE), so the tests below build the escapes by hand -- a member that
climbs out with "..", an absolute path, a symlink pointing outside -- and
assert that none of them survives into the CBZ.

Nothing here requires unrar, bsdtar or unace to be installed. The
"no extractor" path is a supported state and has its own test.
"""
import logging
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaforge.web.comics import archive, convert, covers  # noqa: E402
from mediaforge.web.media_types import COMIC_PAGE_EXTS  # noqa: E402


# A one-pixel PNG. Never decoded by anything under test -- the modules move
# bytes around and only ever look at the file name -- but a real header keeps
# the fixtures honest if that ever changes.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _cbz(path, names, payloads=None):
    """A comic archive that really is a ZIP."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, (payloads or {}).get(name, PNG))
    return path


def _fake_rar(path):
    """A file that sniffs as RAR without being one.

    Enough for every code path that has to decide "this needs an external
    extractor" -- none of which gets as far as actually decoding it, because
    the tests that would are the ones this machine cannot run.
    """
    path.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 64)
    return path


@pytest.fixture()
def caches(tmp_path, monkeypatch):
    """Point both caches at a throwaway directory.

    conftest already redirects MEDIAFORGE_CONFIG_DIR, but that one is shared
    by the whole session; these tests count entries and delete them, so they
    need a directory nobody else writes to.
    """
    convert_root = tmp_path / "cache" / "comic_convert"
    cover_root = tmp_path / "cache" / "comic_covers"
    convert_root.mkdir(parents=True)
    cover_root.mkdir(parents=True)
    monkeypatch.setattr(convert, "_cache_root", lambda: convert_root)
    # The cover cache moved into web/covercache.py (shared with the book
    # shelf), so the directory is now the CoverCache instance's, not the
    # module's. comics/covers.py keeps the same public functions -- they are
    # bound methods of this object.
    monkeypatch.setattr(covers._CACHE, "root", lambda: cover_root)
    convert._jobs.clear()
    convert._failures.clear()
    convert.refresh_extractors()
    yield {"convert": convert_root, "covers": cover_root}
    convert._jobs.clear()
    convert._failures.clear()
    convert.refresh_extractors()


class _Records(logging.Handler):
    """Collect what the shared logger emits.

    caplog cannot see it: logger.py hands out one "mediaforge" logger with
    propagate=False, so records never reach the root logger pytest attaches
    to.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def logs():
    from mediaforge.logger import get_logger
    logger = get_logger(__name__)
    handler = _Records()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", [convert, covers])
def test_the_cache_key_changes_when_the_file_is_touched(module, tmp_path):
    """The whole reason mtime is in the key: a file replaced in place keeps
    its path, so path alone would go on serving the previous comic."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    before = module.cache_key(comic)

    stat = comic.stat()
    os.utime(comic, (stat.st_atime, stat.st_mtime + 120))
    assert module.cache_key(comic) != before


@pytest.mark.parametrize("module", [convert, covers])
def test_the_cache_key_changes_when_the_content_changes(module, tmp_path):
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    before = module.cache_key(comic)
    _cbz(comic, ["p1.png", "p2.png", "p3.png"])
    assert module.cache_key(comic) != before


def test_the_converter_version_is_part_of_the_cache_key(tmp_path):
    """A fix to the repacking has to retire every archive repacked by the old
    code -- path, mtime and size do not change when MediaForge does."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    before = convert.cache_key(comic)
    original = convert._CONVERTER_VERSION
    try:
        convert._CONVERTER_VERSION = original + "-next"
        assert convert.cache_key(comic) != before
    finally:
        convert._CONVERTER_VERSION = original


def test_a_conversion_key_from_outside_is_refused():
    """converted_path() takes a key straight out of a URL."""
    for hostile in ("../../etc/passwd", "..", "", "a" * 8 + "/x", "ZZZZZZZZ"):
        with pytest.raises(ValueError):
            convert.converted_path(hostile)


# ---------------------------------------------------------------------------
# What needs converting at all
# ---------------------------------------------------------------------------

def test_a_native_archive_is_never_converted(caches, tmp_path):
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    status = convert.conversion_status(comic)
    assert status == {"ok": True, "native": True, "format": archive.FMT_ZIP}
    assert not convert.needs_conversion(comic)
    assert convert.readable_source(comic) == comic


def test_a_zip_wearing_a_cbr_extension_needs_no_extractor(caches, tmp_path):
    """The common case in the wild: "CBR" is used as the generic word for a
    comic archive, so a large share of .cbr files are renamed ZIPs. Trusting
    the extension would demand unrar for a file zipfile can open."""
    comic = _cbz(tmp_path / "issue.cbr", ["p1.png"])
    assert convert.conversion_status(comic)["native"] is True


def test_a_pdf_is_handed_to_the_browser(caches, tmp_path):
    book = tmp_path / "issue.pdf"
    book.write_bytes(b"%PDF-1.7\n" + b"\x00" * 64)
    assert convert.conversion_status(book)["direct"] is True


def test_something_that_is_not_a_comic_is_refused(caches, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert convert.conversion_status(other) == {"ok": False, "reason": "unsupported"}


def test_a_missing_file_is_not_an_exception(caches, tmp_path):
    assert convert.conversion_status(tmp_path / "gone.cbz") == {
        "ok": False, "reason": "missing"}


# ---------------------------------------------------------------------------
# No extractor installed -- a normal machine, not a failure
# ---------------------------------------------------------------------------

def test_a_missing_extractor_is_a_clean_answer(caches, tmp_path, monkeypatch, logs):
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **k: None)
    convert.refresh_extractors()

    status = convert.request_conversion(comic)
    assert status["ok"] is False
    assert status["reason"] == "no_extractor"
    assert status["format"] == archive.FMT_RAR
    assert isinstance(status["tool_hint"], list) and status["tool_hint"]
    # Nothing was started and nothing was written.
    assert not convert._jobs
    assert list(caches["convert"].iterdir()) == []
    # And above all: no ERROR record. telemetry.hooks._TelemetryLogHandler
    # turns every one of those into a crash report, and "this machine has no
    # unrar" is not a crash.
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_no_extractor_means_no_cover_and_no_error(caches, tmp_path, monkeypatch, logs):
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **k: None)
    convert.refresh_extractors()

    assert covers.cover_path(comic) is None
    assert covers.has_cover(comic) is False
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_available_extractors_reports_both_formats(caches, monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **k: None)
    convert.refresh_extractors()
    assert convert.available_extractors() == {archive.FMT_RAR: None, archive.FMT_ACE: None}


def test_an_extractor_is_found_when_one_is_on_the_path(caches, monkeypatch):
    """The lookup itself, without needing a real unrar: which() is the only
    thing that decides, and it is asked in preference order."""
    monkeypatch.setattr(convert.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/unrar" if name == "unrar" else None)
    convert.refresh_extractors()
    found = convert.find_extractor(archive.FMT_RAR)
    assert found is not None and found[0] == "unrar"

    monkeypatch.setattr(convert.shutil, "which", lambda name, *a, **k: "/usr/bin/" + name)
    convert.refresh_extractors()
    # bsdtar comes first: it is already present on macOS and Windows 10+.
    assert convert.find_extractor(archive.FMT_RAR)[0] == "bsdtar"


# ---------------------------------------------------------------------------
# Path traversal -- what an external extractor may have left behind
# ---------------------------------------------------------------------------

def test_nothing_outside_the_extraction_directory_is_collected(tmp_path):
    """The core safety check. An extractor has just run on an archive from the
    internet; everything it produced is suspect until proven to be inside."""
    work = tmp_path / "work"
    (work / "issue").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    # The one legitimate page.
    (work / "issue" / "page01.png").write_bytes(PNG)
    # A file the archive climbed out to reach. os.walk under `work` never sees
    # it, and that is exactly the assertion: it must not end up in the CBZ.
    (outside / "stolen.png").write_bytes(b"secret")
    # A file that escapes by symlink instead of by name -- the version of the
    # attack that survives a naive "does the path start with the root" check,
    # because the string does start with the root.
    linked = True
    try:
        (work / "issue" / "escape.png").symlink_to(outside / "stolen.png")
        (work / "shortcut").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        linked = False      # unprivileged Windows; the ".." cases still run

    collected = convert._collect_pages(work)
    members = [member for _path, member in collected]

    assert members == ["issue/page01.png"]
    assert not any(".." in m for m in members)
    for path, _member in collected:
        assert path.resolve().is_relative_to(work.resolve())
        assert not path.is_symlink()
    if linked:
        assert b"secret" not in (work / "issue" / "page01.png").read_bytes()


def test_the_produced_cbz_contains_no_escaping_names(tmp_path):
    """End to end: a hostile extraction result goes in, a CBZ that archive.py
    is willing to read comes out."""
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "page2.png").write_bytes(PNG)
    (work / "page10.png").write_bytes(PNG)
    (work / "page1.png").write_bytes(PNG)
    # Not pages: an executable dropped next to the images, and a sidecar.
    (work / "payload.exe").write_bytes(b"MZ")
    (work / "ComicInfo.xml").write_text("<x/>")

    out = tmp_path / "out.cbz"
    written = convert._build_cbz(convert._collect_pages(work), out)

    assert written == 3
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "payload.exe" not in names and "ComicInfo.xml" not in names
    for name in names:
        assert not name.startswith("/") and ".." not in name.split("/")
        assert Path(name).suffix.lower() in COMIC_PAGE_EXTS
    # Reading order, not lexical -- archive._natural_key sorts the whole
    # member path, so "page1" precedes "page10" and the subdirectory sorts
    # after both by its name.
    assert archive.list_pages(out) == ["page1.png", "page10.png", "sub/page2.png"]


def test_a_half_written_cbz_is_never_left_behind(tmp_path, monkeypatch):
    """The CBZ is renamed into place, so a crash mid-write must not leave a
    file the next request would happily serve as a complete comic."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "page1.png").write_bytes(PNG)
    out = tmp_path / "out.cbz"

    monkeypatch.setattr(convert, "_MAX_ENTRIES", 0)
    with pytest.raises(ValueError):
        convert._build_cbz(convert._collect_pages(work), out)
    assert not out.exists()
    assert not (tmp_path / "out.cbz.part").exists()


# ---------------------------------------------------------------------------
# Orphaned cache entries
# ---------------------------------------------------------------------------

def _fake_entry(root, key, source):
    """A finished conversion, without running one."""
    import json
    folder = root / key
    folder.mkdir(parents=True)
    (folder / "comic.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (folder / "done.json").write_text(
        json.dumps({"key": key, "source": str(source), "pages": 1}), encoding="utf-8")
    return folder


def test_a_conversion_whose_source_is_gone_is_purged(caches, tmp_path):
    """Age alone does not catch this: delete a 900 MB CBR and its repacked
    copy sits in the cache for a month taking up the room you just freed."""
    living = _fake_rar(tmp_path / "kept.cbr")
    kept = _fake_entry(caches["convert"], "a" * 24, living)
    orphan = _fake_entry(caches["convert"], "b" * 24, tmp_path / "deleted.cbr")

    assert convert.purge_orphans() == 1
    assert kept.is_dir()
    assert not orphan.exists()


def test_a_conversion_that_left_the_library_is_purged_only_when_asked(caches, tmp_path):
    """The file still exists, it is just no longer in a library path. Without
    the full list that is indistinguishable from a temporarily unmounted
    share, so it is only removed when the caller supplies one."""
    inside = _fake_rar(tmp_path / "inside.cbr")
    outside = _fake_rar(tmp_path / "outside.cbr")
    _fake_entry(caches["convert"], "c" * 24, inside)
    gone = _fake_entry(caches["convert"], "d" * 24, outside)

    assert convert.purge_orphans() == 0          # both files still exist
    assert convert.purge_orphans([inside]) == 1
    assert not gone.exists()


def test_an_unfinished_conversion_survives_the_orphan_sweep(caches, tmp_path):
    """No done.json means it is still being written -- or was interrupted.
    Either way it is cleanup_converted()'s business, by age, not this one's."""
    partial = caches["convert"] / ("e" * 24)
    partial.mkdir(parents=True)
    assert convert.purge_orphans() == 0
    assert partial.is_dir()


def test_stale_conversions_are_dropped_by_age(caches, tmp_path):
    import time
    entry = _fake_entry(caches["convert"], "f" * 24, tmp_path / "x.cbr")
    old = time.time() - 60 * 86400
    os.utime(entry / "comic.cbz", (old, old))
    os.utime(entry, (old, old))
    assert convert.cleanup_converted(max_age_days=30) == 1
    assert not entry.exists()


# ---------------------------------------------------------------------------
# Cache size reporting
# ---------------------------------------------------------------------------

def test_cache_stats_counts_what_is_actually_on_disk(caches, tmp_path):
    """The figure shown next to the "clear cache" buttons. A conversion is a
    directory, so this has to count the metadata next to the CBZ as well --
    that is what the cache costs."""
    assert convert.cache_stats() == {"files": 0, "bytes": 0}
    assert covers.cache_stats() == {"files": 0, "bytes": 0}

    _fake_entry(caches["convert"], "a" * 24, tmp_path / "one.cbr")
    _fake_entry(caches["convert"], "b" * 24, tmp_path / "two.cbr")
    stats = convert.cache_stats()
    assert stats["files"] == 4          # two comic.cbz + two done.json
    assert stats["bytes"] > 0

    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    covers.cover_path(comic)
    cover_stats = covers.cache_stats()
    assert cover_stats["files"] == 1
    assert cover_stats["bytes"] == len(PNG)


def test_cache_stats_never_raises_without_a_cache(caches, monkeypatch, tmp_path):
    """It is called to render a settings page, so a cache directory that is
    not there (or not readable) has to read as empty rather than as a 500."""
    missing = tmp_path / "nothing-here"
    monkeypatch.setattr(convert, "_cache_root", lambda: missing)
    monkeypatch.setattr(covers._CACHE, "root", lambda: missing)
    assert convert.cache_stats() == {"files": 0, "bytes": 0}
    assert covers.cache_stats() == {"files": 0, "bytes": 0}


def test_clearing_a_cache_is_cleanup_with_a_zero_age(caches, tmp_path):
    """What the two buttons on the settings page do: the same housekeeping
    functions the daily worker calls, with the cutoff moved to "now"."""
    _fake_entry(caches["convert"], "c" * 24, tmp_path / "one.cbr")
    covers.cover_path(_cbz(tmp_path / "issue.cbz", ["p1.png"]))

    assert convert.cleanup_converted(max_age_days=0) == 1
    assert covers.cleanup_covers(max_age_days=0) == 1
    assert convert.cache_stats() == {"files": 0, "bytes": 0}
    assert covers.cache_stats() == {"files": 0, "bytes": 0}


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------

def test_the_cover_is_the_first_page_byte_for_byte(caches, tmp_path):
    """Stored as it came out of the archive: no resize, no re-encode. A cover
    that differs from its page by one byte means something is decoding it."""
    first = b"\x89PNG\r\n\x1a\n" + b"FIRST" + b"\x00" * 32
    comic = _cbz(tmp_path / "issue.cbz",
                 ["page10.png", "page2.png", "page1.png"],
                 {"page1.png": first})

    path = covers.cover_path(comic)
    assert path is not None
    assert path.read_bytes() == first
    assert covers.has_cover(comic) is True
    assert covers.cover_mimetype(path) == "image/png"


def test_the_cover_is_served_from_the_cache_the_second_time(caches, tmp_path, monkeypatch):
    """The reason this cache exists at all: the second request must not open
    the archive again. Proven by making opening it impossible."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    first = covers.cover_path(comic)
    assert first is not None

    def _explode(*args, **kwargs):
        raise AssertionError("the archive was opened a second time")

    monkeypatch.setattr(covers.archive, "first_page", _explode)
    assert covers.cover_path(comic) == first


def test_an_edited_comic_does_not_keep_the_old_cover(caches, tmp_path):
    """Same path, different file: a new key, so the shelf cannot go on showing
    the cover of the issue that used to be there."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    first = covers.cover_path(comic)
    stat = comic.stat()
    os.utime(comic, (stat.st_atime, stat.st_mtime + 300))
    assert covers.cover_path(comic) != first


def test_a_pdf_gets_no_cover(caches, tmp_path):
    """pdf.js draws page one in the browser; rasterising it here would need a
    PDF engine for a picture the client already has."""
    comic = tmp_path / "issue.pdf"
    comic.write_bytes(b"%PDF-1.7\n" + b"\x00" * 64)
    assert covers.cover_path(comic) is None
    assert covers.has_cover(comic) is False


def test_an_archive_without_images_gets_no_cover(caches, tmp_path):
    comic = _cbz(tmp_path / "issue.cbz", ["readme.txt", "ComicInfo.xml"])
    assert covers.cover_path(comic) is None


def test_a_broken_archive_gets_no_cover_and_no_error(caches, tmp_path, logs):
    comic = tmp_path / "truncated.cbz"
    comic.write_bytes(b"PK\x03\x04" + b"\x00" * 32)     # sniffs as ZIP, is not
    assert covers.cover_path(comic) is None
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_an_oversized_first_page_is_not_cached(caches, tmp_path, monkeypatch):
    """The ceiling is about memory, not about pictures: the page is read into
    RAM in one piece on a route any logged-in user can hit."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    monkeypatch.setattr(covers._CACHE, "max_bytes", 4)
    assert covers.cover_path(comic) is None
    assert list(caches["covers"].iterdir()) == []


def test_covers_of_vanished_comics_are_purged(caches, tmp_path):
    one = _cbz(tmp_path / "one.cbz", ["p1.png"])
    two = _cbz(tmp_path / "two.cbz", ["p1.png"])
    assert covers.cover_path(one) and covers.cover_path(two)
    assert len(list(caches["covers"].iterdir())) == 2

    two.unlink()
    assert covers.purge_orphans([one, two]) == 1
    assert covers.has_cover(one) is True
    assert len(list(caches["covers"].iterdir())) == 1


def test_a_cover_whose_comic_left_the_library_is_purged(caches, tmp_path):
    one = _cbz(tmp_path / "one.cbz", ["p1.png"])
    two = _cbz(tmp_path / "two.cbz", ["p1.png"])
    covers.cover_path(one)
    covers.cover_path(two)
    assert covers.purge_orphans([one]) == 1
    assert covers.has_cover(one) is True
    assert covers.has_cover(two) is False


def test_an_edited_comic_leaves_no_stale_cover(caches, tmp_path):
    """The cover of the old version is nobody's cover any more: its key is not
    produced by anything, so the sweep takes it."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    covers.cover_path(comic)
    stat = comic.stat()
    os.utime(comic, (stat.st_atime, stat.st_mtime + 300))
    assert covers.purge_orphans([comic]) == 1
    assert covers.has_cover(comic) is False


def test_an_interrupted_cover_write_is_swept_up(caches, tmp_path):
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    key = covers.cache_key(comic)
    leftover = caches["covers"] / "{}.999-1.part".format(key)
    leftover.write_bytes(b"half")
    # The key is still wanted, but a ".part" is not a cover -- it reduces to
    # the same stem, so it is kept here and replaced by the real write.
    assert covers.purge_orphans([comic]) == 0
    assert covers.cover_path(comic) is not None


def test_purging_with_an_empty_library_empties_the_cache(caches, tmp_path):
    """Documented behaviour, and the reason the docstring insists on a
    complete list: covers are derived data, so this is recoverable, but it
    must never be called with a partial scan."""
    comic = _cbz(tmp_path / "issue.cbz", ["p1.png"])
    covers.cover_path(comic)
    assert covers.purge_orphans([]) == 1
    assert list(caches["covers"].iterdir()) == []


# ---------------------------------------------------------------------------
# Peeking: the cover without the repack
# ---------------------------------------------------------------------------
# A cover is the first page and nothing more, so pulling one member out of a
# CBR must not cost a full unpack plus a second copy of the file in the cache.
# None of this needs unrar or bsdtar to exist: the tool is an argv list and a
# subprocess, and both are stood in for below.


class _FakeTool:
    """An external extractor that is not there.

    Answers the listing step from a fixed set of names and lets the test decide
    what the extraction step drops on disk -- including things a hostile
    archive would drop, which is the whole point of two of the tests below.
    """

    def __init__(self, names, produce=None):
        self.names = list(names)
        self.produce = produce
        self.calls = []

    def __call__(self, argv, **kwargs):
        import subprocess as sp
        self.calls.append(list(argv))
        assert kwargs.get("shell") is False, "an extractor must never go through a shell"
        assert kwargs.get("stdin") == sp.DEVNULL, "a password prompt must fail, not wait"
        assert kwargs.get("timeout"), "an extractor must always have a time limit"
        if "-tf" in argv:                      # the listing step
            out = ("\n".join(self.names) + "\n").encode("utf-8")
        else:                                  # the single-member extraction
            dest = Path(argv[argv.index("-C") + 1])
            if self.produce is not None:
                self.produce(dest, argv)
            out = b""
        return sp.CompletedProcess(argv, 0, out, b"")

    @property
    def extraction(self):
        """The argv of the extraction call, or None if it never happened."""
        for argv in self.calls:
            if "-tf" not in argv:
                return argv
        return None


@pytest.fixture()
def bsdtar(monkeypatch):
    """Pretend bsdtar is installed, and let the test drive it."""
    monkeypatch.setattr(convert, "find_extractor",
                        lambda fmt: ("bsdtar", "/usr/bin/bsdtar", convert._cmd_bsdtar))

    def install(names, produce=None):
        tool = _FakeTool(names, produce)
        monkeypatch.setattr(convert.subprocess, "run", tool)
        return tool

    return install


def test_a_zip_wearing_a_cbr_extension_is_never_peeked_at(caches, tmp_path, monkeypatch):
    """The commonest file in the wild. It is a ZIP, zipfile opens it, and no
    external tool may be involved -- proven by making the peek fatal."""
    peek = convert.extract_first_image

    def _explode(*args, **kwargs):
        raise AssertionError("a native archive was handed to an extractor")

    monkeypatch.setattr(convert, "extract_first_image", _explode)
    comic = _cbz(tmp_path / "issue.cbr", ["page2.png", "page1.png"],
                 {"page1.png": PNG + b"FIRST"})

    path = covers.cover_path(comic)
    assert path is not None and path.read_bytes() == PNG + b"FIRST"
    # And the peek declines it on its own account too: a container this process
    # can open is never an extractor's business, whatever it is named.
    monkeypatch.setattr(convert.subprocess, "run", _explode)
    assert peek(comic) == (None, None)


def test_peeking_without_a_tool_is_a_clean_none(caches, tmp_path, monkeypatch, logs):
    """A machine with no unrar is a normal machine. No exception, no ERROR --
    telemetry.hooks turns every ERROR record into a crash report."""
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **k: None)
    convert.refresh_extractors()

    assert convert.extract_first_image(comic) == (None, None)
    assert list(caches["convert"].iterdir()) == []
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_a_tool_that_cannot_extract_one_member_declines(caches, tmp_path, monkeypatch, logs):
    """unar's single-file extraction is not dependable across the builds in
    circulation, so it answers None and the caller repacks properly. A cover
    that is quietly the wrong page would be worse than no cover."""
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert, "find_extractor",
                        lambda fmt: ("unar", "/usr/bin/unar", convert._cmd_unar))

    def _explode(*args, **kwargs):
        raise AssertionError("unar was run for a peek")

    monkeypatch.setattr(convert.subprocess, "run", _explode)
    assert convert.extract_first_image(comic) == (None, None)
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_the_peeked_page_is_the_one_the_reader_calls_page_one(caches, tmp_path, bsdtar):
    """The cover has to be the page the reader opens as page 1. Both sides go
    through archive._is_page_name and archive._natural_key, so the assertion is
    against list_pages() itself rather than against a hand-written expectation."""
    names = ["page10.png", "sub/page2.png", "page1.png", "ComicInfo.xml", "cover/"]
    reference = _cbz(tmp_path / "reference.cbz", [n for n in names if not n.endswith("/")])
    expected = archive.list_pages(reference)[0]

    def produce(dest, argv):
        member = argv[-1]
        target = dest / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG + member.encode("utf-8"))

    tool = bsdtar(names, produce)
    comic = _fake_rar(tmp_path / "issue.cbr")

    name, data = convert.extract_first_image(comic)
    assert name == expected == "page1.png"
    assert data == PNG + b"page1.png"
    # The sidecar, the directory entry and page10 were never asked for.
    assert tool.extraction[-1] == "page1.png"
    # And nothing was left behind in the cache: no scratch directory, no CBZ.
    assert list(caches["convert"].iterdir()) == []


def test_a_listed_name_that_escapes_is_never_even_requested(caches, tmp_path, bsdtar):
    """Half of the traversal defence, at the earliest possible point: a member
    named "../../evil.png" is not a page name, so it is not a candidate for
    being page one and no extractor is ever pointed at it."""
    tool = bsdtar(["../../evil.png", "/etc/passwd.png", "C:\\windows\\x.png",
                   "__MACOSX/._page1.png", "page1.png"],
                  lambda dest, argv: (dest / "page1.png").write_bytes(PNG))
    comic = _fake_rar(tmp_path / "issue.cbr")

    name, data = convert.extract_first_image(comic)
    assert name == "page1.png" and data == PNG
    for argv in tool.calls:
        assert not any(".." in str(a) for a in argv)


def test_a_peeked_file_that_lands_outside_the_temp_directory_is_dropped(
        caches, tmp_path, bsdtar, logs):
    """The other half, and the one a prefix-string comparison would fail: the
    extractor is told to write page1.png and instead writes it outside the
    directory, leaving a symlink behind that points there. The escape has to be
    detected after the fact, by resolving the path -- the string does start
    with the extraction directory."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.png").write_bytes(PNG + b"SECRET")

    linked = {"ok": False}

    def produce(dest, argv):
        # Nothing legitimate is produced at all: what "page1.png" resolves to
        # is a file the archive had no business reaching.
        try:
            (dest / "page1.png").symlink_to(outside / "stolen.png")
            linked["ok"] = True
        except (OSError, NotImplementedError):
            pass                            # unprivileged Windows

    bsdtar(["page1.png"], produce)
    comic = _fake_rar(tmp_path / "issue.cbr")

    name, data = convert.extract_first_image(comic)
    assert (name, data) == (None, None)
    if linked["ok"]:
        assert data != PNG + b"SECRET"
    # The scratch directory is gone whichever way it ended, and the file it
    # tried to reach was neither read nor touched.
    assert list(caches["convert"].iterdir()) == []
    assert (outside / "stolen.png").read_bytes() == PNG + b"SECRET"
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_a_peek_that_produces_a_different_page_is_refused(caches, tmp_path, bsdtar):
    """"Something came out" is not "the right page came out" -- 7z and unrar
    read their file argument as a mask. A cover showing page 7 is worse than
    no cover."""
    bsdtar(["page1.png", "page7.png"],
           lambda dest, argv: (dest / "page7.png").write_bytes(PNG))
    comic = _fake_rar(tmp_path / "issue.cbr")
    assert convert.extract_first_image(comic) == (None, None)


def test_an_implausibly_large_first_page_is_not_read_into_memory(caches, tmp_path,
                                                                bsdtar, monkeypatch):
    monkeypatch.setattr(convert, "_MAX_PAGE_BYTES", 4)
    bsdtar(["page1.png"], lambda dest, argv: (dest / "page1.png").write_bytes(PNG))
    comic = _fake_rar(tmp_path / "issue.cbr")
    assert convert.extract_first_image(comic) == (None, None)


def test_an_archive_holding_no_images_peeks_to_nothing(caches, tmp_path, bsdtar, logs):
    bsdtar(["readme.txt", "ComicInfo.xml"])
    comic = _fake_rar(tmp_path / "issue.cbr")
    assert convert.extract_first_image(comic) == (None, None)
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_a_failing_extractor_is_not_an_exception(caches, tmp_path, monkeypatch, logs):
    """Every way an external program can fail -- a non-zero exit, a hang, a
    binary that is not there any more -- is one missing cover, never a raise."""
    import subprocess as sp
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert, "find_extractor",
                        lambda fmt: ("bsdtar", "/usr/bin/bsdtar", convert._cmd_bsdtar))

    for outcome in (sp.CompletedProcess([], 2, b"", b""),
                    sp.TimeoutExpired("bsdtar", 20),
                    OSError("no such file")):
        def run(argv, **kwargs):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        monkeypatch.setattr(convert.subprocess, "run", run)
        assert convert.extract_first_image(comic) == (None, None)
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


# ---------------------------------------------------------------------------
# Covers that come from a peek
# ---------------------------------------------------------------------------

def test_a_cbr_cover_costs_one_member_and_no_conversion(caches, tmp_path, monkeypatch, logs):
    """The point of the whole exercise: a shelf of five thousand CBRs gets its
    covers without five thousand full unpacks and without a second copy of the
    library in the conversion cache."""
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert, "extract_first_image",
                        lambda src, fmt=None: ("page1.png", PNG + b"COVER"))

    def _explode(*args, **kwargs):
        raise AssertionError("a conversion was started for a cover")

    monkeypatch.setattr(convert, "conversion_status", _explode)

    path = covers.cover_path(comic)
    assert path is not None
    assert path.read_bytes() == PNG + b"COVER"
    assert covers.has_cover(comic) is True
    assert list(caches["convert"].iterdir()) == []
    assert [r for r in logs.records if r.levelno >= logging.ERROR] == []


def test_a_cover_that_cannot_be_peeked_falls_back_to_the_conversion(caches, tmp_path,
                                                                    monkeypatch):
    """Unchanged behaviour when the peek cannot deliver: the repack is started
    and the cover appears on a later request."""
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(convert, "extract_first_image", lambda src, fmt=None: (None, None))
    asked = []
    monkeypatch.setattr(convert, "conversion_status",
                        lambda src, start=False: asked.append(start) or
                        {"ok": True, "pending": True})

    assert covers.cover_path(comic) is None
    assert asked == [True]


def test_walking_a_library_neither_peeks_nor_converts(caches, tmp_path, monkeypatch):
    """start_conversion=False means "pure cache lookup". An extractor per issue
    would be exactly the stall that flag exists to prevent."""
    comic = _fake_rar(tmp_path / "issue.cbr")

    def _explode(*args, **kwargs):
        raise AssertionError("the shelf renderer ran an extractor")

    monkeypatch.setattr(convert, "extract_first_image", _explode)
    assert covers.cover_path(comic, start_conversion=False) is None


def test_a_peeked_cover_that_is_too_large_is_not_cached(caches, tmp_path, monkeypatch):
    """The same ceiling as for a native archive -- both routes into the cache
    go through CoverCache.store_image(), so neither can quietly lose it."""
    comic = _fake_rar(tmp_path / "issue.cbr")
    monkeypatch.setattr(covers._CACHE, "max_bytes", 4)
    monkeypatch.setattr(convert, "extract_first_image",
                        lambda src, fmt=None: ("page1.png", PNG))
    assert covers.cover_path(comic) is None
    assert list(caches["covers"].iterdir()) == []


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------

def test_both_modules_work_without_any_external_tool(caches, monkeypatch):
    """The whole point of the graceful degradation: a machine with no
    archiver installed must still import and use these."""
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **k: None)
    convert.refresh_extractors()
    assert convert.available_extractors()
    assert covers.cleanup_covers(max_age_days=1) == 0
    assert convert.cleanup_converted(max_age_days=1) == 0
    assert convert.purge_orphans() == 0
