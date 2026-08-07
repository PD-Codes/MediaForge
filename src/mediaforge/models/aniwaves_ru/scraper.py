"""Scraping helpers for aniwaves.ru (a client-rendered "Zoro/9anime-style"
anime aggregator -- a different codebase than 9anime.or.at, but the same
overall shape).

Three different data-fetch layers on this site:

  * Series/episode pages (``/watch/<slug>-<id>``) render their body client-
    side (a React-ish SPA shell) -- a plain HTTP fetch never sees the title,
    genres, poster etc. in classic HTML tags the way 9anime's WordPress theme
    does. What IS server-rendered is a ``<script type="application/ld+json">``
    SEO block (schema.org ``TVSeries``) carrying everything this project
    needs (title, alternateName, description, image, genre, productionCompany,
    startDate/endDate, aggregateRating, numberOfEpisodes) -- see
    parse_series_meta(). The numeric series id is also just the trailing
    number in the URL itself (see series_id_from_url()), so a bare
    ``/watch/<id>`` (no slug) works too and is what episode.py's
    AniwavesEpisode.series uses to resolve its parent series without an
    extra "find the breadcrumb link" fetch (9anime needs that; aniwaves
    doesn't).

  * The episode list is fetched client-side from a legacy jQuery-style ajax
    endpoint that works fine without JS execution::

        GET /ajax/episode/list/<series_id>
        -> {"status", "result": "<html>"}

    ``result`` is itself a blob of HTML (``<a data-num data-sub data-dub>``
    per episode), same design choice as 9anime's ``pages`` field.

  * The per-episode hoster ("server") list is a second ajax call, keyed by
    the *same* series id plus the episode number (no separate episode id to
    resolve, unlike 9anime)::

        GET /ajax/server/list?servers=<series_id>&eps=<n>
        -> {"status", "result": "<html>"}

    Each ``<li data-sv-id data-link-id>`` is an opaque, site-encrypted token
    -- NOT a usable embed URL by itself (unlike 9anime's base64-encoded
    ``data-embed``). A *third* ajax call resolves one token to a real embed
    URL::

        GET /ajax/sources?id=<link_id>&asi=0&autoPlay=1
        -> {"status", "result": {"url": "<embed url>", "server": <int>, ...}}

    Three "servers" are offered per episode/language, labelled "Vidplay",
    and two auto-generated-looking names (seen as "BYFMS"/"DGHG") that both
    resolve into the same Byse-network mirrors filmo.to/filemoon.to already
    use -- see extractors/provider/echovideo.py's module docstring for why
    only "Vidplay" (-> extractors/provider/echovideo.py) is dispatched here.
    Because that third call costs a network round-trip per hoster, it is
    intentionally NOT done eagerly for every language here -- see
    models/aniwaves_ru/episode.py's provider_url, which only resolves the
    selected language's token, mirroring models/filmo_to/movie.py's
    _selected_chip laziness.

Site-wide listing cards (``/newest``, ``/trending``, ``/updated``) all share
one ``.ani.poster`` + ``.name.d-title`` markup -- see _parse_item_cards().
The ``/ajax/anime/search`` endpoint uses a different, simpler ``.item``
markup -- see _parse_search_cards().
"""
import json
import re
from html import unescape

try:
    from ...config import ANIWAVES_BASE_URL, logger, GLOBAL_SESSION
except ImportError:  # pragma: no cover
    from mediaforge.config import ANIWAVES_BASE_URL, logger, GLOBAL_SESSION

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}


class AniwavesUnavailable(Exception):
    """aniwaves.ru did not hand back a usable page/response.

    Own type (not a bare Exception) so callers can isolate "the source site
    had a bad day" the same way models/nineanime_to/scraper.py's
    NineAnimeUnavailable does.
    """


def base_url():
    return ANIWAVES_BASE_URL.rstrip("/")


def _get(path, params=None, timeout=15, ajax=False):
    headers = dict(_HEADERS)
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    resp = GLOBAL_SESSION.get(base_url() + path, params=params, headers=headers, timeout=timeout)
    if resp.status_code == 404:
        raise AniwavesUnavailable(f"Not found (HTTP 404): {path}")
    resp.raise_for_status()
    return resp.text


def fetch_page(url, timeout=15):
    resp = GLOBAL_SESSION.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        raise AniwavesUnavailable(f"Series not found (HTTP 404): {url}")
    resp.raise_for_status()
    return resp.text


