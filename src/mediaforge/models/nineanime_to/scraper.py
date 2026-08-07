"""Scraping helpers for 9anime.or.at (WordPress "9animetv" theme).

Three different data-fetch shapes on this one site:

  * Series pages (``/anime/<slug>/``) are plain server-rendered HTML --
    title/genres/description/poster/studios/score are scraped straight out
    of the definition list, same as any classic site. The page also carries
    a ``data-series="<id>"`` marker the theme's own JS uses for the next
    call.

  * The episode list is NOT in that HTML. The theme loads it client-side
    from its own REST route::

        GET /wp-json/9animetv/v1/episodes/<series_id>?active=0
        -> {"status", "total", "ranges": "<html>", "pages": "<html>"}

    ``pages`` is itself a blob of HTML (``<a data-number data-id>`` per
    episode) rather than JSON -- that's the theme's choice, not ours; see
    parse_episode_list().

  * The per-episode hoster list is a THIRD fetch, keyed by the episode id
    from the step above::

        GET /ajax/episode/servers?id=<episode_id>
        -> {"status", "html": "<...data-embed=base64(hoster url)...>"}

    Each ``data-embed`` is base64 of the real hoster URL (currently only
    seen pointing at megaplay.buzz -- see extractors/provider/megaplay.py),
    grouped into a SUB block and/or a DUB block.

Site-wide listing cards (homepage rows, search results) all share one
``.flw-item`` markup -- see _parse_flw_cards(). The "Top Anime" (most
viewed) widget uses a different, ranked markup -- see _parse_top_anime().
"""
import base64
import re
from html import unescape

try:
    from ...config import NINEANIME_BASE_URL, logger, GLOBAL_SESSION
except ImportError:  # pragma: no cover
    from mediaforge.config import NINEANIME_BASE_URL, logger, GLOBAL_SESSION

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}


class NineAnimeUnavailable(Exception):
    """9anime.or.at did not hand back a usable page/response.

    Own type (not a bare Exception) so callers can isolate "the source site
    had a bad day" the same way megakino_to.scraper.MegakinoUnavailable and
    models/filmo_to/scraper.FilmoUnavailable already do.
    """


def base_url():
    return NINEANIME_BASE_URL.rstrip("/")


def _get(path, params=None, timeout=15, ajax=False):
    headers = dict(_HEADERS)
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    resp = GLOBAL_SESSION.get(base_url() + path, params=params, headers=headers, timeout=timeout)
    if resp.status_code == 404:
        raise NineAnimeUnavailable(f"Not found (HTTP 404): {path}")
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Series metadata
# ---------------------------------------------------------------------------
_SERIES_ID_RE = re.compile(r'block_area-episodes\[?[^>]*data-series="(\d+)"')
_TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>\s*</h2>|<h2[^>]*>(.*?)</h2>', re.S)
_DT_DD_RE = re.compile(r"<div[^>]*class=\"[^\"]*item\b[^\"]*\"[^>]*>\s*<div[^>]*item-title[^>]*>([^<]+)</div>\s*<div[^>]*item-content[^>]*>(.*?)</div>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SCORE_RE = re.compile(r"([\d.]+)")
_POSTER_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*itemprop="image"|<meta property="og:image" content="([^"]+)"')


def _clean_text(fragment):
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def series_id_from_html(html):
    m = re.search(r'data-series="(\d+)"', html)
    return m.group(1) if m else None


def fetch_page(url, timeout=15):
    resp = GLOBAL_SESSION.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        raise NineAnimeUnavailable(f"Series not found (HTTP 404): {url}")
    resp.raise_for_status()
    return resp.text


