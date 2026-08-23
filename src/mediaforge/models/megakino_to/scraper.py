"""Scraping helpers for MegaKino (DLE / DataLife Engine, "popcornie-dark").

The site used to be a React SPA backed by a clean JSON API (``/data/browse/``,
``/data/watch/``). It is not any more: those endpoints answer 404 and the site
now runs on DataLife Engine and server-renders everything. This module was
rewritten accordingly -- HTML in, the same dicts out.

The public surface is deliberately UNCHANGED, because the model classes
(movie.py / series.py / season.py / episode.py) and the callers in
``search.py`` / ``web/routes/search.py`` are written against it:

    fetch_watch(url)            -> payload dict (see _payload_from_html)
    parse_meta(payload)         -> {title, year, genres, description, ...}
    movie_hosters(payload)      -> {hoster_name: embed_url}
    episode_numbers(payload)    -> [1, 2, ...]
    episode_hosters(payload, n) -> {hoster_name: embed_url}
    season_number(payload)      -> int
    search / fetch_new_* / fetch_popular_*   -> [card, ...]

``_payload_from_html`` therefore builds the very same shape the JSON API used
to return (``streams``/``e``/``tv``/``s``/``poster_path``/...), so everything
downstream of it kept working without a change.

Site structure (verified against the live HTML, not the rendered DOM)
--------------------------------------------------------------------
* Content URL: ``/<category>/<id>-<slug>.html``. The category segment VARIES --
  ``/films/``, ``/serials/``, but also ``/crime/``, ``/action/``, ... Search
  results mix all of them, so nothing may key off it; the numeric ``<id>`` is
  the stable part.
* A season page IS the series page (one post per season), same as before.
* Listing pages: ``/films/``, ``/serials/``, ``/kinofilme/``, ``/documentary/``,
  20 cards each, paginated as ``/films/page/2/``. Cards are
  ``<a class="poster grid-item" href=...>`` with ``.poster__title``,
  ``.poster__subtitle`` (country/year, then genres) and ``img[data-src]``.
* Search: ``/index.php?do=search&subaction=search&story=<query>`` (GET works
  and returns the same document the POST form does).
* Movie hosters: ``.pmovie__player.tabs-block`` -> ``.tabs-block__select span``
  carries the hoster NAMES in order, ``.tabs-block__content iframe[data-src]``
  the embed URLs in the SAME order.
* Series hosters: one ``<select id="epN" class="mr-select">`` per episode, each
  ``<option value="<embed url>">Hoster</option>``; the episode list itself is
  ``select.se-select`` with ``<option value="epN">Episode N</option>``. The
  episode<->hoster link is the select's ``id``, not document order.
* The trailer is a plain ``<iframe src=...>`` inside ``.pmovie__trailer`` and
  must never be mistaken for a hoster -- hoster iframes use ``data-src`` and
  live in ``.tabs-block__content``.

The cookie gate
---------------
A client without the site's session cookie does not get the page. It gets ~230
bytes of HTML whose only content is::

    <script>fetch('/index.php?<param>=<token>', {credentials:'include'})
      .then(function(){ location.replace('<the path you asked for>') })
      .catch(function(){ location.reload() })</script>

Fetching that URL sets an HttpOnly cookie; the next request for the real page
then succeeds. This is what broke the scraper on the new site: it reported
"non-JSON response (possible block/challenge page)" because it was looking at
this stub. :func:`_looks_like_gate` / :func:`_clear_gate` replay the handshake
with the session's own cookie jar. It is deliberately token-agnostic -- the URL
is taken from the stub rather than reconstructed -- so a renamed parameter
changes nothing here.
"""
import re
import threading
from html import unescape
from urllib.parse import urlencode, urljoin, urlsplit

try:
    from ...config import megakino_base_url, logger, GLOBAL_SESSION
except ImportError:  # pragma: no cover
    from mediaforge.config import megakino_base_url, logger, GLOBAL_SESSION

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

# Map an embed-domain substring to the canonical extractor/provider name.
_HOSTER_DOMAINS = [
    ("voe", "VOE"),
    ("vidmoly", "Vidmoly"),
    ("vidoza", "Vidoza"),
    ("vidavaca", "Vidavaca"),
    ("vidara", "Vidara"),
    ("vidaar", "Vidara"),  # vidaarax.com/.net and other Vidara-family mirrors
    ("veev", "VeeV"),
    ("filemoon", "Filemoon"),
    ("dood", "Doodstream"),
    ("streamtape", "Streamtape"),
    ("luluvdo", "Luluvdo"),
    ("loadx", "LoadX"),
    ("firestream", "Firestream"),
    ("vidsonic", "Vidsonic"),
    ("upbolt", "Upbolt"),
    ("gxplayer", "GXPlayer"),
]

