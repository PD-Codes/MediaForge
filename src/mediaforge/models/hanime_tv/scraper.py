"""Central hanime.tv scraping/fetching layer (adult / 18+ source).

EVERYTHING hanime-specific that touches the network or parses hanime data
lives here.  If hanime changes its markup / API, this is the ONLY file that
should need adjusting.

hanime.tv is a JavaScript SPA, so the public listing pages contain no server
-rendered cards.  The data the app needs is, however, available as JSON that
the same site fetches -- we read those JSON payloads directly (this is still
"reading what the site serves", just the machine-readable variant).  Two
sources are used:

  * ``search.htv-services.com``  -> listing + search (new / trending / query)
  * ``<api>/video?id=<slug>``    -> a single video: metadata, the franchise's
                                    episode list, and the HLS stream manifest.

All returned "card" dicts share the shape used by the rest of MediaForge:
    {title, url, poster_url, genre, censored, franchise, is_series}
where ``url`` is a *series* URL (``/videos/hentai/<slug>``) and ``censored``
is one of "Censored" / "Uncensored" / "" (shown as a pill on the start page).
"""
import json
import re
import threading
import time
from html import unescape

try:
    from ...config import (
        HANIME_API_BASE,
        HANIME_BASE_URL,
        HANIME_SEARCH_URL,
        logger,
    )
except ImportError:  # pragma: no cover - allow running as a script
    from mediaforge.config import (
        HANIME_API_BASE,
        HANIME_BASE_URL,
        HANIME_SEARCH_URL,
        logger,
    )

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": HANIME_BASE_URL + "/",
    "Origin": HANIME_BASE_URL,
}

# hanime.tv/api/* sits behind Cloudflare and answers plain `requests` with a
# 404-style block, so we impersonate a real browser TLS fingerprint via
# curl_cffi (same approach the project uses for VeeV), falling back to plain
# requests only if curl_cffi is unavailable.  The separate search host
# (search.htv-services.com) is not gated, but using the same path is harmless.
_IMPERSONATE = "chrome120"


def _http_get(url, params=None, timeout=20):
    try:
        from curl_cffi import requests as curl_requests
        return curl_requests.get(url, params=params, headers=_HEADERS,
                                 impersonate=_IMPERSONATE, timeout=timeout,
                                 allow_redirects=True)
    except ImportError:
        import requests as _req
        return _req.get(url, params=params, headers=_HEADERS, timeout=timeout)


def _http_post(url, json_body, timeout=20):
    try:
        from curl_cffi import requests as curl_requests
        return curl_requests.post(url, json=json_body, headers=_HEADERS,
                                  impersonate=_IMPERSONATE, timeout=timeout)
    except ImportError:
        import requests as _req
        return _req.post(url, json=json_body, headers=_HEADERS, timeout=timeout)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def base_url():
    return HANIME_BASE_URL.rstrip("/")


def series_url(slug):
    """Canonical series URL for a video slug."""
    return f"{base_url()}/videos/hentai/{slug}"


def slug_from_url(url):
    """Extract the <slug> from a hanime series/episode URL."""
    m = re.search(r"/videos/hentai/([a-zA-Z0-9._\-]+)", url or "")
    return m.group(1) if m else ""


def _clean(s):
    return unescape(re.sub(r"\s+", " ", str(s or "")).strip())


# ---------------------------------------------------------------------------
# Censored / franchise helpers (adjust here if hanime renames fields)
# ---------------------------------------------------------------------------
def _censored_label(hit):
    """Return 'Censored' / 'Uncensored' / '' for a listing hit or video dict."""
    # 1. explicit boolean, if the payload carries one
    val = hit.get("is_censored")
    if isinstance(val, bool):
        return "Censored" if val else "Uncensored"
    # 2. derive from tags (search hits: list[str]; detail: list[{text}])
    tags = _tag_names(hit)
    low = {t.lower() for t in tags}
    if "uncensored" in low:
        return "Uncensored"
    if "censored" in low:
        return "Censored"
    return ""


def _tag_names(hit):
    tags = hit.get("tags") or hit.get("hentai_tags") or []
    names = []
    for t in tags:
        if isinstance(t, str):
            names.append(t)
        elif isinstance(t, dict):
            names.append(t.get("text") or t.get("name") or "")
    return [n for n in names if n]


