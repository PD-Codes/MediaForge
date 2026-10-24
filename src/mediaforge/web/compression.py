"""Transparent gzip for text responses (HTML, JSON, CSS, JS, SVG).

The app used to send every page, every API answer and every script
uncompressed: the home page alone was ~160 KB of HTML plus ~2 MB of scripts
and stylesheets, and JSON listings such as /api/library run to megabytes.
Text compresses to roughly a quarter of its size, which is what decides how
fast the UI feels on a phone, over Wi-Fi or through a remote reverse proxy.

What is never touched:
  * streamed responses (Server-Sent Events, HLS, generators),
  * anything already encoded, partial (206/Range) or without a body,
  * file downloads other than /static (a book or video served with
    send_file stays a plain pass-through),
  * small bodies, where the gzip header would eat the gain.

Static files are compressed once per (file, mtime, size) and kept in a small
in-memory cache: they are the same bytes for every request, so compressing
them again on every first visit of every browser is wasted CPU on a NAS.
"""

from __future__ import annotations

import os
import threading
import zlib
from collections import OrderedDict

from flask import request

# Below this a gzip body is barely smaller than the original.
_MIN_SIZE = 1024
# Large dynamic bodies (multi-MB library listings) use the fastest level: on a
# LAN, compressing 8 MB at level 6 takes longer than sending it.
_FAST_LEVEL_FROM = 256 * 1024
_LEVEL = 6
_FAST_LEVEL = 1
_STATIC_LEVEL = 9  # done once per file and cached

_COMPRESSIBLE = frozenset({
    "text/html",
    "text/css",
    "text/plain",
    "text/javascript",
    "text/xml",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/manifest+json",
    "application/xml",
    "image/svg+xml",
})

# (path, mtime_ns, size) -> gzip bytes, least recently used first.
_STATIC_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_STATIC_CACHE_LIMIT = 48 * 1024 * 1024
_static_cache_bytes = 0
_static_lock = threading.Lock()


def _gzip(data: bytes, level: int) -> bytes:
    # wbits=31: gzip container with a zeroed mtime, so equal input gives
    # byte-identical output.
    comp = zlib.compressobj(level, zlib.DEFLATED, 31)
    return comp.compress(data) + comp.flush()


def _client_accepts_gzip() -> bool:
    header = request.headers.get("Accept-Encoding", "")
    if not header:
        return False
    for part in header.split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() not in ("gzip", "*"):
            continue
        params = params.replace(" ", "").lower()
        if params.startswith("q="):
            try:
                return float(params[2:]) > 0
            except ValueError:
                return False
        return True
    return False


def _static_gzip(path: str) -> bytes | None:
    """Gzip bytes of a static file, from the cache when unchanged."""
    global _static_cache_bytes
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    with _static_lock:
        hit = _STATIC_CACHE.get(key)
        if hit is not None:
            _STATIC_CACHE.move_to_end(key)
            return hit
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    data = _gzip(raw, _STATIC_LEVEL)
    with _static_lock:
        if key not in _STATIC_CACHE:
            # A changed file leaves its old entry behind under the old key;
            # drop it right away instead of waiting for it to age out.
            for old in [k for k in _STATIC_CACHE if k[0] == path]:
                _static_cache_bytes -= len(_STATIC_CACHE.pop(old))
            _STATIC_CACHE[key] = data
            _static_cache_bytes += len(data)
            while _static_cache_bytes > _STATIC_CACHE_LIMIT and len(_STATIC_CACHE) > 1:
                _, dropped = _STATIC_CACHE.popitem(last=False)
                _static_cache_bytes -= len(dropped)
    return data


def _weaken_etag(response) -> None:
    """The compressed body is a different representation, so a strong ETag
    must not be reused for it. A weak one still lets If-None-Match answer
    304 (that comparison is weak by definition)."""
    etag, weak = response.get_etag()
    if etag and not weak:
        response.set_etag(etag, weak=True)


def compress_response(response):
    """after_request hook: gzip the response when that is safe and useful."""
    try:
        if request.method == "HEAD":
            return response
        if response.status_code != 200:
            return response
        # A send_file() body has no length, so it counts as "streamed"; it is
        # handled (or skipped) below. Anything else streamed -- SSE, HLS,
        # generators -- is never buffered here.
        if response.is_streamed and not response.direct_passthrough:
            return response
        if "Content-Encoding" in response.headers or "Content-Range" in response.headers:
            return response
        mimetype = (response.mimetype or "").lower()
        if mimetype not in _COMPRESSIBLE:
            return response
        # Whatever the outcome below, the answer now depends on the header.
        response.vary.add("Accept-Encoding")
        if not _client_accepts_gzip():
            return response

        if response.direct_passthrough:
            # send_file() responses. Only /static is compressed: those files
            # are small, text and shared by everyone. Any other pass-through
            # (a book, a backup download) is left as it is.
            if request.endpoint != "static":
                return response
            from flask import current_app
            filename = (request.view_args or {}).get("filename") or ""
            static_folder = current_app.static_folder or ""
            path = os.path.realpath(os.path.join(static_folder, filename))
            if not path.startswith(os.path.realpath(static_folder) + os.sep):
                return response
            if (response.content_length or 0) < _MIN_SIZE:
                return response
            data = _static_gzip(path)
            if data is None:
                return response
            # Release the open file the pass-through body was going to stream.
            close = getattr(response.response, "close", None)
            if callable(close):
                close()
            response.direct_passthrough = False
            response.set_data(data)
        else:
            body = response.get_data()
            if len(body) < _MIN_SIZE:
                return response
            level = _FAST_LEVEL if len(body) >= _FAST_LEVEL_FROM else _LEVEL
            response.set_data(_gzip(body, level))

        response.headers["Content-Encoding"] = "gzip"
        _weaken_etag(response)
    except Exception:
        # Compression is an optimisation; a failure here must never cost the
        # response itself.
        from ..logger import get_logger
        get_logger(__name__).debug("gzip skipped", exc_info=True)
    return response


def init_compression(app) -> None:
    """Register the hook. Call this right after the Flask app is created:
    after_request hooks run in reverse order of registration, so the first
    one registered runs last -- after every other hook has finished
    changing the body and the headers."""
    if os.environ.get("MEDIAFORGE_DISABLE_COMPRESSION", "0") == "1":
        return
    app.after_request(compress_response)
