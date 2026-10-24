"""Reading position API.

The video counterpart is routes/progress.py, and this mirrors it deliberately:
same route shape, same "user comes from the session, never from the body"
rule, same fail-soft attitude -- losing a position is an annoyance, refusing to
serve a page over it is a bug.

One difference matters. Watch progress is keyed by *file path*, because a video
file is the thing you watched. A book is not: the same novel routinely exists
as EPUB, MOBI and PDF at once (see web/books/), and someone who starts in the
EPUB and later opens the PDF has not started a different book. So the key here
is the book's grouping key, and switching format keeps your place.
"""
from flask import jsonify, request

from ..db import (
    add_reading_bookmark,
    delete_reading_bookmark,
    delete_reading_progress,
    get_reading_progress,
    get_reading_progress_bulk,
    list_reading_bookmarks,
    save_reading_progress,
)
from ..request_context import get_current_user_info
from ...logger import get_logger

logger = get_logger(__name__)

# A CFI can be long, but not arbitrarily so; anything past this is not a
# position but someone probing what the column accepts.
_MAX_LOCATION_LEN = 512
_MAX_BOOK_KEY_LEN = 512
_MAX_BULK = 200
# A bookmark's label is the chapter it sits in, not a note the reader typed.
_MAX_LABEL_LEN = 200


def _username():
    # get_current_user_info() returns a (username, is_admin) TUPLE. This used
    # to call .get() on it; the AttributeError was swallowed and every account
    # read and wrote the one shared '' bucket -- positions and bookmarks of
    # all users mixed together.
    try:
        username, _is_admin = get_current_user_info()
        return username or ""
    except Exception:
        return ""


# Rows written before the fix above all live under ''. Reads fall back to that
# bucket while a user has no row of their own, so nobody loses a place they
# had; every new save goes to the user's own row. With auth disabled the user
# IS '' and nothing changes.
_LEGACY_USER = ""


def register_reading_routes(app):
    """Registered from create_app, next to register_progress_routes."""

    @app.route("/api/reading/save", methods=["POST"])
    def api_reading_save():
        """Store where the reader currently is.

        POST /api/reading/save  {book, location, percent, kind}
        """
        data = request.get_json(silent=True) or {}
        book = str(data.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        location = str(data.get("location") or "")[:_MAX_LOCATION_LEN]
        if not book or not location:
            return jsonify({"error": "book and location are required"}), 400
        try:
            percent = float(data.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0.0
        percent = max(0.0, min(100.0, percent))
        try:
            save_reading_progress(book, location, percent, username=_username())
        except Exception:
            logger.exception("[Reading] Could not save the position for %s", book)
            return jsonify({"error": "could not save"}), 500
        return jsonify({"ok": True})

    @app.route("/api/reading/get")
    def api_reading_get():
        """GET /api/reading/get?book=<key> -> {location, percent, finished}"""
        book = str(request.args.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        if not book:
            return jsonify({})
        user = _username()
        row = get_reading_progress(book, username=user)
        if not row and user:
            row = get_reading_progress(book, username=_LEGACY_USER)
        return jsonify(row or {})

    @app.route("/api/reading/bulk", methods=["POST"])
    def api_reading_bulk():
        """POST /api/reading/bulk {books: [...]} -> {key: {...}}

        One request for a whole shelf: the library page would otherwise fire a
        request per card, which is what the video grid does through
        /api/progress/bulk for exactly the same reason.
        """
        data = request.get_json(silent=True) or {}
        books = data.get("books")
        if not isinstance(books, list):
            return jsonify({})
        keys = [str(b)[:_MAX_BOOK_KEY_LEN] for b in books[:_MAX_BULK] if b]
        user = _username()
        out = get_reading_progress_bulk(keys, username=user)
        if user:
            missing = [k for k in keys if k not in out]
            if missing:
                legacy = get_reading_progress_bulk(missing, username=_LEGACY_USER)
                for key, row in legacy.items():
                    out.setdefault(key, row)
        return jsonify(out)

    @app.route("/api/reading/reset", methods=["POST"])
    def api_reading_reset():
        """Forget the position for one book. POST /api/reading/reset {book}"""
        data = request.get_json(silent=True) or {}
        book = str(data.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        if not book:
            return jsonify({"error": "book is required"}), 400
        user = _username()
        delete_reading_progress(book, username=user)
        if user:
            # Otherwise the read fallback would bring the old shared position
            # straight back.
            delete_reading_progress(book, username=_LEGACY_USER)
        return jsonify({"ok": True})

    # ---- bookmarks -----------------------------------------------------
    # Separate from the position on purpose: the position is overwritten every
    # few seconds and means "where I stopped", a bookmark is chosen and means
    # "come back here". Nothing but an explicit delete may remove one.

    @app.route("/api/reading/bookmarks")
    def api_reading_bookmarks():
        """GET /api/reading/bookmarks?book=<key> -> {bookmarks: [...]}"""
        book = str(request.args.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        if not book:
            return jsonify({"bookmarks": []})
        try:
            user = _username()
            marks = list_reading_bookmarks(book, username=user)
            if user:
                seen = {m.get("location") for m in marks}
                marks += [m for m in list_reading_bookmarks(book, username=_LEGACY_USER)
                          if m.get("location") not in seen]
                marks.sort(key=lambda m: (m.get("percent") or 0, m.get("created_at") or ""))
            return jsonify({"bookmarks": marks})
        except Exception:
            logger.exception("[Reading] Could not list bookmarks for %s", book)
            return jsonify({"bookmarks": []})

    @app.route("/api/reading/bookmark", methods=["POST"])
    def api_reading_bookmark_add():
        """POST /api/reading/bookmark {book, location, kind, label, percent}"""
        data = request.get_json(silent=True) or {}
        book = str(data.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        location = str(data.get("location") or "")[:_MAX_LOCATION_LEN]
        if not book or not location:
            return jsonify({"error": "book and location are required"}), 400
        # The engine the location came from. A CFI is only meaningful to
        # epub.js; a page number is meaningful to the PDF and the comic
        # reader alike -- and the reader filters the list by this, so an
        # unknown value here is a bookmark that silently never shows up.
        raw_kind = str(data.get("kind") or "")
        kind = raw_kind if raw_kind in ("pdf", "comic") else "epub"
        label = str(data.get("label") or "")[:_MAX_LABEL_LEN]
        try:
            percent = float(data.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0.0
        try:
            stored = add_reading_bookmark(
                book, location, kind=kind, label=label,
                percent=max(0.0, min(100.0, percent)), username=_username(),
            )
        except Exception:
            logger.exception("[Reading] Could not add a bookmark to %s", book)
            return jsonify({"error": "could not save"}), 500
        if not stored:
            return jsonify({"error": "too_many"}), 409
        return jsonify({"ok": True})

    @app.route("/api/reading/bookmark/delete", methods=["POST"])
    def api_reading_bookmark_delete():
        """POST /api/reading/bookmark/delete {book, location}"""
        data = request.get_json(silent=True) or {}
        book = str(data.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        location = str(data.get("location") or "")[:_MAX_LOCATION_LEN]
        if not book or not location:
            return jsonify({"error": "book and location are required"}), 400
        try:
            user = _username()
            delete_reading_bookmark(book, location, username=user)
            if user:
                # A bookmark listed from the legacy bucket (see above).
                delete_reading_bookmark(book, location, username=_LEGACY_USER)
        except Exception:
            logger.exception("[Reading] Could not delete a bookmark from %s", book)
            return jsonify({"error": "could not delete"}), 500
        return jsonify({"ok": True})