def _display_tags(hit, limit=None):
    """Tag names for display (genre line + hover card), excluding the
    censorship-status tags ("censored"/"uncensored") — those are already
    surfaced via the dedicated hanime-pill (see _censored_label) and would
    just be a confusing duplicate in a genre list.
    """
    names = [t for t in _tag_names(hit) if t.lower() not in ("censored", "uncensored")]
    return names[:limit] if limit else names


_EP_SUFFIX_RE = re.compile(r"\s*[-–:]?\s*(?:ep(?:isode)?\.?\s*)?\d+\s*$", re.IGNORECASE)


def franchise_key(hit):
    """Best-effort franchise identity for grouping a listing.

    Uses an explicit franchise id/title when present, otherwise strips a
    trailing episode/chapter number off the name so that "Foo 1", "Foo 2",
    "Foo Episode 3" all collapse to "foo".
    """
    for k in ("hentai_franchise_id", "franchise_id"):
        if hit.get(k):
            return f"id:{hit[k]}"
    fr = hit.get("hentai_franchise") or hit.get("franchise")
    if isinstance(fr, dict) and (fr.get("slug") or fr.get("id")):
        return "id:" + str(fr.get("slug") or fr.get("id"))
    name = _clean(hit.get("name") or hit.get("title"))
    base = _EP_SUFFIX_RE.sub("", name).strip().lower()
    return "name:" + (base or name.lower())


def _poster(hit):
    return hit.get("poster_url") or hit.get("cover_url") or ""


def _hit_to_card(hit):
    slug = hit.get("slug") or ""
    name = _clean(hit.get("name") or hit.get("title"))
    display_tags = _display_tags(hit)
    genres = ", ".join(display_tags[:3])
    # Listing hits are per-episode ("Foo 2"), but a download folder is named
    # after the franchise (HanimeSeries.title strips the episode suffix — see
    # parse_meta). Ship that base title alongside the display title so the
    # frontend's "already downloaded" check has something that can actually
    # match a folder/library entry.
    series_title = _EP_SUFFIX_RE.sub("", name).strip() or name
    return {
        "title": name,
        "series_title": series_title,
        "url": series_url(slug) if slug else "",
        "poster_url": _poster(hit),
        "genre": genres,
        # Fuller tag list for the browse-card hover overlay (genres/FSK, see
        # renderBrowseHoverCards in app.js) — "genre" above stays a short
        # 3-tag summary for the subtitle line under the poster.
        "tags": display_tags[:8],
        "censored": _censored_label(hit),
        "franchise": franchise_key(hit),
        "is_series": True,
        "_slug": slug,
    }


def _group_by_franchise(hits):
    """Collapse per-episode hits into one card per franchise, keeping order.

    The first hit seen for a franchise (the listing is already sorted by the
    requested criterion) becomes the representative card.  Single-episode
    uploads simply stay as their own card.
    """
    out, seen = [], set()
    for hit in hits:
        card = _hit_to_card(hit)
        if not card["url"]:
            continue
        key = card["franchise"]
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


# ---------------------------------------------------------------------------
# Listing / search via search.htv-services.com
# ---------------------------------------------------------------------------
def _search_request(search_text="", order_by="created_at_unix", ordering="desc",
                    page=0, tags=None, blacklist=None):
    body = {
        "search_text": search_text or "",
        "tags": tags or [],
        "tags_mode": "AND",
        "brands": [],
        "blacklist": blacklist or [],
        "order_by": order_by,
        "ordering": ordering,
        "page": page,
    }
    resp = _http_post(HANIME_SEARCH_URL, body)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("hits")
    # hanime returns "hits" as a JSON-encoded string; be tolerant either way.
    if isinstance(hits, str):
        try:
            hits = json.loads(hits)
        except Exception:
            hits = []
    return hits or []


# Throttle for the "search backend is down" warning. A dead endpoint fails for
# every listing (new + trending) and for every page of each, so the same line
# used to appear a handful of times per page load. Once every 10 minutes is
# enough to notice it; the detail stays available at debug level.
_SEARCH_FAIL_LOG_INTERVAL = 600.0
_last_search_fail_log = 0.0


def _log_search_failure(what, exc):
    """Log a search-backend failure at most once per interval."""
    global _last_search_fail_log
    now = time.monotonic()
    if now - _last_search_fail_log > _SEARCH_FAIL_LOG_INTERVAL:
        _last_search_fail_log = now
        logger.warning(
            "hanime search backend unreachable (%s: %s) — set HANIME_SEARCH_URL "
            "if the endpoint moved. Current: %s",
            type(exc).__name__, str(exc)[:160], HANIME_SEARCH_URL,
        )
    else:
        logger.debug("hanime %s fetch failed: %s", what, exc)


