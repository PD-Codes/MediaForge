"""Comic library API: page listing, page images, covers and conversion.

Split out of routes/library.py rather than added to it: that file is already
the video shelf plus the scan machinery plus the eBook endpoints, and the
comic half has its own container problem (five archive formats, two of which
need repacking first) that has nothing to do with any of it.

Every route here reads a file the CLIENT named, so all of them go through
`lib_resolve_library_file()` -- the same guard the eBook routes use, which
resolves the path and refuses anything that does not land inside a configured
scan target. That check is what stops "?path=/etc/passwd", and it runs before
anything is opened. On top of it, the page routes only ever serve a member
name that came back out of `archive.list_pages()`, which rejects traversal
inside the archive as well (see comics/archive.py).
"""
import hashlib
import os
import threading
from collections import OrderedDict

from flask import jsonify
from flask import request

from ..comics import archive
from ..comics import convert
from ..comics import covers
from ..media_types import COMIC_EXTS
from ...logger import get_logger

logger = get_logger(__name__)


# The page lists of the comics being read: (path, size, mtime_ns) ->
# (fmt, source, source_fmt, pages). The reader asks /pages once and then /page
# for every page, and each of those used to sniff the file, open the archive
# and read + naturally sort its whole directory, then open and parse it again
# for the one member: three opens and two directory reads per page turn, over
# SMB on most NAS setups. Size and mtime in the key make a file replaced at
# the same path a different entry. Only readable comics with pages are kept.
_INDEX_MAX = 32
_index = OrderedDict()
_index_lock = threading.Lock()


def _index_key(path):
    """The memo key for *path*, or None if it cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (str(path), st.st_size, st.st_mtime_ns)


def _index_get(key):
    if key is None:
        return None
    with _index_lock:
        hit = _index.get(key)
        if hit is not None:
            _index.move_to_end(key)
        return hit


def _index_put(key, value):
    if key is None:
        return
    with _index_lock:
        _index[key] = value
        _index.move_to_end(key)
        while len(_index) > _INDEX_MAX:
            _index.popitem(last=False)


def _index_drop(key):
    with _index_lock:
        _index.pop(key, None)


def _page_etag(key, source, index):
    """Validator for one page: the comic's identity, where its pages are read
    from (a converted copy's path carries the converter version) and the page."""
    raw = "{}|{}|{}|{}|{}".format(key[0], key[1], key[2], source, index)
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()[:32]


# Pages are addressed by ?path=&n=, a URL that stays the same when the file at
# that path is replaced. So no `immutable` and no day-long freshness (which kept
# showing a replaced issue's old pages for a day without ever asking): five
# minutes, like the covers, then a revalidation that the ETag answers with a
# 304 -- from the memo above, without opening the archive.
_PAGE_CACHE_CONTROL = "private, max-age=300, must-revalidate"


# Content types for the page images we are willing to serve. Derived from the
# member name, never from anything the client sent.
_PAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".avif": "image/avif",
}


def _resolve(param="path"):
    """The comic file the request names, or None.

    `lib_resolve_library_file` is imported lazily: routes/library.py imports a
    good part of the app at module scope, and importing it at the top here
    would make the two files circular.
    """
    from .library import lib_resolve_library_file
    return lib_resolve_library_file(request.args.get(param, "") or
                                    ((request.get_json(silent=True) or {}).get(param, "")),
                                    exts=COMIC_EXTS)


def _readable_source(path, fmt=None):
    """The file pages can actually be read from: the original for a native
    container, the cached CBZ for a RAR/ACE that has already been converted,
    or None when neither is available yet. *fmt* is the sniffed format of
    *path*, when the caller has it already."""
    if fmt is None:
        fmt = archive.sniff(path)
    if archive.is_native(fmt):
        return path, fmt
    converted = convert.readable_source(path)
    if converted is not None:
        return converted, archive.sniff(converted)
    return None, fmt


