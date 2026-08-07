"""Scraping helpers for filmo.to (server-rendered Laravel site, movies only).

Unlike every other supported site, a filmo.to movie page does NOT hand out a
usable hoster link at all: each "provider chip" (one per language x hoster
pair) only carries an encrypted ``data-p`` payload --

    <div data-provider-chip data-movie-link-id="8450" data-p="eyJpdi..."
         aria-label="VOE"> ... </div>

-- grouped under a language header:

    <div class="provider-row">
      <div class="provider-row__head">...<span class="provider-row__lang">English</span></div>
      <div class="provider-row__chips">
        <div data-provider-chip data-p="..." aria-label="VOE">...</div>
      </div>
    </div>

The real hoster URL is only minted on demand: the page's own JS POSTs
``{"p": data_p}`` to ``window.filmoLibrary.urls.openMint`` (``/n``) with the
page's CSRF token, gets back ``{"x": "<one-shot token>"}``, and then loads
``/n/<token>`` -- which 302-redirects straight to the hoster embed (e.g.
``https://voe.sx/e/<id>``). Both requests must reuse the SAME session/cookies
that fetched the movie page (Laravel ties the CSRF token to that session), so
every call below goes through the shared thread-local ``GLOBAL_SESSION``.

filmo.to exposes no IMDb id anywhere on the page (only a plain-text rating,
"IMDb 8.2/10") -- resolving one requires an external lookup; see
models/filmo_to/movie.py's `imdb` property, which uses the app's existing
TMDB integration (web.tmdb_cache) rather than hard-coding a second one here.
"""
import re
from html import unescape
from urllib.parse import quote

try:
    from ...config import FILMO_BASE_URL, logger, GLOBAL_SESSION
except ImportError:  # pragma: no cover
    from mediaforge.config import FILMO_BASE_URL, logger, GLOBAL_SESSION

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


class FilmoTokenExpired(Exception):
    """filmo.to rejected the CSRF token / session as stale (HTTP 419).

    Its own name because it is the one filmo failure that is worth RETRYING
    rather than reporting: 419 is Laravel's "Page Expired", i.e. literally
    "reload the page and try again". The movie page is fetched once and its
    token cached on the FilmoMovie, so on the second download attempt -- or
    simply a few minutes later -- that cached token can be older than the
    session behind it, and every further attempt reuses the same dead token
    and fails identically. See FilmoMovie.provider_url, which refetches once
    when this is raised.
    """


class FilmoUnavailable(Exception):
    """filmo.to did not hand back a usable page/response.

    Kept as its own type (not caught by a generic except) so callers -- e.g.
    the future browse/search routes -- can tell "the source site had a bad
    day" apart from a real bug, the same convention megakino_to/scraper.py's
    MegakinoUnavailable follows.
    """


def base_url():
    return FILMO_BASE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# Movie page: fetch + CSRF
# ---------------------------------------------------------------------------
_CSRF_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def fetch_movie_page(url, timeout=15):
    """GET a filmo.to movie page. Returns (html, csrf_token).

    Uses GLOBAL_SESSION so the cookies this response sets (DDoS-Guard pass,
    Laravel session, XSRF-TOKEN) are still attached to the later mint POST/GET
    in :func:`resolve_provider_url` -- both need to look like the same browser.
    """
    resp = GLOBAL_SESSION.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        raise FilmoUnavailable(f"Movie not found (HTTP 404): {url}")
    resp.raise_for_status()
    html = resp.text
    m = _CSRF_RE.search(html)
    if not m:
        raise FilmoUnavailable(f"No CSRF token found on movie page: {url}")
    return html, m.group(1)


# ---------------------------------------------------------------------------
# Metadata (title, year, genres, description, poster, rating, runtime, cast)
# ---------------------------------------------------------------------------
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_CONTENT_RE = re.compile(r'<h2[^>]*>\s*Inhalt\s*</h2>\s*<p[^>]*>(.*?)</p>', re.S)
_POSTER_RE = re.compile(r'src="(https://filmo\.to/img/poster/desktop/[^"]+)"')
_OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')
_DT_DD_RE = re.compile(
    r"<dt[^>]*>.*?<h3[^>]*>([^<]+)</h3>\s*</dt>\s*<dd[^>]*>(.*?)</dd>", re.S
)
_LINK_ENTRY_RE = re.compile(r'class="[^"]*link-entry[^"]*"[^>]*>([^<]+)</a>')
_TAG_RE = re.compile(r"<[^>]+>")

_LIST_FIELDS = {"Genres", "Regie", "Darsteller"}


def _strip_tags(html_fragment):
    return unescape(_TAG_RE.sub(" ", html_fragment or "")).strip()


def _clean_text(html_fragment):
    """Tag-stripped text with whitespace collapsed (tags are replaced by a
    space each, so "</dt>\\n<dd>" doesn't glue two words together)."""
    text = _strip_tags(html_fragment)
    return re.sub(r"\s+", " ", text).strip()


