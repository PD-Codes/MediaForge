"""Fernsehserien.de integration service.

Thin host-side wrapper around the vendored ``fernsehserien_scraper_single``
single-file scraper. fernsehserien.de has no official API and no crawlable
search endpoint, so this integration works differently from the Crunchyroll
one it mirrors:

* Crunchyroll offers a real (albeit unofficial) client/search API — a title
  can be looked up directly.
* Fernsehserien.de only exposes per-show pages addressed by a URL "slug"
  (e.g. "breaking-bad"). There is no reliable "search by title" here, so this
  module guesses the slug from a title with :func:`_slugify` and simply
  checks whether that page exists. A wrong guess just means no pill is shown
  (fail-safe: we never show wrong information, only sometimes no information).

Responsibilities:

* Build a single, lazily-created, rate-limited :class:`FernsehserienScraper`
  from the CineInfo settings (``fernsehserien_*`` keys, see :func:`_s`).
* Cache slug lookups process-wide (thread-safe) so repeat views don't re-hit
  the site, and so a bad slug guess isn't retried on every request.
* Provide small, defensive helpers the web layer consumes:
    - :func:`is_enabled`      – master switch check
    - :func:`test_connection` – verify the scraper still works (settings UI)
    - :func:`get_provider_info` / :func:`is_available` – "which streaming
      service carries this title (per fernsehserien.de)?", used to add a
      provider pill in the detail modal, same spot the Crunchyroll pill uses.

Every public function is failure-tolerant: a missing dependency, a blocked
request, or an unexpected page layout degrades to "no data" rather than
raising into the request handler.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
import re as _re
from typing import Any, Dict, Optional

from .db import (
    get_setting,
    get_provider_cache,
    set_provider_cache,
    clear_provider_cache,
)

logger = logging.getLogger(__name__)

# Vendored library — imported lazily-safe so the rest of the app keeps working
# even if the single-file bundle or its ``beautifulsoup4`` dependency is
# missing.
try:  # pragma: no cover - import guard
    from ..vendor.fernsehserien_scraper_single import (  # type: ignore
        FernsehserienScraper,
        FernsehserienError,
    )
    _IMPORT_OK = True
    _IMPORT_ERR: Optional[str] = None
except Exception as _exc:  # pragma: no cover - import guard
    FernsehserienScraper = None  # type: ignore
    FernsehserienError = Exception  # type: ignore
    _IMPORT_OK = False
    _IMPORT_ERR = str(_exc)

# A well-known slug used to verify the scraper still works (site layout
# hasn't changed, our user-agent isn't blocked, etc.) from the settings UI.
_TEST_SLUG = "breaking-bad"

_AVAIL_TTL = 24 * 3600  # cache slug lookups (positive AND negative) for 24 h

_scraper_lock = threading.Lock()
_scraper: Any = None
_scraper_delay: Optional[float] = None  # delay the cached instance was built with

# Slug/provider lookups are cached persistently (SQLite, same mechanism as the
# TMDB cache) so a restart doesn't lose 24h of work and a bad slug guess
# doesn't get retried on every process start.
_PROVIDER_CACHE_NS = "fernsehserien_avail"


# ── Settings helpers ──────────────────────────────────────────────────────────
def _s(key: str, default: str = "") -> str:
    return (get_setting("fernsehserien_" + key, default) or "").strip()


def is_enabled() -> bool:
    """Master switch: Fernsehserien integration active and importable."""
    return _IMPORT_OK and _s("enabled", "0") == "1"


def _delay() -> float:
    try:
        d = float(_s("delay", "1.5") or "1.5")
    except ValueError:
        d = 1.5
    # Never allow this to be tuned down to something aggressive — the scraper
    # is polite-by-default on purpose (see vendor file docstring).
    return max(1.0, d)


def _get_scraper() -> Any:
    """Return a cached scraper instance, rebuilding it if the delay setting changed."""
    global _scraper, _scraper_delay
    if not _IMPORT_OK:
        return None
    delay = _delay()
    with _scraper_lock:
        if _scraper is None or _scraper_delay != delay:
            _scraper = FernsehserienScraper(delay=delay)
            _scraper_delay = delay
        return _scraper


def invalidate_cache() -> None:
    """Drop all cached slug lookups (call after settings change / manual clear)."""
    clear_provider_cache(_PROVIDER_CACHE_NS)


# ── Slug guessing ──────────────────────────────────────────────────────────────
_UMLAUT_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
}


def _slugify(title: str) -> str:
    """Best-effort guess at a fernsehserien.de URL slug from a display title.

    fernsehserien.de slugs are typically the lowercased title with umlauts
    transliterated and everything but letters/digits collapsed to single
    hyphens (matches the pattern of e.g. "Breaking Bad" -> "breaking-bad").
    This is a guess, not a lookup — see the module docstring.
    """
    if not title:
        return ""
    out = title
    for src, repl in _UMLAUT_MAP.items():
        out = out.replace(src, repl)
    # Strip remaining diacritics (accented Latin letters etc.).
    out = unicodedata.normalize("NFKD", out)
    out = "".join(ch for ch in out if not unicodedata.combining(ch))
    out = out.lower()
    # Apostrophes and apostrophe-like marks (straight/curly quotes, backtick,
    # acute/grave accent, prime marks) are dropped outright, not turned into
    # a hyphen — fernsehserien.de renders "Duke's"/"Won't" as "dukes"/"wont",
    # not "duke-s"/"won-t".
    out = _re.sub(r"['‘’‛ʼ`´′ʹˊˋ]", "", out)
    out = _re.sub(r"[^a-z0-9]+", "-", out)
    return out.strip("-")


# ── Connection test (settings UI) ─────────────────────────────────────────────
def test_connection() -> Dict[str, Any]:
    """Verify the scraper still works against a known, stable page.

    Public scraping is inherently fragile (site layout / bot-blocking can
    change any time), so this just confirms the current build still parses a
    real page rather than validating any credentials (there are none).
    """
    if not _IMPORT_OK:
        return {"ok": False, "error": "library_unavailable", "detail": _IMPORT_ERR or ""}
    try:
        scraper = _get_scraper()
        info = scraper.show_info(_TEST_SLUG)
        return {"ok": True, "title": info.get("title"), "slug": _TEST_SLUG}
    except FernsehserienError as exc:
        return {"ok": False, "error": "request_failed", "detail": str(exc)}
    except Exception as exc:
        logger.debug("[Fernsehserien] test_connection error: %s", exc)
        return {"ok": False, "error": "unknown", "detail": str(exc)}


# ── Provider name cleanup ──────────────────────────────────────────────────────
# The vendored scraper's provider-name regex only stops at a following
# "Original-TV-Premiere" heading — verified live against fernsehserien.de,
# pages for streaming-only originals (Wednesday, Stranger Things, Squid Game,
# ...) have no such heading, so the raw capture runs to the end of the page
# and swallows the entire rest of the page text. Titles that DO have a
# following "Deutsche TV-Premiere" (as opposed to "Original-TV-Premiere")
# have the same problem the other way — the German provider name gets stuck
# together with that block too (e.g. "wow Deutsche TV-Premiere ... Sky
# Atlantic" for The Last of Us, ground-truth provider should just be "wow").
# Cut the capture at the first of these known landmarks instead of trusting
# the vendored regex's stop condition, then apply a hard length cap as a
# safety net in case the page layout changes again — a provider name is
# always a short label, never a paragraph.
_PROVIDER_STOP_MARKERS = (
    "original-tv-premiere",
    "original-streaming-premiere",
    "deutsche tv-premiere",
    "deutsche free-tv-premiere",
    "erhalte neuigkeiten",
)

# A few providers whose display name isn't just Title Case of the scraped
# (lowercased) name.
_PROVIDER_DISPLAY_NAMES = {
    "netflix": "Netflix",
    "disney+": "Disney+",
    "amazon prime video": "Amazon Prime Video",
    "prime video": "Prime Video",
    "wow": "WOW",
    "sky": "Sky",
    "joyn": "Joyn",
    "rtl+": "RTL+",
    "magenta tv": "MagentaTV",
    "magentatv": "MagentaTV",
    "paramount+": "Paramount+",
    "apple tv+": "Apple TV+",
}


def _clean_provider_name(raw: str) -> Optional[str]:
    name = (raw or "").strip()
    if not name:
        return None
    lowered = name.lower()
    cut = len(lowered)
    for marker in _PROVIDER_STOP_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    name = name[:cut].strip(" -")
    # Hard safety net: never surface a wall of text as a "provider" even if
    # none of the markers above match a future page-layout change.
    if not name or len(name) > 40 or "\n" in name:
        return None
    return _PROVIDER_DISPLAY_NAMES.get(name.lower(), name.title())


# ── Provider info / availability (pill) ───────────────────────────────────────
def get_provider_info(title: str) -> Dict[str, Any]:
    """Look up the German streaming premiere provider for ``title`` (cached).

    Returns ``{"available": bool, "provider": str | None, "date": str | None}``.
    ``available`` is True only when the guessed slug resolves to a real page
    AND that page names a streaming provider — a page existing without
    streaming-premiere data (e.g. TV-only shows) does not produce a pill,
    since there'd be nothing useful to show.
    """
    empty = {"available": False, "provider": None, "date": None}
    title = (title or "").strip()
    if not title or not is_enabled():
        return empty

    key = title.lower()
    cached = get_provider_cache(_PROVIDER_CACHE_NS, key, _AVAIL_TTL)
    if cached is not None:
        return dict(cached)

    slug = _slugify(title)
    result = dict(empty)
    if slug:
        scraper = _get_scraper()
        if scraper is not None:
            try:
                info = scraper.show_info(slug)
                premiere = info.get("german_streaming_premiere")
                if premiere and premiere.get("provider"):
                    provider = premiere["provider"] or {}
                    name = _clean_provider_name(provider.get("name") or "")
                    if name:
                        result = {
                            "available": True,
                            "provider": name,
                            "date": provider.get("firstEpisode"),
                        }
            except FernsehserienError as exc:
                # 404 (wrong slug guess) or a transient HTTP error — fail safe,
                # just cache the negative result so we don't retry every call.
                logger.debug("[Fernsehserien] %s -> %s: %s", title, slug, exc)
            except Exception as exc:
                logger.debug("[Fernsehserien] lookup failed for %r: %s", title, exc)

    set_provider_cache(_PROVIDER_CACHE_NS, key, result)
    return dict(result)


def is_available(title: str) -> bool:
    """True if fernsehserien.de names a streaming provider for ``title``."""
    return bool(get_provider_info(title).get("available"))
