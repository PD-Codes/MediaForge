"""Repacking a comic archive this process cannot open into one it can.

Two of the six comic containers are closed shop for Python: RAR needs an
unrar-derived decoder and ACE needs a decoder nobody has maintained since the
nineties. Neither can be shipped -- unrar's licence forbids redistribution as
part of a competing archiver, and there is no maintained ACE library at all --
so the only honest way to read them is to ask whatever the machine already has
to unpack the file once, repack the pages into a CBZ, and let every other
module go on reading plain ZIPs (see comics/archive.py).

That "once" is the whole point. The result is cached under the config
directory keyed by (path, mtime, size, converter version), the same identity
books/convert.py and transcoder._thumb_key use: replace the source file and
the key stops matching, so a stale conversion can never outlive its source.

THE ORIGINAL FILE IS NEVER MODIFIED, MOVED, REPLACED OR DELETED. Nothing in
this module opens the user's comic for writing, and nothing is ever written
next to it -- everything lands in the cache. That is also what makes this work
when the library sits on a read-only mount or a network share.

Security note, because this is the part that matters: the extractors are
external programs, they are handed a file from wherever the user got it, and
they write to disk. A comic archive is the textbook path-traversal vector --
CVE-2018-20250 was exactly this, an ACE archive whose member name escaped the
extraction directory and dropped a payload into the user's Startup folder. So
extraction goes into a fresh temporary directory and NOTHING that comes out of
it is trusted: every produced path is resolved and checked to still be inside
that directory, symlinks are discarded rather than followed, and only files
whose name passes the same page-name validator archive.py uses are copied into
the CBZ. Subprocesses are always an argument list (never shell=True), always
with stdin closed so a password prompt cannot hang the worker, and always with
a timeout.

Nothing here installs anything. If no extractor is present the answer is a
plain ``{"ok": False, "reason": "no_extractor"}`` -- a machine without unrar
is a normal machine, not a failure, and it must not be logged at ERROR level
(telemetry.hooks._TelemetryLogHandler turns every ERROR record into a crash
report, and "this user has no unrar" is not a crash).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ...logger import get_logger
from ..media_types import COMIC_EXTS
from . import archive

logger = get_logger(__name__)


# Bump when the produced CBZ changes shape (different member naming, a new
# filter, a bug fix in the traversal guard). It is part of the cache key, so
# raising it retires every previously converted comic instead of serving the
# old result forever -- the mistake that let a bad conversion survive a fix in
# books/convert.py, because path, mtime and size had of course not changed.
_CONVERTER_VERSION = "v1"

# At most two conversions at a time. Unpacking a 300 MB CBR is mostly disk,
# but a shelf-wide click storm should not be able to start forty of them.
_MAX_PARALLEL = 2

# Remember a failure for an hour: long enough that the client stops polling an
# archive that cannot be converted, short enough that a transient problem (a
# NAS that was briefly away) is retried the same afternoon.
_FAIL_TTL = 3600.0

# A single comic issue is tens of megabytes; a collected volume can be a
# gigabyte. Beyond that it is not a comic, and unpacking it would be an easy
# way to fill the config volume.
_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ENTRIES = 20_000

# How long an external extractor may run. Generous, because it is measured
# against a gigabyte off a spinning disk -- but finite, because an extractor
# that sits waiting for something would otherwise hold a worker slot forever.
_EXTRACT_TIMEOUT = 900

# Only keys this module produced can ever address a cache entry.
_KEY_RE = re.compile(r"[0-9a-f]{8,40}")

_lock = threading.Lock()
_jobs: dict = {}        # key -> True while a conversion runs
_failures: dict = {}    # key -> (timestamp, reason)


# ---------------------------------------------------------------------------
# External extractors
# ---------------------------------------------------------------------------
# Order is preference, best first:
#
#   bsdtar  libarchive, reads RAR4/RAR5 and needs no separate unrar. Ships with
#           macOS and with Windows 10+ (as tar.exe), so on two of three
#           platforms this is already there.
#   7z/7za  p7zip; reads RAR when the (separately packaged) rar codec is
#           present, which the common distro builds include.
#   unar    The Unarchiver's CLI, the usual answer on macOS via Homebrew.
#   unrar   the reference decoder. Last because it is the one distributions
#           put in a non-free repository.
#
# ACE has exactly one real option, and "unace" is not packaged anywhere any
# more. It is listed because a user who already has it should get their .cba
# read; it is emphatically not something MediaForge asks anyone to install.

def _cmd_bsdtar(exe, src, dest):
    # -C changes into the destination; --no-same-owner keeps a hostile archive
    # from asking for a chown it must never get.
    return [exe, "-x", "--no-same-owner", "-f", str(src), "-C", str(dest)], dest


def _cmd_7z(exe, src, dest):
    # "-p" is an EMPTY password rather than no password option at all: without
    # it an encrypted archive stops and asks, and a prompt in a daemon is a
    # hang. -bd drops the progress indicator, -y answers the overwrite
    # questions that cannot happen anyway (the directory is fresh).
    return [exe, "x", "-y", "-bd", "-p", "-o{}".format(dest), str(src)], dest


def _cmd_unar(exe, src, dest):
    # -D extracts the contents rather than wrapping them in a folder named
    # after the archive; -f overwrites without asking; -p "" is the same
    # anti-prompt measure as 7z's -p.
    return [exe, "-q", "-D", "-f", "-p", "", "-o", str(dest), str(src)], dest


def _cmd_unrar(exe, src, dest):
    # "x" keeps the paths inside the archive, -p- refuses to ask for a
    # password, -idq silences everything but errors. The trailing separator on
    # the destination is not decoration: unrar treats a bare word as a filename
    # prefix instead of a directory.
    return [exe, "x", "-y", "-idq", "-p-", str(src), str(dest) + os.sep], dest


def _cmd_unace(exe, src, dest):
    # unace has no output-directory option worth relying on -- the builds in
    # circulation disagree about it -- so it is run *inside* the destination
    # and writes relative to the working directory. Its own path handling is
    # exactly what CVE-2018-20250 was about, which is why the result is
    # re-checked path by path afterwards regardless of what it did.
    return [exe, "x", str(src)], dest


def _is_bsdtar(exe: str) -> bool:
    """True if this ``tar`` is libarchive's, not GNU's.

    Windows 10 (1803+) and every current macOS ship bsdtar -- but they install
    it as `tar`, not `bsdtar`, so looking only for the latter misses a working
    RAR reader that is already on the machine. That is worth catching: it is
    the difference between "comics just work" and "install something first" on
    two of the three platforms MediaForge builds for.

    The name alone cannot be trusted the other way round, though. On Linux
    `tar` is GNU tar, which cannot read RAR at all and whose failure would
    surface as an unhelpful "extraction failed" instead of the honest "no
    extractor here". So the binary is asked what it is, once, and only
    accepted if it says libarchive.
    """
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=10,
            stdin=subprocess.DEVNULL, text=True, errors="replace", check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
    return "bsdtar" in blob or "libarchive" in blob


def _bundled(name: str):
    """Path to a tool shipped inside the PyInstaller bundle, or None.

    Nothing is bundled today: libarchive publishes no prebuilt binaries, and
    the one tool that could simply be dropped in -- RARLAB's unrar -- carries
    a licence that forbids redistribution alongside GPL-3.0 code. The lookup
    exists anyway because it costs one stat: a later build that does vendor a
    bsdtar into `vendor/` is picked up without touching this file again.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    for candidate in (name, name + ".exe"):
        path = Path(base) / "vendor" / candidate
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


