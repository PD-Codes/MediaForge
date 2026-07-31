"""One on-disk cache for derived cover images, and one worker that fills it.

A shelf is a wall of covers, and for most of what MediaForge shelves the cover
is not a file next to the book -- it is an image *inside* it. Producing it
means opening the container and reading one member, which is cheap once and
absurd per page view: two thousand issues would otherwise open two thousand
archives on every scroll. So the picture is written to a small on-disk cache
and served from there afterwards.

This module is that cache, with nothing media-specific in it. The comic shelf
had all of it (``comics/covers.py``), the book shelf had none of it, and a
third shelf -- music, manga -- would have made it three copies of the same
hundred lines. What actually differs between media is exactly two functions:
"pull the cover bytes out of this file" and "how long may that take", and both
belong to the caller.

Two pieces:

  * :class:`CoverCache` -- key, look up, downscale, store, prune, measure.
  * :class:`PrepareWorker` -- a background queue with a progress report the
    shelf can poll, so a cover that takes real work to produce arrives without
    anybody waiting on it and without a page load queueing three hundred jobs.

Cached by (path, mtime, size, version), the same identity comics/convert.py
and books/convert.py use: replace the file and the key stops matching, so a
cover can never belong to a different title than the one on screen.

NOTHING HERE EVER WRITES OUTSIDE THE CACHE DIRECTORY. The source file is only
ever opened for reading, and only by the extractor the caller supplies.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path

from ..logger import get_logger

logger = get_logger(__name__)

# What to send with a cached file. Guessing from the extension at each call
# site is how a WebP ends up labelled JPEG.
MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".avif": "image/avif",
}

# A cover is drawn into a grid card a few hundred pixels wide. The image it
# comes from is a scan or a publisher's press shot: 1600x2400 and two to five
# megabytes is ordinary. Kept at full size, a 5,000-item library would spend
# ~15 GB of cache on thumbnails nobody ever sees at that resolution.
#
# 640px on the long edge covers a 320px card at 2x device pixel ratio, which is
# the largest a poster tile gets on any display worth optimising for.
DEFAULT_MAX_EDGE = 640

# WebP over JPEG: roughly 30% smaller at the same visual quality, it keeps
# transparency, and every browser that can run this UI has supported it for
# years.
DEFAULT_QUALITY = 80

# The ceiling is not about picture quality, it is about memory: the image is
# read into RAM in one piece on a route any logged-in user can hit, and a file
# whose "cover" is 400 MB is either malformed or hostile.
DEFAULT_MAX_BYTES = 24 * 1024 * 1024


class CoverCache:
    """A flat directory of cached cover images, keyed by source identity.

    - ``subdir``: directory under the config dir. Give each media kind its own,
      so the two can be pruned and cleared independently -- their economics
      differ (a cover is 200 KB and worth keeping for years, a repacked archive
      is 400 MB and worth dropping after a month).
    - ``version``: part of the key. Bump it when what gets STORED changes;
      every existing entry then stops matching and is regenerated rather than
      being served forever in the old shape.
    - ``allowed_exts``: extensions that may become a filename in the cache
      directory. Checked on the way in, because that is what this value turns
      into.
    - ``log_prefix``: how this cache identifies itself in the log, e.g.
      "[Comics]".
    """

    def __init__(self, subdir, version, allowed_exts, log_prefix,
                 max_edge=DEFAULT_MAX_EDGE, quality=DEFAULT_QUALITY,
                 max_bytes=DEFAULT_MAX_BYTES):
        self.subdir = str(subdir)
        self.version = str(version)
        # Sorted, so a lookup that tries them in turn is deterministic across
        # platforms. ".webp" is always allowed: it is what _downscale writes,
        # whatever the source format was.
        self.allowed_exts = tuple(sorted(set(allowed_exts) | {".webp"}))
        self.log_prefix = str(log_prefix)
        self.max_edge = int(max_edge)
        self.quality = int(quality)
        self.max_bytes = int(max_bytes)

    # ---------------------------------------------------------------- paths
    def root(self) -> Path:
        """Where the files live: ``<config>/<subdir>/<key><ext>``.

        Flat files rather than a directory per cover: a large library produces
        one entry per item, and a directory per entry would multiply the inode
        cost of the cache by three for no gain.
        """
        try:
            from ..config import MEDIAFORGE_CONFIG_DIR
            base = Path(MEDIAFORGE_CONFIG_DIR)
        except Exception:
            base = Path(tempfile.gettempdir()) / "mediaforge"
        root = base / self.subdir
        root.mkdir(parents=True, exist_ok=True)
        return root

    def cache_key(self, path) -> str:
        """Identity of one cover: path + mtime + size + version.

        Raises OSError when the file is gone -- callers treat that as "no
        cover", which is also what it is.
        """
        path = Path(path)
        stat = path.stat()
        raw = "{}|{}|{}|{}".format(
            path.resolve(), int(stat.st_mtime), stat.st_size, self.version
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def cached(self, key: str):
        """The cached file for *key*, whatever image format it was stored in."""
        root = self.root()
        for ext in self.allowed_exts:
            candidate = root / (key + ext)
            if candidate.is_file():
                return candidate
        return None

    def has_cover(self, src) -> bool:
        """True if a cover for *src* is already cached.

        A pure lookup -- it never opens the source and never starts any work,
        so a shelf renderer may call it for every row it lists.
        """
        try:
            return self.cached(self.cache_key(src)) is not None
        except OSError:
            return False

    def mimetype(self, path) -> str:
        """The Content-Type for a cached file."""
        return MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")

    # --------------------------------------------------------------- writing
    def store_image(self, key: str, src, name: str, data: bytes):
        """Cache *data* as the cover for *key*. None if it is not fit to be one.

        The single way in, so the size ceiling and the extension check cannot
        end up applying to one caller and not to another.
        """
        src = Path(src)
        if not data:
            return None
        if len(data) > self.max_bytes:
            logger.info("%s Cover of %s is %s bytes, refusing to cache it",
                        self.log_prefix, src.name, len(data))
            return None

        ext = Path(name or "").suffix.lower()
        if ext not in self.allowed_exts:
            # Usually unreachable -- the extractors only yield known formats --
            # but checked anyway, because this becomes a filename.
            return None

        shrunk = self._downscale(data, src)
        if shrunk is not None:
            return self._store(key, ".webp", shrunk)
        return self._store(key, ext, data)

    def _downscale(self, data: bytes, src: Path):
        """Re-encode as a small WebP thumbnail. None if that is not possible.

        None is not a failure -- the caller then stores the original bytes.
        Pillow is a hard dependency of MediaForge, but an image can still be in
        a format it will not open, and a cover that is merely large beats no
        cover at all.
        """
        try:
            import io

            from PIL import Image
        except Exception:
            return None

        try:
            with Image.open(io.BytesIO(data)) as img:
                # One frame: an animated GIF used as a cover would otherwise be
                # re-encoded frame by frame for no reason.
                img.seek(0)
                if max(img.size) <= self.max_edge and len(data) < 120 * 1024:
                    # Already small: re-encoding would cost quality and save
                    # nothing worth having.
                    return None
                img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
                img.thumbnail((self.max_edge, self.max_edge), Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, format="WEBP", quality=self.quality, method=4)
                return out.getvalue()
        except Exception:
            # Truncated image, an exotic format, a decompression-bomb refusal --
            # all of it means "keep the original", none of it is an error.
            logger.debug("%s Could not downscale the cover of %s",
                         self.log_prefix, src.name, exc_info=True)
            return None

    def _store(self, key: str, ext: str, data: bytes):
        """Write bytes into the cache atomically. Returns the path or None.

        Through a uniquely named temporary file in the same directory and a
        rename, so two requests racing for the same uncached cover cannot
        produce a half-written file that the loser then serves.
        """
        root = self.root()
        target = root / (key + ext)
        tmp = root / "{}.{}-{}.part".format(key, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except OSError as exc:
            logger.info("%s Cannot cache cover %s: %s", self.log_prefix, key, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
        return target

    # ----------------------------------------------------------- maintenance
    def purge_orphans(self, known_paths) -> int:
        """Remove cached covers that no longer belong to anything.

        *known_paths* is every source the library currently holds. Each
        existing one is turned into its cache key, and every cache file whose
        name is not one of those keys goes -- which covers three cases at once:
        the source was deleted, it left the library, and it was edited (its
        mtime changed, so its old key is no longer produced by anything).

        This trusts the caller: an empty *known_paths* means an empty library
        and empties the cache. Deliberate and harmless -- covers are derived
        data and regenerate on the next visit -- but it does mean this must be
        called with a complete list, never with a partial scan.
        """
        keep = set()
        for candidate in known_paths or ():
            try:
                path = Path(candidate)
                if path.is_file():
                    keep.add(self.cache_key(path))
            except OSError:
                continue

        removed = 0
        try:
            entries = list(self.root().iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_file():
                continue
            # "<key>.jpg" and a leftover "<key>.1234-5678.part" both reduce to
            # the key, so an interrupted write is swept up by the same pass.
            # Split rather than Path.stem: stem strips one suffix and would
            # leave "<key>.1234-5678". A key is hexadecimal and contains no
            # dot, so everything before the first one is exactly the key.
            if entry.name.split(".", 1)[0] in keep:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info("%s Removed %s orphaned cover(s)", self.log_prefix, removed)
        return removed

    def stats(self) -> dict:
        """How much room this cache takes up: ``{"files": n, "bytes": n}``.

        Purely informational -- it is what the settings page shows next to the
        button that empties this cache, so pressing it is an informed decision
        rather than a leap in the dark. It never raises: a cache directory that
        cannot be listed reads as empty, which is also what it is worth here.
        """
        files = 0
        total = 0
        try:
            entries = list(self.root().iterdir())
        except OSError:
            return {"files": 0, "bytes": 0}
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                total += entry.stat().st_size
                files += 1
            except OSError:
                continue
        return {"files": files, "bytes": total}

    def cleanup(self, max_age_days: int = 180) -> int:
        """Drop covers nothing has asked for in a long time.

        Much more patient than a conversion cache: a cover is a couple of
        hundred kilobytes and rebuilding it means opening the source again, so
        the cheap thing is to keep it. This mainly exists so a library that was
        unmounted for half a year does not leave its covers behind forever.
        """
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        try:
            entries = list(self.root().iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.stat().st_atime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("%s Removed %s stale cover(s)", self.log_prefix, removed)
        return removed

    def clear(self) -> int:
        """Empty the cache completely. Returns how many files went."""
        removed = 0
        try:
            entries = list(self.root().iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_file():
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


class PrepareWorker:
    """A background queue that produces derived data, with a progress report.

    Why this exists: some covers can only be produced by real work -- repacking
    a RAR, converting a MOBI -- and the shelf route deliberately refuses to
    start that, because one page load must not queue three hundred jobs. The
    result was a shelf of blank cards that would never fill in. This is the
    missing half: it does the work deliberately, in the background, at a pace
    nobody has to wait on, and it says how far along it is so the shelf can
    show that instead of pretending everything is fine.

    - ``name``: the thread name, e.g. "comic-covers".
    - ``prepare_one``: ``fn(Path) -> bool``. True when the item now has what it
      needed. May block; that is the point of running here.
    - ``log_prefix``: how this worker identifies itself in the log.

    One worker thread at a time. A second submit() while it runs appends to the
    queue instead of starting a rival.
    """

    def __init__(self, name, prepare_one, log_prefix):
        self.name = str(name)
        self._prepare_one = prepare_one
        self.log_prefix = str(log_prefix)
        self._lock = threading.Lock()
        self._queue: list = []
        self._seen: set = set()
        self._state = {"running": False, "total": 0, "done": 0, "failed": 0,
                       "current": "", "finished_at": 0}

    def status(self) -> dict:
        """What the worker is doing, for the shelf's progress UI."""
        with self._lock:
            state = dict(self._state)
            state["pending"] = len(self._queue)
        return state

    def submit(self, paths) -> int:
        """Queue these files. Returns how many were newly queued.

        Safe to call repeatedly: anything already queued or already handled
        this run is skipped, so a rescan does not redo the work.
        """
        queued = 0
        with self._lock:
            for raw in paths or ():
                path = str(raw or "")
                if not path or path in self._seen:
                    continue
                self._seen.add(path)
                self._queue.append(path)
                queued += 1
            if not queued and not self._state["running"]:
                return 0
            self._state["total"] += queued
            if self._state["running"]:
                return queued
            self._state["running"] = True
            self._state["finished_at"] = 0

        threading.Thread(target=self._loop, name=self.name, daemon=True).start()
        return queued

    def _loop(self) -> None:
        """Drain the queue. Never raises -- a failure is one missing cover."""
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    path = self._queue.pop(0)
                    self._state["current"] = Path(path).name
                ok = False
                try:
                    ok = bool(self._prepare_one(Path(path)))
                except Exception:
                    logger.debug("%s Preparation failed for %s",
                                 self.log_prefix, path, exc_info=True)
                with self._lock:
                    self._state["done"] += 1
                    if not ok:
                        self._state["failed"] += 1
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["current"] = ""
                self._state["finished_at"] = int(time.time())

    def reset(self) -> None:
        """Forget what has been attempted. For tests and for a forced rescan."""
        with self._lock:
            self._seen.clear()
            del self._queue[:]
            self._state.update({"total": 0, "done": 0, "failed": 0,
                                "current": "", "finished_at": 0})