def series_id_from_url(url):
    """The trailing numeric id in a /watch/<slug>-<id> or bare /watch/<id>
    URL -- aniwaves' own anime id, reused directly as the ajax params
    everything else here needs (no HTML fetch required to get it)."""
    m = re.search(r"/watch/(?:[a-zA-Z0-9\-]*-)?(\d+)/?$", url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Series metadata (JSON-LD -- see module docstring)
# ---------------------------------------------------------------------------
_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S
)


def _find_series_ld(html):
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "TVSeries":
            return data
    return None


def parse_series_meta(html):
    ld = _find_series_ld(html) or {}

    genres = [g for g in (ld.get("genre") or []) if isinstance(g, str)]

    studios = [
        p.get("name") for p in (ld.get("productionCompany") or [])
        if isinstance(p, dict) and p.get("name")
    ]

    score = None
    rating = ld.get("aggregateRating")
    if isinstance(rating, dict):
        try:
            score = float(rating.get("ratingValue"))
        except (TypeError, ValueError):
            score = None

    start_date = ld.get("startDate") or ld.get("datePublished") or ""
    year = start_date[:4] if start_date[:4].isdigit() else ""

    # Heuristic: schema.org TVSeries only carries an endDate once the show
    # has actually finished airing -- aniwaves omits the field entirely for
    # still-airing series (verified against several currently-airing pages
    # during research). No dedicated "status" field exists in the LD block.
    status = "Finished" if ld.get("endDate") else "Ongoing"

    return {
        "title": ld.get("name") or "",
        "title_alt": ld.get("alternateName") or "",
        "description": ld.get("description") or "",
        "poster_url": ld.get("image") or "",
        "genres": genres,
        "studios": studios,
        "status": status,
        "year": year,
        "score": score,
        "content_rating": ld.get("contentRating") or "",
        "episode_count": ld.get("numberOfEpisodes"),
        "series_id": series_id_from_url(ld.get("url") or ""),
    }


# ---------------------------------------------------------------------------
# Episode list
# ---------------------------------------------------------------------------
_EP_ITEM_RE = re.compile(
    r'<a href="([^"]+)"[^>]*data-num="(\d+)"[^>]*data-sub="(\d*)"[^>]*data-dub="(\d*)"[^>]*>'
    r'.*?<span class="d-title"[^>]*>([^<]*)</span>',
    re.S,
)


def fetch_episode_list_raw(series_id, timeout=15):
    """Raw {"status", "result": "<html>"} payload from the ajax episode-list
    route."""
    data = json.loads(_get(f"/ajax/episode/list/{series_id}", ajax=True, timeout=timeout))
    if not isinstance(data, dict):
        raise AniwavesUnavailable(f"Unexpected episode-list response for series {series_id}")
    return data


def parse_episode_list(payload):
    """[{"number", "url", "title", "has_sub", "has_dub"}, ...] in site order,
    sorted by episode number."""
    html = (payload or {}).get("result") or ""
    episodes = []
    for href, number, has_sub, has_dub, title in _EP_ITEM_RE.findall(html):
        try:
            num = int(number)
        except ValueError:
            continue
        episodes.append({
            "number": num,
            "url": base_url() + href if href.startswith("/") else href,
            "title": unescape(title).strip() or f"Episode {num}",
            "has_sub": has_sub == "1",
            "has_dub": has_dub == "1",
        })
    episodes.sort(key=lambda e: e["number"])
    return episodes


def fetch_episode_list(series_id, timeout=15):
    return parse_episode_list(fetch_episode_list_raw(series_id, timeout=timeout))


# ---------------------------------------------------------------------------
# Per-episode hoster ("server") list + per-server source resolution
# ---------------------------------------------------------------------------
_SERVER_TYPE_BLOCK_RE = re.compile(
    r'<div class="type" data-type="([a-z]+)">.*?<ul>(.*?)</ul>', re.S
)
_SERVER_ITEM_RE = re.compile(
    r'<li[^>]*data-sv-id="(\d+)"[^>]*data-link-id="([^"]+)"[^>]*>([^<]*)</li>'
)