# /<category>/<id>-<slug>.html -- the category segment is not significant.
_CONTENT_PATH_RE = re.compile(r"/[^/]+/(\d+)-[^/]*\.html", re.IGNORECASE)


def base_url():
    """The live MegaKino origin, e.g. ``https://megakino16.com``.

    Resolved per call on purpose: the domain rotates, and a module-level
    constant would keep the whole provider pinned to a dead host until the next
    restart. See config.megakino_base_url().

    Raises MegakinoUnavailable when no domain is known. MediaForge ships none
    for this site -- the domain feed is the only source -- so "not resolved
    yet" is a real state, and it has to surface as "the source is unavailable"
    rather than as requests against ``https:///data/...``.
    """
    base = (megakino_base_url() or "").rstrip("/")
    if not base:
        raise MegakinoUnavailable(
            "MegaKino has no known domain right now — the domain lookup "
            "(Settings → Sources → “Resolve domains automatically”) has not "
            "produced one yet. Add a domain by hand under Domain fallback, or "
            "set MEGAKINO_BASE_URL, to use the source without the lookup."
        )
    return base


def _base_or_empty():
    """Like :func:`base_url` but ``""`` instead of raising.

    For the two callers that only decorate data (poster and card URLs): a card
    that cannot render its poster is a cosmetic problem, and letting the
    "no domain yet" exception escape from there would take down a whole
    listing response over it.
    """
    try:
        return base_url()
    except MegakinoUnavailable:
        return ""


def _text(value, _depth=0):
    """Coerce a field that should be text but may arrive as a list.

    Kept from the JSON era: :func:`_payload_from_html` still produces lists for
    genres, and every ``re.*`` / ``html.unescape()`` call below expects ``str``.
    Depth-bounded so a malformed structure can never turn into a RecursionError.
    """
    if isinstance(value, (list, tuple, set)):
        if _depth >= 2:
            return ""
        return ", ".join(t for t in (_text(v, _depth + 1).strip() for v in value) if t)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def poster_url(path):
    """Poster path -> absolute URL.

    Posters used to be TMDB paths; the DLE site serves its own
    ``/uploads/posts/...webp`` instead, so a site-relative path is resolved
    against the LIVE origin rather than against image.tmdb.org. Both forms are
    accepted, since a cached card from before the switch may still carry a
    TMDB path.
    """
    path = _text(path)
    if not path:
        return ""
    if path.startswith("http"):
        return path
    if path.startswith("/t/p/"):  # legacy TMDB poster path
        return "https://image.tmdb.org" + path
    base = _base_or_empty()
    if not base:
        return ""
    return base + (path if path.startswith("/") else "/" + path)