_LISTING_TARGET_COUNT = 24  # aim for a full-looking grid
_LISTING_MAX_PAGES = 4      # politeness cap — stop even if still short of target


def _fetch_filtered(order_by, ordering, show_censored=True, show_uncensored=True):
    """Fetch a new/trending listing, applying the censored/uncensored content
    filter and backfilling from additional pages so filtering doesn't just
    leave a half-empty grid.

    The censorship filter has to happen here (before the page is "full")
    rather than after a single fixed-size page is fetched — otherwise ticking
    off "Zensiert" would simply remove matching cards from an already-short
    list instead of the caller getting a fresh page's worth of items that
    actually match. Franchise de-duplication (see _group_by_franchise) spans
    all fetched pages, not just one, so a franchise seen on page 0 can't
    reappear once page 1 is pulled in.
    """
    if show_censored and show_uncensored:
        # No filtering needed — one page is enough, same as before.
        try:
            hits = _search_request(order_by=order_by, ordering=ordering)
        except Exception as e:
            _log_search_failure(order_by, e)
            return None
        return _group_by_franchise(hits)

    cards, seen_franchise = [], set()
    fetched_any_page = False
    for page in range(_LISTING_MAX_PAGES):
        try:
            hits = _search_request(order_by=order_by, ordering=ordering, page=page)
        except Exception as e:
            _log_search_failure(f"{order_by} page {page}", e)
            break
        fetched_any_page = True
        if not hits:
            break
        for hit in hits:
            card = _hit_to_card(hit)
            if not card["url"]:
                continue
            key = card["franchise"]
            if key in seen_franchise:
                continue
            seen_franchise.add(key)
            c = card["censored"]
            if c == "Censored" and not show_censored:
                continue
            if c == "Uncensored" and not show_uncensored:
                continue
            cards.append(card)
        if len(cards) >= _LISTING_TARGET_COUNT:
            break
    if not fetched_any_page:
        return None
    return cards


def fetch_new(show_censored=True, show_uncensored=True):
    """Newest uploads, grouped into franchise cards."""
    return _fetch_filtered("created_at_unix", "desc", show_censored, show_uncensored)


def fetch_trending(show_censored=True, show_uncensored=True):
    """Most-viewed uploads (trending), grouped into franchise cards."""
    return _fetch_filtered("views", "desc", show_censored, show_uncensored)


def search(keyword):
    """Free-text search, grouped into franchise cards."""
    try:
        hits = _search_request(search_text=keyword, order_by="likes", ordering="desc")
    except Exception as e:
        logger.warning("hanime search failed: %s", e)
        return []
    return _group_by_franchise(hits)


# ---------------------------------------------------------------------------
# Single video detail  (<api>/video?id=<slug>)
# ---------------------------------------------------------------------------
def _slug_candidates(slug):
    """Alternate video-id forms to try if the primary slug 404s.

    Some listing slugs use a "-season-N" suffix that the video endpoint does
    not accept; try the "-N" and bare-base variants as a fallback.
    """
    cands = [slug]
    m = re.match(r"^(.*)-season-(\d+)$", slug)
    if m:
        for alt in (f"{m.group(1)}-{m.group(2)}", m.group(1)):
            if alt not in cands:
                cands.append(alt)
    return cands


# Since the Astro rewrite hanime signs every /api/v8/* request, so the video
# endpoint can only be reached through a real (signing) browser.  Browsing and
# search still go through the unsigned search host, so ONLY the per-title detail
# + stream need the browser.  Results are cached briefly so one modal open (which
# hits /api/series + /api/seasons + /api/episodes) spawns just one browser.
_video_cache = {}
_video_cache_ttl = 300.0  # seconds
_video_cache_lock = threading.Lock()


def _video_cache_get(slug):
    with _video_cache_lock:
        e = _video_cache.get(slug)
        if e and time.time() - e[0] < _video_cache_ttl:
            return e[1]
    return None


def _video_cache_put(slug, detail, m3u8):
    with _video_cache_lock:
        _video_cache[slug] = (time.time(), (detail, m3u8))


_video_inflight_locks = {}
_video_inflight_master = threading.Lock()