def fetch_episode_servers(series_id, episode_number, referer=None, timeout=15):
    """{"sub": [...], "dub": [...], "kord": [...]} -- each entry
    {"name", "server_id", "link_id"}. *link_id* is an opaque token, not a
    usable URL yet -- see resolve_source()."""
    headers = dict(_HEADERS, **{"X-Requested-With": "XMLHttpRequest"})
    if referer:
        headers["Referer"] = referer
    resp = GLOBAL_SESSION.get(
        base_url() + "/ajax/server/list",
        params={"servers": series_id, "eps": episode_number},
        headers=headers, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data or data.get("status") != 200:
        return {"sub": [], "dub": [], "kord": []}
    html = (data.get("result") or "")

    result = {}
    for type_, block_html in _SERVER_TYPE_BLOCK_RE.findall(html):
        servers = []
        for server_id, link_id, name in _SERVER_ITEM_RE.findall(block_html):
            servers.append({
                "name": unescape(name).strip() or "Server",
                "server_id": server_id,
                "link_id": link_id,
            })
        result[type_] = servers
    return result


def resolve_source(link_id, referer=None, timeout=15):
    """Resolve one server's opaque *link_id* to its real embed URL."""
    headers = dict(_HEADERS, **{"X-Requested-With": "XMLHttpRequest"})
    if referer:
        headers["Referer"] = referer
    resp = GLOBAL_SESSION.get(
        base_url() + "/ajax/sources",
        params={"id": link_id, "asi": 0, "autoPlay": 1},
        headers=headers, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data or data.get("status") != 200:
        raise AniwavesUnavailable("aniwaves.ru: source resolution failed")
    url = ((data.get("result") or {}).get("url") or "")
    if not url:
        raise AniwavesUnavailable("aniwaves.ru: no embed URL in source response")
    return url


# ---------------------------------------------------------------------------
# Listing cards (newest/trending/updated) -- shared ".ani.poster"/".d-title" markup
# ---------------------------------------------------------------------------
_CARD_RE = re.compile(
    r'<div class="ani poster[^"]*"[^>]*>\s*<a href="([^"]+)">\s*<img src="([^"]+)"[^>]*alt="[^"]*">.*?'
    r'<a class="name d-title" href="[^"]+" data-jp="([^"]*)">([^<]*)</a>',
    re.S,
)


def _parse_item_cards(html, limit=None):
    cards = []
    for href, poster, jname, title in _CARD_RE.findall(html):
        cards.append({
            "title": unescape(title).strip(),
            "title_jp": unescape(jname).strip(),
            "url": base_url() + href if href.startswith("/") else href,
            "poster_url": poster,
        })
        if limit and len(cards) >= limit:
            break
    return cards


def fetch_new(limit=24):
    """"Neueste" -- the site's own /newest listing."""
    try:
        html = _get("/newest")
    except Exception as e:
        logger.warning("aniwaves new-episodes fetch failed: %s: %s", type(e).__name__, e)
        return None
    return _parse_item_cards(html, limit)


def fetch_popular(limit=24):
    """"Beliebte" -- the site's own /trending listing."""
    try:
        html = _get("/trending")
    except Exception as e:
        logger.warning("aniwaves popular fetch failed: %s: %s", type(e).__name__, e)
        return None
    return _parse_item_cards(html, limit)


# ---------------------------------------------------------------------------
# Search -- /ajax/anime/search uses a simpler, different ".item" markup
# ---------------------------------------------------------------------------
_SEARCH_ITEM_RE = re.compile(
    r'<a class="item" href="([^"]+)"><div class="poster"><span><img src="([^"]+)"/?></span></div>'
    r'<div class="info"><div class="name d-title" data-jp="([^"]*)">([^<]*)</div>'
)


def _parse_search_cards(html, limit=None):
    cards = []
    for href, poster, jname, title in _SEARCH_ITEM_RE.findall(html):
        cards.append({
            "title": unescape(title).strip(),
            "title_jp": unescape(jname).strip(),
            "url": base_url() + href if href.startswith("/") else href,
            "poster_url": poster,
        })
        if limit and len(cards) >= limit:
            break
    return cards


def search(keyword, limit=30):
    if not keyword:
        return []
    try:
        raw = _get("/ajax/anime/search", params={"keyword": keyword}, ajax=True)
        data = json.loads(raw)
    except Exception as e:
        logger.warning("aniwaves search failed for %r: %s: %s", keyword, type(e).__name__, e)
        return []
    if not data or data.get("status") != 200:
        return []
    html = ((data.get("result") or {}).get("html") or "")
    return _parse_search_cards(html, limit)