# fmt -> ordered tuple of (executable name, argv builder, verifier or None).
# A verifier runs the candidate once and decides whether it is really the tool
# the name suggests; see _is_bsdtar.
_EXTRACTORS = {
    archive.FMT_RAR: (
        ("bsdtar", _cmd_bsdtar, None),
        # Same binary, the name it has on Windows and macOS. Verified, because
        # on Linux this name belongs to GNU tar.
        ("tar", _cmd_bsdtar, _is_bsdtar),
        ("7z", _cmd_7z, None),
        ("7za", _cmd_7z, None),
        ("unar", _cmd_unar, None),
        ("unrar", _cmd_unrar, None),
    ),
    archive.FMT_ACE: (
        ("unace", _cmd_unace, None),
        ("unar", _cmd_unar, None),
    ),
}

# What to tell the user when nothing is installed. Names only -- MediaForge
# does not hand out install commands for software it cannot vouch for.
# `tar` is deliberately NOT listed: it is something a machine either already
# has in the right flavour or does not, never something to go and install.
_TOOL_HINTS = {
    archive.FMT_RAR: ("bsdtar", "7z", "unar", "unrar"),
    archive.FMT_ACE: ("unace", "unar"),
}

_extractor_cache: dict = {}
_extractor_lock = threading.Lock()