def _slug_lock(slug):
    with _video_inflight_master:
        lk = _video_inflight_locks.get(slug)
        if lk is None:
            lk = threading.Lock()
            _video_inflight_locks[slug] = lk
        return lk


def _get_video(cand, want_stream=False):
    """Return (detail, m3u8) for a candidate slug, cached, one browser at a time.

    ``want_stream`` triggers the poster/play click so the signed .m3u8 is
    captured; metadata-only calls skip it (faster).  A cached metadata result is
    reused, but a stream request re-runs the browser if no m3u8 was captured yet.
    """
    cached = _video_cache_get(cand)
    if cached is not None and (cached[1] or not want_stream):
        return cached
    with _slug_lock(cand):
        cached = _video_cache_get(cand)
        if cached is not None and (cached[1] or not want_stream):
            return cached
        try:
            from . import browser as _browser
            detail, m3u8 = _browser.fetch_video(cand, want_stream=want_stream)
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("hanime browser fetch_video(%s) failed: %s", cand, e)
            detail, m3u8 = {}, None
        if cached is not None:
            detail = detail or cached[0]
            m3u8 = m3u8 or cached[1]
        if detail or m3u8:
            _video_cache_put(cand, detail, m3u8)
        return (detail, m3u8)


def video_detail(slug):
    """Normalised video/metadata dict for a slug via the browser, or None."""
    if not slug:
        return None
    for cand in _slug_candidates(slug):
        detail, _m3u8 = _get_video(cand, want_stream=False)
        if detail:
            return detail
    logger.warning("hanime video_detail failed for %r", slug)
    return None


def stream_for_slug(slug):
    """Best HLS (.m3u8) URL for a video slug via the browser (clicks play)."""
    if not slug:
        return None
    for cand in _slug_candidates(slug):
        _detail, m3u8 = _get_video(cand, want_stream=True)
        if m3u8:
            return m3u8
    logger.warning("hanime stream_for_slug found nothing for %r", slug)
    return None


def parse_meta(detail):
    """Series-level metadata from the normalised browser detail dict."""
    detail = detail or {}
    title = _clean(detail.get("title") or "")
    title = _EP_SUFFIX_RE.sub("", title).strip() or title
    return {
        "title": title,
        "description": _clean(detail.get("description") or ""),
        "poster_url": detail.get("poster_url") or "",
        "genres": detail.get("genres") or [],
        "year": detail.get("year") or "",
        "censored": detail.get("censored") or "",
        "views": detail.get("views") or 0,
    }


def franchise_episodes(detail):
    """Ordered episode dicts {slug, name, censored} from the normalised detail.

    browser.py's page scrape collects every ``a[href*="/videos/hentai/"]``
    link on the video page, which also picks up unrelated "related/
    recommended videos" widgets elsewhere on the page — not just the real
    franchise episode list. That pollutes the ordered list this returns, and
    since HanimeEpisode.episode_slug indexes into it by position (`eps[idx]`),
    a stray unrelated link doesn't just look wrong in the UI, it can make
    "Episode 1" actually resolve to and stream/download a completely
    different video.

    Guard against this the same way listing hits are grouped into franchise
    cards (see franchise_key()/_EP_SUFFIX_RE above): strip a trailing episode
    number off both the series' own title and each candidate's name, and only
    keep entries whose stripped base title agrees with the series' — this
    doesn't depend on hanime's exact page markup, just on titles matching
    once episode numbering is ignored. If the series title is missing
    (extraction failed) nothing is filtered, since guessing wrong would drop
    real episodes.
    """
    detail = detail or {}
    series_title = _clean(detail.get("title") or "")
    series_base = _EP_SUFFIX_RE.sub("", series_title).strip().lower()

    out = []
    for v in detail.get("episodes") or []:
        slug = v.get("slug")
        if not slug:
            continue
        name = _clean(v.get("name"))
        ep_base = _EP_SUFFIX_RE.sub("", name).strip().lower()
        if series_base and ep_base and series_base != ep_base \
                and ep_base not in series_base and series_base not in ep_base:
            continue  # different franchise entirely — drop it
        out.append({"slug": slug, "name": name, "censored": v.get("censored") or ""})
    return out


def best_stream(detail):
    """Stream URL from the normalised detail (raw-manifest fallback kept)."""
    detail = detail or {}
    if detail.get("m3u8"):
        return detail["m3u8"]
    manifest = detail.get("videos_manifest") or {}
 