"""Short-lived, process-wide reuse of scraper page fetches (AniWorld, s.to).

The queue worker builds a fresh episode model for every item AND for every
hoster it falls back to, and each model fetched its own copy of the episode
page and -- through ``episode.series`` -- of the series page. A 24-episode
season meant 24 fetches of the identical series page, and every hoster fallback
two more identical fetches, all against sites behind DDoS-Guard/Cloudflare
(seconds each, and every request is another chance to trigger a challenge).

Only real pages are kept (HTTP 200 with a body), for two minutes, at most
``_MAX`` of them: a challenge or error page is fetched again by the next
caller instead of being served from here. The cache key includes the session
object, so a stubbed session in a test never sees another session's pages.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

_TTL = 120.0
_MAX = 32

# (id(session), url) -> (fetched_at, snapshot, session). The session itself is
# kept and compared by identity, so a recycled id() cannot hand one session's
# page to another.
_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_lock = threading.Lock()


class _Snapshot:
    """The parts of a response the page models read: status, text, headers."""

    __slots__ = ("status_code", "text", "headers", "url")

    def __init__(self, resp):
        self.status_code = resp.status_code
        self.text = resp.text
        try:
            encoding = resp.headers.get("Content-Encoding")
        except Exception:
            encoding = None
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.url = getattr(resp, "url", None)


def fetch_page(session, url):
    """``session.get(url)``, reused for a short while per URL (see above).

    Returns the live response on a miss that is not cacheable, a snapshot with
    ``status_code``/``text``/``headers``/``url`` otherwise.
    """
    key = (id(session), url)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[2] is session and now - hit[0] < _TTL:
            _cache.move_to_end(key)
            return hit[1]
    resp = session.get(url)
    if getattr(resp, "status_code", None) != 200:
        return resp
    try:
        snap = _Snapshot(resp)
    except Exception:
        return resp
    if not snap.text:
        return resp
    with _lock:
        _cache[key] = (time.monotonic(), snap, session)
        _cache.move_to_end(key)
        while len(_cache) > _MAX:
            _cache.popitem(last=False)
    return snap


def forget(url=None) -> None:
    """Drop the cached copy of *url* (every page when None)."""
    with _lock:
        if url is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[1] == url]:
            _cache.pop(key, None)
