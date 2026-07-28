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
    delete_reading_progress,
    get_reading_progress,
    get_reading_progress_bulk,
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


def _username():
    try:
        info = get_current_user_info() or {}
        return info.get("username") or ""
    except Exception:
        return ""


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
        return jsonify(get_reading_progress(book, username=_username()) or {})

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
        return jsonify(get_reading_progress_bulk(keys, username=_username()))

    @app.route("/api/reading/reset", methods=["POST"])
    def api_reading_reset():
        """Forget the position for one book. POST /api/reading/reset {book}"""
        data = request.get_json(silent=True) or {}
        book = str(data.get("book") or "")[:_MAX_BOOK_KEY_LEN]
        if not book:
            return jsonify({"error": "book is required"}), 400
        delete_reading_progress(book, username=_username())
        return jsonify({"ok": True})