def parse_meta(html):
    """Title/year/genres/description/poster/rating/runtime/director/cast from
    a movie page's own "Mehr Infos" definition list -- see module docstring
    for the raw dt/dd shape."""
    title = ""
    m = _H1_RE.search(html)
    if m:
        title = unescape(_clean_text(m.group(1)))

    description = ""
    m = _CONTENT_RE.search(html)
    if m:
        description = unescape(_clean_text(m.group(1)))

    poster_url = ""
    m = _POSTER_RE.search(html)
    if m:
        poster_url = m.group(1)
    else:
        m = _OG_IMAGE_RE.search(html)
        if m:
            poster_url = m.group(1)

    fields = {}
    for label, content in _DT_DD_RE.findall(html):
        label = label.strip()
        if label in _LIST_FIELDS:
            fields[label] = [unescape(v.strip()) for v in _LINK_ENTRY_RE.findall(content)]
        else:
            fields[label] = _clean_text(content)

    year = ""
    ym = re.search(r"\b(19|20)\d{2}\b", fields.get("Erscheinungsdatum", ""))
    if ym:
        year = ym.group(0)

    runtime_minutes = None
    rm = re.search(r"(\d+)\s*Min", fields.get("Laufzeit", ""))
    if rm:
        runtime_minutes = int(rm.group(1))

    rating = None
    rm2 = re.search(r"([\d.]+)\s*/\s*10", fields.get("Bewertung", ""))
    if rm2:
        try:
            rating = float(rm2.group(1))
        except ValueError:
            rating = None

    return {
        "title": title,
        "description": description,
        "poster_url": poster_url,
        "year": year,
        "runtime_minutes": runtime_minutes,
        "rating": rating,
        "original_language": fields.get("Originalsprache", ""),
        "countries": fields.get("Länder", ""),
        "genres": fields.get("Genres", []),
        "directors": fields.get("Regie", []),
        "cast": fields.get("Darsteller", []),
    }


# ---------------------------------------------------------------------------
# Provider chips (language -> hoster -> mint payload)
# ---------------------------------------------------------------------------
_LANG_MARK_RE = re.compile(r'provider-row__lang">([^<]+)<')
_CHIP_RE = re.compile(
    r'data-provider-chip\s+data-movie-link-id="(?P<link_id>\d+)"\s+'
    r'data-p="(?P<data_p>[^"]+)"[^>]*aria-label="(?P<hoster>[^"]+)"'
)
_METADATA_TAG_RE = re.compile(r'provider-chip__metadata-tag">([^<]+)<')


def parse_provider_rows(html):
    """{canonical_label: {hoster_name: {"data_p", "movie_link_id"}}}.

    canonical_label is resolved via mediaforge.languages (site labels
    "Deutsch"/"English" already match its alias table); an unrecognized label
    is kept as-is rather than dropped, so a future language filmo.to adds
    still shows up instead of silently disappearing.
    """
    try:
        from ...languages import normalize_label
    except ImportError:  # pragma: no cover
        from mediaforge.languages import normalize_label

    lang_marks = [(m.start(), unescape(m.group(1)).strip()) for m in _LANG_MARK_RE.finditer(html)]
    if not lang_marks:
        return {}

    result = {}
    for chip in _CHIP_RE.finditer(html):
        pos = chip.start()
        label_raw = None
        for mark_pos, label in lang_marks:
            if mark_pos <= pos:
                label_raw = label
            else:
                break
        if label_raw is None:
            continue
        label = normalize_label(label_raw) or label_raw
        hoster = chip.group("hoster").strip()
        result.setdefault(label, {})
        if hoster not in result[label]:
            result[label][hoster] = {
                "data_p": chip.group("data_p"),
                "movie_link_id": chip.group("link_id"),
            }
    return result


# ---------------------------------------------------------------------------
# Mint: data_p -> real hoster URL
# ---------------------------------------------------------------------------
def resolve_provider_url(data_p, csrf_token, referer, timeout=15):
    """POST *data_p* to /n (mints a one-shot token) then follow /n/<token>'s
    redirect to the real hoster URL (e.g. https://voe.sx/e/<id>).

    *csrf_token* and the GLOBAL_SESSION cookies must come from the SAME movie
    page fetch that produced *data_p* -- see fetch_movie_page().
    """
    post_headers = dict(_HEADERS, **{
        "X-CSRF-TOKEN": csrf_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    })
    resp = GLOBAL_SESSION.post(
        base_url() + "/n", json={"p": data_p}, headers=post_headers, timeout=timeout,
    )
    if resp.status_code == 419:
        # Laravel's "Page Expired": the CSRF token no longer matches the
        # session. Distinct exception so the caller can refetch the movie page
        # and retry instead of surfacing a number nobody can act on.
        raise FilmoTokenExpired(
            f"filmo.to rejected the CSRF token as expired (HTTP 419) for {referer}"
        )
    resp.raise_for_status()
    try:
        token = (resp.json() or {}).get("x")
    except ValueError:
        token = None
    if not token:
        raise FilmoUnavailable("Filmo mint endpoint (/n) returned no token")

    mint_url = f"{base_url()}/n/{quote(token, safe='')}"
    redirect_resp = GLOBAL_SESSION.get(
        mint_url, headers=dict(_HEADERS, Referer=referer), timeout=timeout, allow_redirects=True,
    )
    return redirect_resp.url