def parse_series_meta(html):
    title = ""
    m = re.search(r'<h2[^>]*class="[^"]*film-name[^"]*"[^>]*>(.*?)</h2>', html, re.S)
    if not m:
        m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.S)
    if m:
        title = _clean_text(m.group(1))

    jname = ""
    m = re.search(r'class="film-name dynamic-name" data-jname="([^"]*)"', html)
    if m:
        jname = unescape(m.group(1)).strip()

    description = ""
    m = re.search(r'<div[^>]*class="[^"]*film-description[^"]*"[^>]*>(.*?)</div>', html, re.S)
    if m:
        description = _clean_text(m.group(1))

    poster_url = ""
    m = re.search(r'<div class="film-poster">\s*<img src="([^"]+)"', html)
    if not m:
        m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        poster_url = m.group(1)

    fields = {}
    for label, content in re.findall(
        r'<div class="item-title">([^<]+):?</div>\s*<div class="item-content">(.*?)</div>', html, re.S,
    ):
        fields[label.strip().rstrip(":")] = _clean_text(content)

    genres = []
    m = re.search(r'<div class="item-title">Genre:?</div>\s*<div class="item-content">(.*?)</div>', html, re.S)
    if m:
        genres = [unescape(g.strip()) for g in re.findall(r">([^<]+)</a>", m.group(1))]

    studios = []
    m = re.search(r'<div class="item-title">Studios:?</div>\s*<div class="item-content">(.*?)</div>', html, re.S)
    if m:
        studios = [unescape(s.strip()) for s in re.findall(r">([^<]+)</a>", m.group(1))]

    score = None
    raw_score = fields.get("Scores") or fields.get("Score")
    if raw_score:
        sm = _SCORE_RE.search(raw_score)
        if sm:
            try:
                score = float(sm.group(1))
            except ValueError:
                score = None

    year = ""
    ym = re.search(r"\b(19|20)\d{2}\b", fields.get("Date aired", ""))
    if ym:
        year = ym.group(0)

    return {
        "title": title,
        "jname": jname,
        "description": description,
        "poster_url": poster_url,
        "genres": genres,
        "studios": studios,
        "status": fields.get("Status", ""),
        "date_aired": fields.get("Date aired", ""),
        "year": year,
        "score": score,
        "series_id": series_id_from_html(html),
    }


# ---------------------------------------------------------------------------
# Episode list
# ---------------------------------------------------------------------------
_EP_ITEM_RE = re.compile(
    r'<a href="([^"]+)" title="([^"]*)" class="item ep-item" data-number="(\d+)" data-id="(\d+)"'
)