def find_extractor(fmt: str):
    """The first usable extractor for *fmt* as ``(name, path, builder)``, or None.

    Looked up with :func:`shutil.which` and remembered, because this is called
    from a status route the client polls. Nothing is ever installed or
    downloaded; a machine without any of these is simply a machine that cannot
    open CBRs, which is a supported state.
    """
    with _extractor_lock:
        if fmt in _extractor_cache:
            return _extractor_cache[fmt]
    found = None
    for name, builder, verify in _EXTRACTORS.get(fmt, ()):
        exe = _bundled(name) or shutil.which(name)
        if not exe:
            continue
        if verify is not None and not verify(exe):
            logger.debug("[Comics] %s at %s is not the tool we need -- skipping", name, exe)
            continue
        found = (name, exe, builder)
        break
    with _extractor_lock:
        _extractor_cache[fmt] = found
    if found is None:
        logger.debug("[Comics] No extractor for %s (looked for %s)",
                     fmt, ", ".join(_TOOL_HINTS.get(fmt, ())))
    return found


def refresh_extractors() -> None:
    """Forget which extractors were found. Call after the PATH may have changed."""
    with _extractor_lock:
        _extractor_cache.clear()


def available_extractors() -> dict:
    """{format: tool name or None} -- for the diagnostics page and for tests."""
    return {fmt: (find_extractor(fmt) or (None,))[0] for fmt in _EXTRACTORS}


# ---------------------------------------------------------------------------
# Cache layout
# ---------------------------------------------------------------------------