# ---------------------------------------------------------------------------
# Listing / search (title + url + poster cards)
# ---------------------------------------------------------------------------
# filmo.to uses TWO different card markups for the same kind of card, not one
# per page type: /movies and /search render the plain grid
# ("movie-poster-grid-card"), while /popular renders a swiper/spotlight
# carousel ("popular-spotlight-card__link") -- confirmed live: the grid regex
# alone matched only the single non-carousel "hero" card /popular also has,
# 1 result instead of the 35 actually listed. Both are tried and merged so a
# page that switches layout (or mixes both, as /popular does) still parses.
_CARD_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_CARD_IMG_ALT_RE = re.compile(r'<img[^>]+alt="([^"]*)"')

_CARD_PATTERNS = (
    # (link regex, title regex) -- link regex group 1 = url, group 2 = body
    (
        re.compile(r'<a href="(https://filmo\.to/movies/[^"]+)" class="movie-poster-grid-card[^"]*"(.*?)</a>', re.S),
        re.compile(r'movie-poster-grid-card__title">([^<]+)<'),
    ),
    (
        re.compile(r'<a href="(https://filmo\.to/movies/[^"]+)" class="popular-spotlight-card__link[^"]*"(.*?)</a>', re.S),
        re.compile(r'popular-spotlight-card__title[^"]*">([^<]+)<'),
    ),
)


def _parse_cards(html, limit=None):
    cards = []
    seen_urls = set()
    for link_re, title_re in _CARD_PATTERNS:
        for m in link_re.finditer(html):
            url, body = m.group(1), m.group(2)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title_m = title_re.search(body)
            img_m = _CARD_IMG_RE.search(body)
            title = title_m.group(1).strip() if title_m else ""
            if not title:
                # The /popular "hero" card carries no dedicated title element,
                # only the poster's alt text -- fall back to that rather than
                # surfacing a blank card.
                alt_m = _CARD_IMG_ALT_RE.search(body)
                title = alt_m.group(1).strip() if alt_m else ""
            cards.append({
                "title": unescape(title),
                "url": url,
                "poster_url": img_m.group(1) if img_m else "",
            })
            if limit and len(cards) >= limit:
                return cards
    return cards


def _get(path, params=None, timeout=15):
    resp = GLOBAL_SESSION.get(base_url() + path, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_new_movies(page=1, limit=24):
    """"Neueste" -- /movies sorted by newest release."""
    try:
        html = _get("/movies", {"sort": "release_desc", "page": page})
    except Exception as e:
        logger.warning("Filmo new-movies fetch failed: %s: %s", type(e).__name__, e)
        return None
    return _parse_cards(html, limit)


def fetch_popular_movies(page=1, limit=24):
    """"Beliebte" -- the dedicated /popular listing."""
    try:
        html = _get("/popular", {"page": page})
    except Exception as e:
        logger.warning("Filmo popular-movies fetch failed: %s: %s", type(e).__name__, e)
        return None
    return _parse_cards(html, limit)


def search(keyword, limit=30):
    """Search filmo.to for *keyword*. Same {title, url, poster_url} card shape
    as fetch_new_movies()/fetch_popular_movies() (mirrors megakino_to.scraper
    .search(), which every provider's search wiring already expects).

    Uses the server-rendered /search results page (posters included) rather
    than the bare /search/suggest JSON endpoint -- that one only ever returns
    title+url and exists purely for the site's own autocomplete dropdown.
    /search/suggest is kept as a fallback for when the full page comes back
    empty/blocked, since a title+url hit still beats no result at all.
    """
    if not keyword:
        return []
    try:
        html = _get("/search", {"q": keyword})
        cards = _parse_cards(html, limit)
        if cards:
            return cards
    except Exception as e:
        logger.debug("Filmo search page failed for %r: %s", keyword, e)

    try:
        resp = GLOBAL_SESSION.get(
            base_url() + "/search/suggest",
            params={"q": keyword},
            headers=dict(_HEADERS, Accept="application/json", **{"X-Requested-With": "XMLHttpRequest"}),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as e:
        logger.debug("Filmo search suggest fallback failed for %r: %s", keyword, e)
        return []
    items = data.get("movies") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [{"title": it.get("title", ""), "url": it.get("url", ""), "poster_url": ""} for it in items[:limit]]