def register_comic_routes(app):
    """Register the comic library's API routes on the Flask app."""

    @app.route("/api/library/comic/pages")
    def api_comic_pages():
        """List the pages of one comic. GET /api/library/comic/pages?path=…

        Returns member names rather than URLs so the reader can address pages
        by index and does not have to round-trip a name it never needs.

        Called from static/reader.js's comic engine.
        """
        path = _resolve()
        if path is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        key = _index_key(path)
        hit = _index_get(key)
        if hit is not None:
            fmt, _source, _source_fmt, pages = hit
            return jsonify({"ok": True, "readable": True, "format": fmt,
                            "count": len(pages), "pages": pages})

        fmt = archive.sniff(path)
        if fmt in archive.DIRECT_FORMATS:
            # A PDF is not an archive: pdf.js reads it straight from
            # /api/library/book/file, and there is nothing to enumerate.
            return jsonify({"ok": True, "direct": True, "format": fmt,
                            "readable": True, "count": 0, "pages": []})

        source, source_fmt = _readable_source(path, fmt)
        if source is None:
            status = convert.conversion_status(path)
            reason = status.get("reason") or ("needs_conversion" if status.get("ok") else "unreadable")
            if status.get("ok") and not status.get("ready"):
                reason = "needs_conversion"
            elif (convert.is_retryable_failure(reason)
                  and convert.find_extractor(fmt) is not None):
                # The previous attempt failed for a circumstantial reason -- an
                # extractor that timed out while a library scan was hammering
                # the same disk, a share that blinked -- and this machine does
                # have a tool for the format. That is "not prepared yet", not
                # "unreadable": the reader offers the prepare button, and
                # pressing it now really does start a fresh attempt (see
                # conversion_status()'s handling of start=True).
                reason = "needs_conversion"
            return jsonify({"ok": False, "readable": False, "format": fmt,
                            "label": archive.FORMAT_LABELS.get(fmt, ""),
                            "reason": reason,
                            "tool_hint": status.get("tool_hint") or []})

        pages = archive.list_pages(source, source_fmt)
        if not pages:
            return jsonify({"ok": False, "readable": False, "format": fmt,
                            "reason": "no_pages", "count": 0, "pages": []})
        _index_put(key, (fmt, source, source_fmt, pages))
        return jsonify({"ok": True, "readable": True, "format": fmt,
                        "count": len(pages), "pages": pages})

    @app.route("/api/library/comic/page")
    def api_comic_page():
        """Serve one page image. GET /api/library/comic/page?path=…&n=<index>

        Addressed by INDEX, not by member name: the name would have to make a
        round trip through the URL and come back as user input, and an index
        cannot name a file at all. The name is looked up from the same sorted
        list the reader was given.

        Called from static/reader.js's comic engine.
        """
        from flask import Response
        path = _resolve()
        if path is None:
            return jsonify({"error": "not found"}), 404
        try:
            index = int(request.args.get("n", ""))
        except (TypeError, ValueError):
            return jsonify({"error": "bad page index"}), 400

        key = _index_key(path)
        hit = _index_get(key)
        while True:
            if hit is not None:
                _fmt, source, source_fmt, pages = hit
            else:
                fmt = archive.sniff(path)
                source, source_fmt = _readable_source(path, fmt)
                if source is None:
                    return jsonify({"error": "not readable"}), 409
                pages = archive.list_pages(source, source_fmt)
                if pages:
                    _index_put(key, (fmt, source, source_fmt, pages))
            if index < 0 or index >= len(pages):
                return jsonify({"error": "page out of range"}), 404

            name = pages[index]
            etag = _page_etag(key, source, index) if key is not None else None
            if etag is not None and request.if_none_match.contains_weak(etag):
                not_modified = Response(status=304)
                not_modified.set_etag(etag)
                not_modified.headers["Cache-Control"] = _PAGE_CACHE_CONTROL
                return not_modified
            data = archive.read_page(source, name, source_fmt)
            if data is not None or hit is None:
                break
            # A remembered source that does not read any more -- a converted
            # copy removed by "clear cache" -- is looked up afresh, once.
            _index_drop(key)
            hit = None
        if data is None:
            return jsonify({"error": "page unreadable"}), 404

        from pathlib import PurePosixPath
        mime = _PAGE_MIME.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")
        response = Response(data, mimetype=mime)
        # `private` because a library path is not public; see
        # _PAGE_CACHE_CONTROL for why this is no longer `immutable`.
        response.headers["Cache-Control"] = _PAGE_CACHE_CONTROL
        if etag is not None:
            response.set_etag(etag)
        return response

    @app.route("/api/library/comic/cover")
    def api_comic_cover():
        """Serve a comic's cover. GET /api/library/comic/cover?path=…

        The cover is the first page, extracted once and cached on disk (see
        comics/covers.py) so scrolling a shelf of 300 series does not reopen
        300 archives.

        Called from static/library_comics.js.
        """
        from flask import send_file
        path = _resolve()
        if path is None:
            return jsonify({"error": "not found"}), 404
        # start_conversion=False: a shelf render must never kick off 300
        # background repacks just because it drew 300 cards. resolved=True:
        # _resolve() already did, so the cache key skips a second realpath.
        cached = covers.cover_path(path, start_conversion=False, resolved=True)
        if cached is None:
            # A cover that does not exist YET is not a fact worth remembering.
            # Browsers heuristically cache a 404 that carries no caching
            # headers, so the miss a card collected before the background
            # worker got to it was served from the browser's own cache
            # afterwards -- the picture existed on disk and the shelf still
            # showed a blank tile until a hard reload. no-store is what makes
            # the next request actually ask.
            missing = jsonify({"error": "no cover"})
            missing.headers["Cache-Control"] = "no-store"
            return missing, 404
        response = send_file(str(cached), mimetype=covers.cover_mimetype(cached),
                             conditional=True)
        # A cover is not immutable the way a page inside an archive is: the
        # "Clear cover cache" button in Settings deletes it, and the URL
        # carries no content hash that would change when it does. A full day of
        # max-age meant the browser kept painting covers that no longer existed
        # on disk -- so the shelf looked fine while the settings page correctly
        # reported an empty cache, and nothing ever asked for them again, so
        # nothing was regenerated either.
        #
        # Five minutes plus must-revalidate keeps scrolling a shelf of hundreds
        # of cards free while bounding how long a deleted cover can survive.
        # send_file(conditional=True) above answers the revalidation with a 304
        # from the ETag, so this costs headers, not pictures.
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
        return response

    @app.route("/api/library/comic/convert", methods=["POST"])
    def api_comic_convert():
        """Start repacking a RAR/ACE comic into a cached CBZ.

        POST /api/library/comic/convert  {"path": "…"}

        The original file is never touched -- see comics/convert.py. Called
        from static/reader.js when the user presses "Prepare for viewing".
        """
        path = _resolve()
        if path is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(convert.request_conversion(path))

    @app.route("/api/library/comic/convert/status")
    def api_comic_convert_status():
        """Poll a conversion. GET /api/library/comic/convert/status?path=…

        Read-only: it reports, it never starts anything, so the reader can
        poll it without a press of the button turning into a repack loop.
        """
        path = _resolve()
        if path is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(convert.conversion_status(path, start=False))

    @app.route("/api/library/comic/cache")
    def api_comic_cache():
        """What the comic caches cost and what this machine can unpack.

        GET /api/library/comic/cache

        One request for the whole Comics block on the Library settings tab:
        the size of both caches (so "clear" is pressed with a number in view
        rather than blind) and which extractor was found per format. Admin
        only -- see _admin_only in web/app.py; it reports on server-wide state
        and its sibling below deletes files.

        Called from static/settings.js (loadComicCacheStats).
        """
        return jsonify({
            "ok": True,
            "covers": covers.cache_stats(),
            "converted": convert.cache_stats(),
            "extractors": convert.available_extractors(),
        })

    @app.route("/api/library/comic/cache/clear", methods=["POST"])
    def api_comic_cache_clear():
        """Empty one of the two comic caches.

        POST /api/library/comic/cache/clear  {"cache": "covers"|"converted"}

        Both caches hold nothing but derived data: a cover is re-extracted the
        next time a shelf is drawn and a conversion is redone the next time
        the comic is opened, so the worst case of pressing this is that the
        work happens again. The comic files themselves are never touched --
        the cleanup functions only ever look inside the config directory.

        Deliberately a whitelist of two names rather than a path or a
        directory: nothing the client sends may ever decide *what* gets
        deleted. Admin only (see _admin_only in web/app.py).
        """
        data = request.get_json(silent=True) or {}
        which = str(data.get("cache", "") or "").strip().lower()
        # max_age_days=0 puts the cutoff at "now", which is what makes these
        # the "empty it" spelling of the same functions the daily housekeeping
        # worker calls with 30/180.
        if which == "covers":
            removed = covers.cleanup_covers(max_age_days=0)
            stats = covers.cache_stats()
        elif which == "converted":
            removed = convert.cleanup_converted(max_age_days=0)
            stats = convert.cache_stats()
        else:
            return jsonify({"ok": False, "error": "unknown cache"}), 400
        logger.info("[Comics] Cleared the %s cache (%s entries removed)", which, removed)
        return jsonify({"ok": True, "cache": which, "removed": removed, "stats": stats})

    @app.route("/api/library/comic/covers/status")
    def api_comic_covers_status():
        """How far the background cover preparation has got.

        GET /api/library/comic/covers/status
        -> {running, total, done, failed, pending, current, finished_at}

        Read-only and cheap: the shelf polls it while covers are still being
        made, so it must not start anything or touch the disk.

        Called from static/library_comics.js.
        """
        return jsonify(covers.preparation_status())

    @app.route("/api/library/comic/tools")
    def api_comic_tools():
        """Which unpackers this server found. GET /api/library/comic/tools

        Surfaced so "CBR files cannot be opened" can be answered with what is
        actually missing instead of a shrug.
        """
        return jsonify({"extractors": convert.available_extractors()})