def _cache_root() -> Path:
    """Where conversions live: ``<config>/comic_convert/<key>/comic.cbz``.

    Its own subdirectory rather than a shared one, so the comic cache can be
    wiped without touching converted books. Imported lazily and with a
    fallback the same way books/convert._cache_root does it -- config is a
    package-level module and this has to keep working when it is reached from
    a context that never built the Flask app (the CLI, a test).
    """
    try:
        from ...config import MEDIAFORGE_CONFIG_DIR
        base = Path(MEDIAFORGE_CONFIG_DIR)
    except Exception:
        base = Path(tempfile.gettempdir()) / "mediaforge"
    root = base / "comic_convert"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_key(path) -> str:
    """Identity of one conversion: path + mtime + size + converter version.

    Including mtime and size is what makes a replaced file produce a different
    key instead of silently serving the previous comic's pages. Raises OSError
    if the file is gone, which every caller already has to handle.
    """
    path = Path(path)
    stat = path.stat()
    raw = "{}|{}|{}|{}".format(
        path.resolve(), int(stat.st_mtime), stat.st_size, _CONVERTER_VERSION
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def converted_path(key: str) -> Path:
    """Where the finished CBZ for *key* lives. Never trusts the key blindly.

    The key reaches this through an HTTP route, so it is user input by the
    time it comes back -- the regex rules out traversal and the containment
    check catches anything the regex somehow did not.
    """
    if not _KEY_RE.fullmatch(key or ""):
        raise ValueError("invalid conversion key")
    root = _cache_root().resolve()
    target = (root / key / "comic.cbz").resolve()
    if not target.is_relative_to(root):
        raise ValueError("conversion key escapes the cache")
    return target


def is_converted(src) -> bool:
    """True if a finished CBZ for *src* is already on disk."""
    try:
        key = cache_key(src)
    except OSError:
        return False
    return (_cache_root() / key / "done.json").is_file()


def needs_conversion(src) -> bool:
    """True if this file has to be repacked before its pages can be read."""
    fmt = archive.sniff(src)
    return fmt in _EXTRACTORS


# ---------------------------------------------------------------------------
# Doing the work
# ---------------------------------------------------------------------------

def _run_extractor(tool, src: Path, dest: Path) -> tuple:
    """Run one extractor into *dest*. Returns (ok, short reason).

    Never raises: a missing binary, a crash, a hang and a refusal all mean the
    same thing to the caller -- this archive did not unpack.
    """
    name, exe, builder = tool
    argv, cwd = builder(exe, src, dest)
    try:
        proc = subprocess.run(
            argv,                       # always a list; shell=True would hand
            shell=False,                # a filename with a quote in it to sh
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,   # a password prompt must fail, not wait
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_EXTRACT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[Comics] %s timed out after %ss on %s", name, _EXTRACT_TIMEOUT, src.name)
        return False, "extract_timeout"
    except OSError as exc:
        # The binary vanished between which() and here, or is not executable.
        logger.info("[Comics] Cannot run %s: %s", name, exc)
        refresh_extractors()
        return False, "extract_failed"
    if proc.returncode != 0:
        tail = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        # info, not error: a password-protected or truncated CBR is a property
        # of the file, not a bug in MediaForge.
        logger.info("[Comics] %s failed on %s (rc=%s) %s",
                    name, src.name, proc.returncode, " | ".join(tail))
        return False, "extract_failed"
    return True, ""


def _collect_pages(root: Path) -> list:
    """Every page image under *root* that is provably still under *root*.

    This is the containment check the whole module exists for. Everything an
    external extractor produced is suspect: a member named ``../../evil.jpg``,
    an absolute path, a symlink pointing at ``/etc``, a directory symlink that
    turns an innocent-looking relative path into an escape. So each entry is
    resolved and compared against the resolved root, symlinks are dropped
    without being followed, and the surviving names still have to satisfy
    archive._is_page_name -- the same validator that will later decide whether
    a member of the produced CBZ may be served, which is what guarantees every
    page written here is readable afterwards.

    Returns [(absolute path, member name)] in reading order.
    """
    root_real = root.resolve()
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune symlinked directories before descending: os.walk would not
        # follow them, but a file *inside* one still resolves outside the root
        # and there is no reason to look at them at all.
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                real = path.resolve()
                if not real.is_relative_to(root_real):
                    logger.warning("[Comics] Dropped an entry that escaped the "
                                   "extraction directory: %r", name)
                    continue
                if not real.is_file():
                    continue
                member = real.relative_to(root_real).as_posix()
            except (OSError, ValueError):
                continue
            if not archive._is_page_name(member):
                continue
            found.append((real, member))
    return sorted(found, key=lambda item: archive._natural_key(item[1]))


def _build_cbz(pages: list, out: Path) -> int:
    """Write *pages* into a CBZ at *out*. Returns the number of pages written.

    Stored, not deflated: every page is a JPEG, PNG or WebP and therefore
    already compressed, so deflate would spend real CPU to save about one
    percent -- and stored entries are what make serving a single page out of
    the middle of a 400 MB archive cheap.

    Written to a ``.part`` file and renamed, so a crash or a power cut leaves
    no half-written CBZ that the next request would happily serve.
    """
    tmp_out = out.with_name(out.name + ".part")
    total = 0
    written = 0
    try:
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_STORED) as zf:
            for path, member in pages:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                total += size
                written += 1
                if total > _MAX_UNPACKED_BYTES or written > _MAX_ENTRIES:
                    raise ValueError("converted comic is implausibly large")
                zf.write(path, member)
        tmp_out.replace(out)
    except BaseException:
        tmp_out.unlink(missing_ok=True)
        raise
    return written


def _convert(src: Path, key: str, fmt: str) -> None:
    """Do one conversion. Runs on a worker thread; never raises to the caller.

    Reads *src* and nothing else. The source file is opened by the extractor
    read-only and is never modified, moved or deleted -- on success or on
    failure, the only thing that changes on disk is this cache entry.
    """
    out_dir = _cache_root() / key
    out = out_dir / "comic.cbz"
    tmp = None
    reason = "conversion_failed"
    try:
        tool = find_extractor(fmt)
        if tool is None:
            reason = "no_extractor"
            raise RuntimeError(reason)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Inside the cache directory rather than the system temp dir: the CBZ
        # is renamed into place at the end and a rename across filesystems is
        # a copy, which for a gigabyte is the difference between instant and
        # not.
        tmp = Path(tempfile.mkdtemp(prefix="extract-", dir=str(out_dir)))

        ok, why = _run_extractor(tool, src, tmp)
        if not ok:
            reason = why
            raise RuntimeError(why)

        pages = _collect_pages(tmp)
        if not pages:
            # Unpacked fine but held no images: a CBR full of .txt, or an
            # extractor that silently produced nothing. Not an error.
            reason = "no_pages"
            raise RuntimeError(reason)

        count = _build_cbz(pages, out)
        (out_dir / "done.json").write_text(
            json.dumps({
                "key": key,
                "source": str(src),
                "format": fmt,
                "tool": tool[0],
                "pages": count,
                "created": int(time.time()),
            }),
            encoding="utf-8",
        )
        logger.info("[Comics] Repacked %s (%s, %s pages) with %s",
                    src.name, archive.FORMAT_LABELS.get(fmt, fmt), count, tool[0])
    except Exception as exc:
        # Deliberately NOT logger.error/.exception: every reason to land here
        # is a property of the file or of the machine (no extractor, a
        # password-protected archive, a truncated download), and an ERROR
        # record would be filed as a crash report by
        # telemetry.hooks._TelemetryLogHandler.
        logger.info("[Comics] Cannot convert %s: %s", src.name, reason)
        logger.debug("[Comics] Conversion detail for %s", src, exc_info=exc)
        with _lock:
            _failures[key] = (time.time(), reason)
        shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        with _lock:
            _jobs.pop(key, None)


# ---------------------------------------------------------------------------
# Peeking: one page, without unpacking the archive
# ---------------------------------------------------------------------------
# A cover is the FIRST PAGE of an archive and nothing else. Producing it
# through the conversion above means unpacking a whole 400 MB CBR and writing a
# second copy of it into the cache -- for a library of five thousand issues,
# five thousand complete unpacks and a duplicate of the entire shelf on disk,
# to end up with five thousand thumbnails. That cost is the reason CBR covers
# were in practice never made at all.
#
# Every extractor that can unpack an archive can also list it and pull exactly
# one member out of it, so that is what happens here: list, choose the first
# page by the SAME rules the reader uses (archive._is_page_name and
# archive._natural_key), extract that single member into a throwaway directory
# and read it. Using the same two functions is not a detail -- it is what
# guarantees the cover on the shelf is the page the reader opens as page 1.
#
# The safety rules are the module docstring's, unchanged and for the same
# reason: the member name comes out of a file from wherever the user got it,
# the extractor is an external program, and CVE-2018-20250 was precisely a
# member name that escaped its extraction directory. So the destination is a
# fresh mkdtemp under the cache, every produced path is resolved and proven to
# still be inside it, symlinks are dropped rather than followed, and the
# directory is removed in a finally.
#
# unar is deliberately absent from the table below. Its single-member
# extraction is not dependable across the builds in circulation, and a cover
# that is quietly the wrong page is worse than no cover -- so it answers None
# and the caller falls back to the full conversion.

# A peek must never feel like a conversion. Listing reads the archive's
# directory and extracting reads one member, both seconds' work, so the budget
# is deliberately a small fraction of _EXTRACT_TIMEOUT: a cover must never be
# able to block anything for minutes.
_PEEK_LIST_TIMEOUT = 20
_PEEK_EXTRACT_TIMEOUT = 30

# Mirrors covers._MAX_COVER_BYTES -- restated rather than imported, because
# covers.py imports this module. A "page" larger than this is not an image, and
# it would be read into memory in one piece.
_MAX_PAGE_BYTES = 24 * 1024 * 1024


def _peek_list_bsdtar(exe, src):
    return [exe, "-tf", str(src)]


def _peek_list_unrar(exe, src):
    # "lb" is the bare listing: names only, one per line, nothing to parse
    # around. -p- refuses to ask for a password instead of waiting for one.
    return [exe, "lb", "-p-", str(src)]


def _peek_list_7z(exe, src):
    # -slt prints one "Path = ..." per entry, which is the only 7-Zip listing
    # format that survives a member name containing spaces -- the columns of
    # the default listing do not. -ba drops the banner, -p is the same empty
    # password as in _cmd_7z.
    return [exe, "l", "-slt", "-ba", "-y", "-p", str(src)]


def _peek_names_plain(blob: str) -> list:
    """One name per line -- what bsdtar -tf and unrar lb both print."""
    return [line.strip() for line in blob.splitlines() if line.strip()]


def _peek_names_7z(blob: str) -> list:
    """The "Path = ..." lines of a 7-Zip -slt listing."""
    prefix = "Path = "
    return [line[len(prefix):].strip()
            for line in blob.splitlines() if line.startswith(prefix)]


def _peek_extract_bsdtar(exe, src, dest, member):
    # "--" ends the switches, so a member whose name begins with a dash is a
    # filename here and not an option.
    return [exe, "-x", "--no-same-owner", "-f", str(src),
            "-C", str(dest), "--", member]


def _peek_extract_7z(exe, src, dest, member):
    # "e" rather than "x": one member, no directory tree worth recreating. It
    # flattens the path away, which is why the result is looked up by name
    # afterwards instead of being assumed.
    return [exe, "e", "-y", "-bd", "-p", "-o{}".format(dest), str(src), member]


def _peek_extract_unrar(exe, src, dest, member):
    # The trailing separator is what makes unrar treat the destination as a
    # directory rather than as a filename prefix; see _cmd_unrar.
    return [exe, "x", "-y", "-idq", "-p-", str(src), member, str(dest) + os.sep]


# tool name -> (list argv builder, listing parser, extract argv builder).
# Keyed by the name find_extractor() reports, so this stays in step with
# _EXTRACTORS. A tool that is not in here simply cannot peek.
_PEEKERS = {
    "bsdtar": (_peek_list_bsdtar, _peek_names_plain, _peek_extract_bsdtar),
    "tar": (_peek_list_bsdtar, _peek_names_plain, _peek_extract_bsdtar),
    "7z": (_peek_list_7z, _peek_names_7z, _peek_extract_7z),
    "7za": (_peek_list_7z, _peek_names_7z, _peek_extract_7z),
    "unrar": (_peek_list_unrar, _peek_names_plain, _peek_extract_unrar),
}


def _run_peek(argv: list, timeout: int, cwd=None) -> tuple:
    """Run one short helper command. Returns (ok, decoded stdout).

    Never raises and never logs above debug: no tool, a tool that refuses, a
    password-protected archive and a hang all mean the same thing to the
    caller -- there is no cover to be had this way -- and an ERROR record here
    would be filed as a crash report by telemetry.hooks.
    """
    try:
        proc = subprocess.run(
            argv,                       # always a list; never shell=True
            shell=False,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,   # a password prompt must fail, not wait
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("[Comics] %s timed out after %ss while peeking", argv[0], timeout)
        return False, ""
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[Comics] Cannot run %s: %s", argv[0], exc)
        return False, ""
    blob = (proc.stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        logger.debug("[Comics] %s exited %s while peeking", argv[0], proc.returncode)
        return False, blob
    return True, blob


def _pick_extracted(root: Path, member: str):
    """The file the peek was after, or None. Containment-checked.

    Goes through _collect_pages, so the guarantees are exactly the conversion's:
    resolved, proven to still be inside *root*, symlinks dropped, and a name
    archive.py is willing to serve.

    What survives that is then still only accepted if it really is the member
    that was asked for. "Something came out" is not the same as "the right page
    came out": 7z's "e" flattens the path away, and both 7z and unrar read
    their file argument as a mask, so a name containing a wildcard can produce
    more than the one entry. A cover showing the wrong page is worse than no
    cover, so anything else answers None and the caller repacks properly.
    """
    wanted = member.replace("\\", "/")
    base = wanted.rsplit("/", 1)[-1]
    for path, produced in _collect_pages(root):
        if produced == wanted or produced.rsplit("/", 1)[-1] == base:
            return path
    logger.debug("[Comics] Peek produced nothing matching %r", member)
    return None


def extract_first_image(src, fmt=None) -> tuple:
    """(member name, bytes) of the first page image, without a full unpack.

    For the archives this process cannot open -- RAR and ACE -- and for one
    purpose: the cover. Returns ``(None, None)`` for everything it cannot do,
    which is a normal answer and never an exception: a native archive (read it
    directly), a machine with no extractor, a tool that cannot extract a single
    member, a password-protected or truncated file, an archive holding no
    images. The caller falls back to the full conversion, or to nothing.

    The page chosen is archive.list_pages()'s first, by construction: the
    listing is filtered through archive._is_page_name and sorted with
    archive._natural_key, the same two functions the reader uses.
    """
    try:
        return _extract_first_image(Path(src), fmt)
    except Exception:
        # Deliberately not error/exception: see the module docstring. Anything
        # that lands here is a property of the file or of the machine.
        logger.debug("[Comics] Could not peek into %s", src, exc_info=True)
        return None, None


def _extract_first_image(src: Path, fmt) -> tuple:
    fmt = fmt or archive.sniff(src)
    if fmt not in _EXTRACTORS:
        # Native, PDF or unrecognised: nothing to peek at, by definition.
        return None, None
    tool = find_extractor(fmt)
    if tool is None:
        return None, None           # a machine without unrar; already debug-logged
    name, exe, _builder = tool
    peeker = _PEEKERS.get(name)
    if peeker is None:
        logger.debug("[Comics] %s cannot extract a single member -- no peek for %s",
                     name, src.name)
        return None, None
    build_list, parse_names, build_extract = peeker

    ok, blob = _run_peek(build_list(exe, src), _PEEK_LIST_TIMEOUT)
    if not ok:
        return None, None
    listed = parse_names(blob)[:_MAX_ENTRIES]
    pages = sorted((n for n in listed if archive._is_page_name(n)),
                   key=archive._natural_key)
    if not pages:
        logger.debug("[Comics] Nothing that looks like a page listed in %s", src.name)
        return None, None
    member = pages[0]
    if member.startswith("-"):
        # 7z and unrar would read this as a switch. Rare enough to hand to the
        # full conversion instead of guessing at four tools' quoting rules.
        logger.debug("[Comics] Not peeking at a member that looks like a switch: %r",
                     member)
        return None, None

    tmp = None
    try:
        # Under the cache directory, never next to the library and never in the
        # working directory: this is where an external program is about to
        # write a file from a hostile archive.
        tmp = Path(tempfile.mkdtemp(prefix="peek-", dir=str(_cache_root())))
        ok, _blob = _run_peek(build_extract(exe, src, tmp, member),
                              _PEEK_EXTRACT_TIMEOUT, cwd=tmp)
        if not ok:
            return None, None
        found = _pick_extracted(tmp, member)
        if found is None:
            return None, None
        size = found.stat().st_size
        if size > _MAX_PAGE_BYTES:
            # Not read into memory at all. Informational, not an error: an
            # archive whose first page is 400 MB is malformed or hostile.
            logger.info("[Comics] First page of %s is %s bytes -- not reading it",
                        src.name, size)
            return None, None
        return member, found.read_bytes()
    except OSError as exc:
        logger.debug("[Comics] Cannot peek into %s: %s", src.name, exc)
        return None, None
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------

def conversion_status(src, start: bool = False) -> dict:
    """What can be done with this comic right now.

    Shaped like books.convert.conversion_status() and transcoder.thumbs_status()
    -- the client polls it, so every answer is either progress or terminal,
    never an exception. One of:

        {"ok": True,  "direct": True}                 PDF: pdf.js reads it
        {"ok": True,  "native": True}                 ZIP/TAR/7z: read in place
        {"ok": True,  "ready": True,  "key": ...}     a converted CBZ exists
        {"ok": True,  "pending": True}                a conversion is running
        {"ok": True,  "pending": False, ...}          convertible, not started
        {"ok": False, "reason": "no_extractor", "tool_hint": [...]}
        {"ok": False, "reason": ...}                  missing / too_large / ...

    With *start* true a conversion is kicked off in the background if one is
    needed and no other is already running for this file; see
    :func:`request_conversion`.
    """
    src = Path(src)
    if src.suffix.lower() not in COMIC_EXTS:
        return {"ok": False, "reason": "unsupported"}
    try:
        stat = src.stat()
    except OSError:
        return {"ok": False, "reason": "missing"}

    # The extension is a hint; the bytes decide. A .cbr that is really a ZIP
    # is read in place and never touches an extractor at all, which is the
    # single biggest reason this detour is needed less often than it looks.
    fmt = archive.sniff(src)
    if fmt in archive.DIRECT_FORMATS:
        return {"ok": True, "direct": True, "format": fmt}
    if archive.is_native(fmt):
        return {"ok": True, "native": True, "format": fmt}
    if fmt not in _EXTRACTORS:
        return {"ok": False, "reason": "unknown_format"}

    if stat.st_size > _MAX_SOURCE_BYTES:
        return {"ok": False, "reason": "too_large"}
    try:
        key = cache_key(src)
    except OSError:
        return {"ok": False, "reason": "unreadable"}

    if (_cache_root() / key / "done.json").is_file():
        return {"ok": True, "ready": True, "key": key, "format": fmt}

    tool = find_extractor(fmt)
    if tool is None:
        # The normal state on a machine without unrar. A clean answer, no
        # exception, and nothing above logger.debug -- see the module
        # docstring on why an ERROR here would file a crash report.
        return {
            "ok": False,
            "reason": "no_extractor",
            "format": fmt,
            "label": archive.FORMAT_LABELS.get(fmt, fmt),
            "tool_hint": list(_TOOL_HINTS.get(fmt, ())),
        }

    with _lock:
        failed = _failures.get(key)
        if failed and time.time() - failed[0] < _FAIL_TTL:
            return {"ok": False, "reason": failed[1], "format": fmt}
        if key in _jobs:
            return {"ok": True, "pending": True, "key": key, "format": fmt}
        if not start:
            return {"ok": True, "pending": False, "key": key, "format": fmt,
                    "tool": tool[0]}
        if len(_jobs) >= _MAX_PARALLEL:
            # Not an error: the client keeps polling and gets a slot shortly.
            return {"ok": True, "pending": True, "queued": True, "key": key}
        _failures.pop(key, None)
        _jobs[key] = True

    threading.Thread(target=_convert, args=(src, key, fmt),
                     daemon=True, name="comic-convert").start()
    return {"ok": True, "pending": True, "key": key, "format": fmt}


def request_conversion(src) -> dict:
    """Ask for this comic as a CBZ, starting the conversion if it is not there.

    The route-facing spelling of ``conversion_status(src, start=True)``: it
    returns immediately and the work happens on a background thread, because
    unpacking a gigabyte must not hold a request open.
    """
    return conversion_status(src, start=True)


def readable_source(src):
    """The path whose pages can actually be read for *src*, or None.

    ZIP/TAR/7z answer with themselves, a converted RAR/ACE with its cached
    CBZ, and everything else -- PDF, an unconverted archive, a missing
    extractor -- with None. Purely a lookup: it never starts a conversion, so
    it is safe to call while rendering a shelf of two thousand issues.
    """
    src = Path(src)
    fmt = archive.sniff(src)
    if archive.is_native(fmt):
        return src
    if fmt not in _EXTRACTORS:
        return None
    try:
        key = cache_key(src)
    except OSError:
        return None
    if not (_cache_root() / key / "done.json").is_file():
        return None
    try:
        out = converted_path(key)
    except ValueError:
        return None
    return out if out.is_file() else None


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def cache_stats() -> dict:
    """How much room the repacked comics are taking up: ``{"files": n, "bytes": n}``.

    The counterpart of covers.cache_stats(), and the number that actually
    matters of the two -- a converted volume is hundreds of megabytes where a
    cover is a couple of hundred kilobytes. Walked recursively because an
    entry is a directory (comic.cbz plus done.json, and possibly a leftover
    extract-* scratch directory from a conversion that was interrupted), and
    the point of the figure is what the cache costs on disk, not what it holds
    in finished CBZs.

    Never raises: a cache directory that cannot be read reads as empty.
    """
    files = 0
    total = 0
    try:
        root = _cache_root()
    except OSError:
        return {"files": 0, "bytes": 0}
    # os.walk() with the default onerror simply yields nothing for a directory
    # it cannot read, which is exactly the wanted behaviour here. Iterated
    # lazily rather than through list(), because pruning dirnames only has an
    # effect while the walk is still running.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Symlinked directories are not followed and not counted: their bytes
        # belong to whatever they point at, not to this cache.
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            entry = Path(dirpath) / name
            try:
                if entry.is_symlink():
                    continue
                total += entry.stat().st_size
                files += 1
            except OSError:
                continue
    return {"files": files, "bytes": total}


def cleanup_converted(max_age_days: int = 30) -> int:
    """Drop conversions nobody has opened in a while.

    Access time rather than creation time, so a series you re-read every month
    survives while a one-off preview does not. Called from the same daily
    worker that prunes the image cache and the converted books.
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
            with _lock:
                if entry.name in _jobs:
                    continue        # a conversion is writing into it right now
            cbz = entry / "comic.cbz"
            when = cbz.stat().st_atime if cbz.is_file() else entry.stat().st_mtime
            if when < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("[Comics] Removed %s stale conversion(s)", removed)
    return removed


def purge_orphans(known_paths=None) -> int:
    """Drop conversions whose source comic is gone.

    Age alone does not catch this: delete a 900 MB CBR and its repacked copy
    stays in the cache for a month, taking up as much room as the file the
    user deleted to free it. Every entry records the path it was made from, so
    the check is simply "does that file still exist".

    Pass *known_paths* (every comic the scanner currently sees) to also drop
    entries for files that still exist but have left the library -- a path
    removed from the library settings, or a share that is no longer mounted.
    Leave it None to only remove entries whose source is provably gone, which
    is the safe answer when the caller does not have the full picture.
    """
    wanted = None
    if known_paths is not None:
        wanted = set()
        for candidate in known_paths:
            try:
                wanted.add(str(Path(candidate).resolve()))
            except OSError:
                continue

    root = _cache_root()
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        with _lock:
            if entry.name in _jobs:
                continue
        meta = entry / "done.json"
        if not meta.is_file():
            # Unfinished or interrupted. Not an orphan by this definition;
            # cleanup_converted() sweeps it up by age.
            continue
        try:
            source = json.loads(meta.read_text(encoding="utf-8")).get("source") or ""
        except (OSError, ValueError):
            continue
        if not source:
            continue
        src = Path(source)
        try:
            gone = not src.exists()
            if not gone and wanted is not None:
                gone = str(src.resolve()) not in wanted
        except OSError:
            gone = True
        if gone:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("[Comics] Removed %s orphaned conversion(s)", removed)
    return removed