def fetch_episode_list_raw(series_id, timeout=15):
    """Raw {"status","total","ranges","pages"} payload from the theme's own
    episode-list REST route."""
    path = f"/wp-json/9animetv/v1/episodes/{series_id}"
    resp = GLOBAL_SESSION.get(base_url() + path, params={"active": 0}, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise NineAnimeUnavailable(f"Unexpected episode-list response for series {series_id}")
    return data


def parse_episode_list(payload):
    """[{"number", "id", "url", "title"}, ...] in site order, sorted by
    episode number (the REST route can return them out of numeric order --
    see the raw One Piece response captured during research, which listed
    54, 57, 58 before jumping to 1160+)."""
    pages_html = (payload or {}).get("pages") or ""
    episodes = []
    for href, title, number, ep_id in _EP_ITEM_RE.findall(pages_html):
        try:
            num = int(number)
        except ValueError:
            continue
        episodes.append({
            "number": num,
            "id": ep_id,
            "url": unescape(href),
            "title": unescape(title) or f"Episode {num}",
        })
    episodes.sort(key=lambda e: e["number"])
    return episodes


def fetch_episode_list(series_id, timeout=15):
    return parse_episode_list(fetch_episode_list_raw(series_id, timeout=timeout))


# ---------------------------------------------------------------------------
# Per-episode hoster ("server") list
# ---------------------------------------------------------------------------
_SERVER_BLOCK_RE = re.compile(
    r'ps_-block-(sub|dub)[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S
)
_SERVER_ITEM_RE = re.compile(
    r'data-type="(sub|dub)"\s*data-id="[^"]*"\s*data-server-id="(\d+)"\s*data-embed="([^"]+)"[^>]*>\s*'
    r'<a[^>]*class="btn"[^>]*>([^<]*)</a>',
)


def fetch_episode_servers(episode_id, referer=None, timeout=15):
    """{"sub": [{"name","embed_url"}], "dub": [...]} for one episode.

    *referer* should be the episode page URL -- observed to matter for the
    ajax call itself in earlier testing of the sibling /n-style endpoints on
    other sites; harmless to include here even if 9anime doesn't enforce it.
    """
    headers = dict(_HEADERS, **{"X-Requested-With": "XMLHttpRequest"})
    if referer:
        headers["Referer"] = referer
    # Trailing slash on purpose: WordPress 301-redirects the bare path to
    # this one, and hitting it directly saves that extra round trip.
    resp = GLOBAL_SESSION.get(
        base_url() + "/ajax/episode/servers/", params={"id": episode_id},
        headers=headers, timeout=timeout, allow_redirects=True,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data or not data.get("status"):
        return {"sub": [], "dub": []}
    html = data.get("html") or ""

    result = {"sub": [], "dub": []}
    for type_, server_id, embed_b64, name in _SERVER_ITEM_RE.findall(html):
        try:
            embed_url = base64.b64decode(embed_b64).decode("utf-8", "ignore")
        except Exception:
            continue
        if not embed_url:
            continue
        result.setdefault(type_, []).append({
            "name": unescape(name).strip() or "Server",
            "server_id": server_id,
            "embed_url": embed_url,
        })
    return result


# ---------------------------------------------------------------------------
# Listing cards (homepage rows, search results) -- shared ".flw-item" markup
# ---------------------------------------------------------------------------
_FLW_CARD_RE = re.compile(
    r'<div class="flw-item[^"]*"[^>]*>(.*?)<div class="clearfix"></div>\s*</div>', re.S
)
_FLW_HREF_RE = re.compile(r'<a href="([^"]+)" class="film-poster-ahref"')
_FLW_TITLE_RE = re.compile(r'<h3 class="film-name"><a[^>]*title="([^"]*)"')
_FLW_IMG_RE = re.compile(r'<img[^>]+data-src="([^"]+)"')


def _parse_flw_cards(html, limit=None):
    cards = []
    for m in _FLW_CARD_RE.finditer(html):
        body = m.group(1)
        href_m = _FLW_HREF_RE.search(body)
        title_m = _FLW_TITLE_RE.search(body)
        img_m = _FLW_IMG_RE.search(body)
        if not href_m:
            continue
        cards.append({
            "title": unescape(title_m.group(1).strip()) if title_m else "",
            "url": href_m.group(1),
            "poster_url": img_m.group(1) if img_m else "",
        })
        if limit and len(cards) >= limit:
            break
    return cards


_TOP_ANIME_TAB_RE = r'id="{tab}"[^>]*class="[^"]*anime-block-ul[^"]*"[^>]*>(.*?)</div>\s*(?:<div id="|</div>\s*</div>\s*</div>)'
_TOP_ITEM_RE = re.compile(
    r'<div class="film-poster[^"]*"[^>]*>\s*<img[^>]+data-src="([^"]+)".*?'
    r'<a href="([^"]+)"\s*\n?\s*title="([^"]*)"',
    re.S,
)


def _parse_top_anime(html, tab="top-viewed-day", limit=None):
    m = re.search(_TOP_ANIME_TAB_RE.format(tab=re.escape(tab)), html, re.S)
    section = m.group(1) if m else html
    cards = []
    for poster, href, title in _TOP_ITEM_RE.findall(section):
        cards.append({"title": unescape(title.strip()), "url": href, "poster_url": poster})
        if limit and len(cards) >= limit:
            break
    return cards


def fetch_new(limit=24):
    """"Neueste" -- homepage "Recently Updated" row (links to the newest
    episode of each recently-updated series, same as the site's own
    homepage presents it)."""
    try:
        html = _get("/")
    except Exception as e:
        logger.warning("9anime new-episodes fetch failed: %s: %s", type(e).__name__, e)
        return None
    m = re.search(r"Recently Updated</h2>.*?film_list-wrap\">(.*?)</section>", html, re.S)
    section = m.group(1) if m else html
    return _parse_flw_cards(section, limit)


def fetch_popular(limit=24):
    """"Beliebte" -- homepage "Top Anime" widget, daily-most-viewed tab."""
    try:
        html = _get("/")
    except Exception as e:
        logger.warning("9anime popular fetch failed: %s: %s", type(e).__name__, e)
        return None
    return _parse_top_anime(html, tab="top-viewed-day", limit=limit)


def search(keyword, limit=30):
    if not keyword:
        return []
    try:
        html = _get("/", params={"s": keyword})
    except Exception as e:
        logger.warning("9anime search failed for %r: %s: %s", keyword, type(e).__name__, e)
        return []
    m = re.search(r'film_list-wrap\">(.*?)</section>', html, re.S)
    section = m.group(1) if m else html
    return _parse_flw_cards(section, limit)