def slugify(title):
    s = unescape(_text(title)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "title"


def content_url(item):
    """Absolute URL for a card/payload item.

    The path comes from the site itself (the card's own href) -- it cannot be
    rebuilt from id+slug, because the category segment varies per title and is
    not derivable from anything we hold.
    """
    path = _text(item.get("path") or item.get("url"))
    if not path:
        return ""
    if path.startswith("http"):
        return path
    base = _base_or_empty()
    if not base:
        return ""
    return base + (path if path.startswith("/") else "/" + path)


def extract_id(url):
    """The numeric post id out of a ``/<category>/<id>-<slug>.html`` URL."""
    if not url:
        return None
    m = _CONTENT_PATH_RE.search(str(url).split("?")[0])
    return m.group(1) if m else None


def content_path(url):
    """The site-relative path of a content URL, query stripped."""
    if not url:
        return ""
    parts = urlsplit(str(url))
    return parts.path or ""


# A season post's slug carries the season, in either word order:
#   .../4692-the-penguin-staffel-1.html      -> 1
#   .../6406-reacher-4-staffel.html          -> 4
_SLUG_SEASON_RE = re.compile(r"(?:staffel-(\d+)|(\d+)-staffel)", re.IGNORECASE)


def is_series_item(item):
    """True when this card/payload is a season post rather than a movie.

    Decided by the payload's own ``tv`` flag when present (set from the page's
    episode selector, which is authoritative), else by the season marker in the
    slug -- that is all a listing card offers.
    """
    if str(item.get("tv")) == "1":
        return True
    if item.get("tv") == "0":
        return False
    return bool(_SLUG_SEASON_RE.search(_text(item.get("path") or item.get("url"))))


def normalize_hoster_url(url):
    """VOE embeds arrive as ``voe.sx/<id>``; the extractor expects ``voe.sx/e/<id>``."""
    if not url:
        return url
    low = url.lower()
    if "voe" in low and "/e/" not in url:
        m = re.match(r"^(https?://[^/]+)/([A-Za-z0-9]+)(.*)$", url)
        if m:
            return f"{m.group(1)}/e/{m.group(2)}{m.group(3)}"
    return url


def classify_hoster(url):
    if not url:
        return None
    low = url.lower()
    for needle, name in _HOSTER_DOMAINS:
        if needle in low:
            return name
    return None


# ---------------------------------------------------------------------------
# HTTP (server-rendered HTML behind a cookie gate)
# ---------------------------------------------------------------------------
_session = None
_session_base = None      # the origin _session was built for
_session_lock = threading.Lock()


def _get_session():
    """The plain-requests fallback session, rebuilt when the domain rotates.

    It bakes in a ``Referer`` and carries the cookie jar the gate handshake
    depends on, and both belong to ONE origin. Keeping the session across a
    domain change meant sending the old domain's referer (and its cookie) to
    the new one. ``reset_session()`` existed for this but nothing ever called
    it; the origin is now tracked, so the rebuild happens on its own.
    """
    global _session, _session_base
    base = base_url()
    if _session is None or _session_base != base:
        with _session_lock:
            if _session is None or _session_base != base:
                import requests as _req
                s = _req.Session()
                s.headers.update({
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                    "Referer": base + "/",
                })
                _session = s
                _session_base = base
    return _session


def reset_session():
    global _session, _session_base
    with _session_lock:
        _session = None
        _session_base = None


_MK_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def _doh_get(url, headers, timeout):
    return GLOBAL_SESSION.get(url, headers=headers, timeout=timeout)


def _plain_get(url, headers, timeout):
    return _get_session().get(url, headers=headers, timeout=timeout)


class MegakinoUnavailable(Exception):
    """MegaKino did not hand back a usable page.

    Covers every way this can happen, because to a caller they are the same
    thing: the host is unreachable, the request timed out, the cookie gate did
    not clear, or the response was not the page we asked for.

    It exists as its own type so the web routes can tell "the source site had a
    bad day" apart from "MediaForge is broken" without matching on exception
    CLASS NAMES, which is what routes/search.py did before: it looked for the
    substrings "connection"/"timeout"/"protocol"/"ssl" and therefore missed a
    whole family of failures. Those fell through to the generic handler, which
    answers 500 and logs at ERROR -- and an ERROR log is exactly what
    telemetry/hooks.py turns into a crash report. Every hiccup on the site's
    side was being filed as a defect in this app. See routes/browse.py's
    "upstream failures answer 502" note for the convention this follows.
    """


# The gate stub is tiny and contains nothing but the bootstrap script. The cap
# is generous (a real page is 35-70 KB) but low enough that no real page can be
# mistaken for it.
_GATE_MAX_BYTES = 4096
_GATE_FETCH_RE = re.compile(r"""fetch\(\s*['"]([^'"]{1,300})['"]""")
_REAL_PAGE_MARKERS = ("dle-content", "pmovie__", "poster grid-item", "</body>")


def _looks_like_gate(text):
    """True for the cookie-gate stub, false for any real page.

    Three independent conditions, because mistaking a real page for the gate
    would send every request through a pointless extra round-trip: it has to be
    tiny, it must not carry any marker a real page always has, and it must
    contain both the bootstrap fetch and the reload it performs afterwards.
    """
    if not text or len(text) > _GATE_MAX_BYTES:
        return False
    low = text.lower()
    if any(m in low for m in _REAL_PAGE_MARKERS):
        return False
    return bool(_GATE_FETCH_RE.search(text)) and ("location.replace" in text or "location.reload" in text)


def _clear_gate(get, gate_html, timeout):
    """Replay the gate's own bootstrap request so the session earns its cookie.

    The URL is taken verbatim from the stub rather than reconstructed, so the
    token format and the parameter name are none of our business and a change
    to either does not break this. It is resolved against the live origin and
    then REJECTED unless it stayed on that origin: the stub is untrusted input
    from the network, and following it to an arbitrary host would turn this
    into an open redirect that fetches whatever the page names.
    """
    m = _GATE_FETCH_RE.search(gate_html or "")
    if not m:
        return False
    base = base_url()
    target = urljoin(base + "/", m.group(1))
    if urlsplit(target).hostname != urlsplit(base).hostname:
        logger.warning("[MegaKino] Ignoring a cookie-gate redirect to a foreign host")
        return False
    try:
        get(target, dict(_MK_HEADERS, Referer=base + "/"), timeout)
    except Exception as e:
        logger.debug("[MegaKino] Cookie-gate handshake failed: %s", e)
        return False
    return True


def _get_html(path, params=None, timeout=15):
    """Fetch a MegaKino page and return its HTML.

    Tries the DoH session first (bypasses ISP DNS blocks) and the plain
    requests session as a fallback. Each transport clears the cookie gate at
    most once -- retrying it forever would spin if the handshake ever stopped
    producing a usable cookie.

    Raises MegakinoUnavailable when neither transport produced a real page.
    """
    base = base_url()
    url = base + path
    if params:
        url += ("&" if "?" in path else "?") + urlencode(params)
    headers = dict(_MK_HEADERS, Referer=base + "/")
    reason = None

    for get in (_doh_get, _plain_get):
        for attempt in (0, 1):
            try:
                resp = get(url, headers, timeout)
                resp.raise_for_status()
            except Exception as e:
                reason = f"{get.__name__}: {type(e).__name__}: {e}"
                break
            body = getattr(resp, "text", "") or ""
            if _looks_like_gate(body):
                if attempt == 0 and _clear_gate(get, body, timeout):
                    continue  # cookie earned -- ask for the real page again
                reason = f"{get.__name__}: the cookie gate did not clear"
                break
            if not body.strip():
                reason = f"{get.__name__}: empty response"
                break
            return body
    raise MegakinoUnavailable(
        f"MegaKino ({urlsplit(base).hostname or base}) returned no usable page "
        f"({reason or 'unknown'})"
    )


def _soup(html):
    from bs4 import BeautifulSoup
    # html.parser, not lxml: only beautifulsoup4 is a declared dependency.
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Listings, search
# ---------------------------------------------------------------------------
# Listing sections. "kinofilme" and "documentary" are separate DLE categories
# that also hold films; they are not merged in here, the home feed asks for
# what it wants.
LIST_MOVIES = "/films/"
LIST_SERIES = "/serials/"
LIST_CINEMA = "/kinofilme/"
LIST_DOCS = "/documentary/"


def _card_from_poster(node):
    """One ``<a class="poster grid-item">`` -> the card dict the app uses."""
    href = node.get("href") or ""
    title_node = node.select_one(".poster__title")
    title = unescape((title_node.get_text(" ", strip=True) if title_node else "").strip())
    img = node.select_one(".poster__img img")
    poster = ""
    if img is not None:
        # data-src holds the real image; src is the lazy-load placeholder.
        poster = img.get("data-src") or ""
        if not poster or "no-img" in poster:
            poster = img.get("src") or ""
        if "no-img" in poster:
            poster = ""
    if not title and img is not None:
        title = unescape((img.get("alt") or "").strip())

    # <ul class="poster__subtitle"><li>Country, Year</li><li>Cat / Genre / ...</li></ul>
    year, genre = "", ""
    subs = [li.get_text(" ", strip=True) for li in node.select(".poster__subtitle li")]
    for line in subs:
        if not year:
            m = re.search(r"\b(19|20)\d{2}\b", line)
            if m:
                year = m.group(0)
        if "/" in line and not genre:
            genre = " / ".join(p.strip() for p in line.split("/") if p.strip())
    item = {"path": href, "title": title}
    return {
        "title": title,
        "url": content_url(item),
        "poster_url": poster_url(poster),
        "genre": genre,
        "rating": (node.select_one(".poster__rating").get_text(strip=True)
                   if node.select_one(".poster__rating") else ""),
        "year": year,
        "is_series": is_series_item(item),
    }


def _cards_from_html(html):
    return [_card_from_poster(a) for a in _soup(html).select("a.poster")]


def _listing(path, page=1, limit=24):
    """Cards from a DLE listing page (20 per page, hence the page walk)."""
    cards = []
    try:
        page_no = 1
        while len(cards) < limit and page_no <= 3:
            sub = path if page_no == 1 else f"{path.rstrip('/')}/page/{page_no}/"
            batch = _cards_from_html(_get_html(sub))
            if not batch:
                break
            cards.extend(batch)
            page_no += 1
    except Exception as e:
        logger.debug("Megakino listing failed (%s): %s", path, e)
        return cards[:limit] or None
    return cards[:limit]


def search(keyword):
    """Search across movies and series (DLE ``do=search``).

    Results span every category (``/crime/``, ``/action/``, ``/films/``, ...),
    which is why nothing downstream may key off the path's first segment.
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    try:
        html = _get_html("/index.php", {
            "do": "search", "subaction": "search", "story": kw,
        })
    except Exception as e:
        logger.debug("Megakino search failed (%r): %s", kw, e)
        return []
    return _cards_from_html(html)


def fetch_new_movies():
    return _listing(LIST_MOVIES)


def fetch_popular_movies():
    return _listing(LIST_CINEMA)


def fetch_new_series():
    return _listing(LIST_SERIES)


def fetch_popular_series():
    return _listing(LIST_SERIES)


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
def _first_text(soup, selector):
    node = soup.select_one(selector)
    return node.get_text(" ", strip=True) if node is not None else ""


def _payload_from_html(html, url):
    """A detail page -> the payload dict the model classes consume.

    Shaped exactly like the JSON the old API returned, so parse_meta(),
    season_number(), movie_hosters() and episode_hosters() below did not have
    to change: ``streams`` is a list of ``{"stream": <embed>, "e": <episode or
    None>, "source": <hoster name>}``.
    """
    soup = _soup(html)
    path = content_path(url)

    title = _first_text(soup, "h1")
    year_line = _first_text(soup, ".pmovie__year")            # "Japan, 2025, 95 min"
    genres_line = _first_text(soup, ".pmovie__genres")        # "Filme / Horror / Thriller"
    poster = ""
    poster_img = soup.select_one(".pmovie__poster img")
    if poster_img is not None:
        poster = poster_img.get("data-src") or poster_img.get("src") or ""
    if not poster or "no-img" in poster:
        og = soup.select_one('meta[property="og:image"]')
        poster = (og.get("content") if og is not None else "") or ""

    description = ""
    for sel in ('[itemprop="description"]', ".pmovie__text", ".page__text", ".pmovie__descr"):
        description = _first_text(soup, sel)
        if description:
            break

    # Genres: drop the leading DLE category ("Filme", "Serien") -- it is a
    # section, not a genre, and it would otherwise show up on every card.
    genres = [g.strip() for g in genres_line.split("/") if g.strip()]
    if genres and genres[0].lower() in ("filme", "serien", "kinofilme", "dokumentation", "documentary"):
        genres = genres[1:]

    streams, episodes = _streams_from_soup(soup)
    is_series = bool(episodes)

    return {
        "path": path,
        "url": url,
        "title": unescape(title),
        "original_title": unescape(_first_text(soup, ".pmovie__original-title")),
        "year": year_line,
        "genres": genres,
        "storyline": unescape(description),
        "poster_path": poster,
        "imdb_id": None,   # the DLE template exposes no IMDb id
        "rating": _first_text(soup, ".pmovie__subrating--site"),
        "tv": "1" if is_series else "0",
        "s": _season_from_slug(path) or _season_from_title(title) or 1,
        "streams": streams,
    }


def _streams_from_soup(soup):
    """(streams, episode_numbers) for a detail page.

    Two shapes, and they must not be confused:

    * Season post -- one ``<select id="epN" class="mr-select">`` per episode,
      each option's ``value`` IS the embed URL and its text the hoster name.
      The episode number comes from the select's id, never from document
      order, so a missing episode cannot shift every following one.
    * Movie -- ``.tabs-block__select span`` holds the hoster names and
      ``.tabs-block__content iframe[data-src]`` the embed URLs, positionally
      paired. The trailer is excluded structurally: it sits in
      ``.pmovie__trailer`` and uses a plain ``src``, not ``data-src``.
    """
    streams = []
    episodes = []

    for select in soup.select("select.mr-select"):
        m = re.fullmatch(r"ep(\d+)", (select.get("id") or "").strip(), re.IGNORECASE)
        if not m:
            continue
        number = int(m.group(1))
        episodes.append(number)
        for option in select.select("option"):
            embed = (option.get("value") or "").strip()
            if not embed.lower().startswith(("http://", "https://")):
                continue
            streams.append({
                "stream": embed,
                "e": number,
                "source": option.get_text(" ", strip=True),
            })
    if streams:
        return streams, sorted(set(episodes))

    player = soup.select_one(".pmovie__player")
    if player is None:
        return [], []
    names = [s.get_text(" ", strip=True) for s in player.select(".tabs-block__select span")]
    frames = []
    for block in player.select(".tabs-block__content"):
        frame = block.find("iframe")
        embed = (frame.get("data-src") if frame is not None else "") or ""
        if embed.lower().startswith(("http://", "https://")):
            frames.append(embed)
    for index, embed in enumerate(frames):
        streams.append({
            "stream": embed,
            "e": None,
            "source": names[index] if index < len(names) else "",
        })
    return streams, []


def _season_from_slug(path):
    m = _SLUG_SEASON_RE.search(path or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _season_from_title(title):
    m = re.search(r"Staffel\s*(\d+)|(\d+)\.?\s*Staffel", title or "", re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def fetch_watch(url_or_id):
    """The payload for a content URL.

    A bare id is not enough any more: the category segment of the path varies
    per title and cannot be derived, so callers must pass the full URL (which
    is what every model class stores anyway).
    """
    raw = str(url_or_id or "")
    path = content_path(raw) if "//" in raw else raw
    if not _CONTENT_PATH_RE.search(path):
        raise ValueError(f"Cannot build a MegaKino page URL from: {url_or_id}")
    return _payload_from_html(_get_html(path), base_url() + path)


def parse_meta(data):
    """Shared metadata from a detail payload (movie or season)."""
    genres = [g.strip() for g in re.split(r"[/,]", _text(data.get("genres"))) if g.strip()]
    year = ""
    ym = re.search(r"\b(19|20)\d{2}\b", _text(data.get("year")))
    if ym:
        year = ym.group(0)
    return {
        "title": unescape(_text(data.get("title"))),
        "year": year,
        "genres": genres,
        "description": unescape(_text(data.get("storyline")) or _text(data.get("overview"))),
        "poster_url": poster_url(data.get("poster_path") or ""),
        "imdb_id": _text(data.get("imdb_id")) or None,
        "rating": _text(data.get("rating") or ""),
        "tv": _text(data.get("tv")) or "0",
    }


def strip_season_suffix(title):
    if not title:
        return title
    t = re.sub(r"\s*[-–]\s*\d+\.?\s*Staffel\b.*$", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\s*[-–]\s*Staffel\s*\d+\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*Staffel\s*\d+\b.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\d+\.?\s*Staffel\b.*$", "", t, flags=re.IGNORECASE)
    return t.strip(" -–") or title.strip()


def season_number(data):
    for key in ("s", "season", "season_number"):
        v = data.get(key)
        if v not in (None, "", "0"):
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    m = re.search(r"Staffel\s*(\d+)", _text(data.get("title")), re.IGNORECASE)
    return int(m.group(1)) if m else 1


def movie_hosters(data):
    """{provider_name: embed_url} for a movie payload (streams without episode)."""
    hosters = {}
    for st in (data.get("streams") or []):
        if st.get("e") is not None:
            continue
        url = normalize_hoster_url(st.get("stream") or "")
        name = classify_hoster(url) or (st.get("source") or "").strip()
        if name and name not in hosters:
            hosters[name] = url
    return hosters


def episode_numbers(data):
    nums = set()
    for st in (data.get("streams") or []):
        e = st.get("e")
        if e is not None:
            try:
                nums.add(int(e))
            except (TypeError, ValueError):
                pass
    return sorted(nums)


def episode_hosters(data, episode_number):
    """{provider_name: embed_url} for one episode of a season payload."""
    hosters = {}
    for st in (data.get("streams") or []):
        try:
            if int(st.get("e")) != int(episode_number):
                continue
        except (TypeError, ValueError):
            continue
        url = normalize_hoster_url(st.get("stream") or "")
        name = classify_hoster(url) or (st.get("source") or "").strip()
        if name and name not in hosters:
            hosters[name] = url
    return hosters
