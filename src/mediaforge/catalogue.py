"""Full A-Z catalogue of a source site, and the registry that holds them.

Both AniWorld and SerienStream publish their COMPLETE list of titles on one
page (``/animes-alphabet``, ``/serien``) -- 2.4k and 10.8k entries in a single
HTML response. That is a different kind of list from everything in
``search.py`` (a keyword query) or ``home_feed.py`` (a handful of cards): it is
the entire catalogue, it changes slowly, and it is what the Catalogue page
(``web/routes/catalogue.py``) offers for bulk selection.

What a catalogue entry deliberately does NOT have: a poster, a description, a
year. Neither site puts those on the list page, and fetching them per title
would mean thousands of requests. The Catalogue page therefore renders titles
only, and pulls the rich data for ONE title at a time when the user opens its
details -- through the same ``/api/series`` endpoint the search modal uses.

Alternative titles ARE included, because both sites hand them over for free in
the list markup (``data-alternative-title`` / ``data-search``). They never
appear in the UI; they exist so the client-side filter finds "Shingeki no
Kyojin" when the entry is called "Attack on Titan".

Fetching and PARSING is all this module does. Where the lists are kept, how
long they stay fresh and when they are refetched is ``web/catalogue_store.py``
-- they live in SQLite and are served stale while they revalidate. This file
used to hold a process-local dict as well; two caches with two different
lifetimes is how the next reader ends up trusting the wrong one.

Deliberately free of any ``mediaforge.web`` import: this is core, and the web
layer imports it, never the other way round -- same rule as home_feed.py.
"""

from __future__ import annotations

import re
from html import unescape

from .logger import get_logger

logger = get_logger(__name__)

# Hard ceiling on how many entries one catalogue may contribute. Not a
# performance guard (the page virtualises its list) but a corruption guard: a
# challenge page or a redesigned layout can make a regex match tens of
# thousands of times, and silently accepting that would fill the cache with
# junk that then looks like a catalogue.
MAX_ENTRIES = 50_000


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
# AniWorld: <li><a data-alternative-title="..." href="/anime/stream/<slug>"
#               title="... Stream anschauen"> Title</a></li>
_ANIWORLD_ITEM_RE = re.compile(
    r'<li>\s*<a\b(?P<attrs>[^>]*?)href="(?P<href>/anime/stream/[^"#?]+)"[^>]*>'
    r'(?P<title>[^<]*)</a>\s*</li>',
    re.IGNORECASE,
)
_ALT_TITLE_RE = re.compile(r'data-alternative-title="([^"]*)"', re.IGNORECASE)

# SerienStream: <li class="series-item" data-search="..."><a href="/serie/<slug>">Title</a></li>
_STO_ITEM_RE = re.compile(
    r'<li[^>]*class="[^"]*series-item[^"]*"(?P<attrs>[^>]*)>\s*'
    r'<a[^>]*href="(?P<href>/serie/[^"#?]+)"[^>]*>(?P<title>[^<]*)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DATA_SEARCH_RE = re.compile(r'data-search="([^"]*)"', re.IGNORECASE)


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _entry(title, path, base_url, alt=""):
    title = _clean(title)
    if not title:
        return None
    return {
        "title": title,
        "url": base_url.rstrip("/") + path.rstrip("/"),
        # Lower-cased and whitespace-collapsed here rather than in the browser:
        # doing it once for 10k entries on the server beats doing it on every
        # keystroke in every open tab.
        "alt": _clean(alt).lower(),
    }


def parse_aniworld_catalogue(html, base_url):
    """[{title, url, alt}] from AniWorld's /animes-alphabet page."""
    out, seen = [], set()
    for m in _ANIWORLD_ITEM_RE.finditer(html or ""):
        alt_m = _ALT_TITLE_RE.search(m.group("attrs") or "")
        entry = _entry(m.group("title"), m.group("href"), base_url,
                       alt_m.group(1) if alt_m else "")
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            out.append(entry)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def parse_sto_catalogue(html, base_url):
    """[{title, url, alt}] from SerienStream's /serien page."""
    out, seen = [], set()
    for m in _STO_ITEM_RE.finditer(html or ""):
        search_m = _DATA_SEARCH_RE.search(m.group("attrs") or "")
        entry = _entry(m.group("title"), m.group("href"), base_url,
                       search_m.group(1) if search_m else "")
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            out.append(entry)
        if len(out) >= MAX_ENTRIES:
            break
    return out


# ---------------------------------------------------------------------------
# Built-in catalogues
# ---------------------------------------------------------------------------
def _fetch(url, timeout=45):
    """GET a catalogue page through the project session (mirrors, DoH, ...).

    The generous timeout is not laziness: these responses are 0.7-2.5 MB and
    the sites are behind DDoS-Guard, so the default read timeout genuinely is
    too short for the largest of them.
    """
    from .config import GLOBAL_SESSION
    resp = GLOBAL_SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# The base URLs the rest of the app writes its AniWorld / SerienStream links
# with (see search.py, which spells them exactly like this). Deliberately NOT
# taken from mirrors.canonical_host(): that returns "s.to" for SerienStream,
# the domain the project deactivated -- the mirror registry keeps it as the
# first entry for host-rewriting purposes, but no URL we STORE should be
# written with it. Entry URLs from here end up in the download queue and in
# AutoSync jobs, so they have to be spelled the canonical way; GLOBAL_SESSION
# rewrites the host to whichever mirror is healthy at request time, and
# nothing downstream ever sees that.
ANIWORLD_CATALOGUE_URL = "https://aniworld.to/animes-alphabet"
STO_CATALOGUE_URL = "https://serienstream.to/serien"


def fetch_aniworld_catalogue():
    return parse_aniworld_catalogue(_fetch(ANIWORLD_CATALOGUE_URL), "https://aniworld.to")


def fetch_sto_catalogue():
    return parse_sto_catalogue(_fetch(STO_CATALOGUE_URL), "https://serienstream.to")


# id -> entry. Ids match the source ids used everywhere else
# (web/source_policy.py's BUILTIN_SEARCH_SOURCES), so the Catalogue page can
# reuse the same enabled/disabled state and the same labels.
BUILTIN_CATALOGUES = {
    "aniworld": {
        "label": "AniWorld",
        "kind": "anime",
        "color": "#6aa9ff",
        "fetch": fetch_aniworld_catalogue,
    },
    "sto": {
        "label": "SerienStream",
        "kind": "series",
        "color": "#8b7dff",
        "fetch": fetch_sto_catalogue,
    },
}


def _safe_color(value) -> str:
    """Allow only a literal hex colour through -- the value is rendered into a
    style attribute on the Catalogue page, and a module is not a trusted
    source of CSS. Same rule and same reason as home_feed._safe_color()."""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) not in (4, 7) or not text.startswith("#"):
        return ""
    if any(c not in "0123456789abcdefABCDEF" for c in text[1:]):
        return ""
    return text


# ---------------------------------------------------------------------------
# Third-party catalogues
# ---------------------------------------------------------------------------
_EXTRA_CATALOGUES: dict = {}  # item_id -> entry

# Called with the source_id when a third-party catalogue is unregistered.
# A hook rather than a direct call, because the thing that needs to react --
# the DB store in web/catalogue_store.py -- lives in the web layer, and this
# module must not import from there (see the module docstring).
UNREGISTER_HOOKS: list = []


def register_catalogue(item_id, source_id, label, fetch, kind="series", color=None):
    """Add a full-catalogue source from a third-party module's ``register(app)``.

    - ``item_id``: the id already passed to ``register_thirdparty()``, so
      ``web/thirdparties/registry.py``'s ``unregister_module()`` drops this
      automatically when the module is disabled or uninstalled.
    - ``source_id``: must match the id the module used for
      ``register_provider()`` / ``register_search_source()``, and must not
      collide with a built-in -- the Catalogue page keys its selection, its
      enabled state and its bulk actions on it.
    - ``fetch``: ``fn()`` returning ``[{"title", "url", "alt"}]``. ``url`` has
      to be resolvable by :func:`mediaforge.providers.resolve_provider`, or
      nothing can be queued from it. ``alt`` is optional (searchable alternate
      titles, lower-cased); an empty string is fine.
    - ``kind``: ``"anime"`` or ``"series"``; only used for the label shown
      above the list.
    - ``color``: optional CSS hex colour (e.g. ``"#7c5cff"``) for the dot next
      to this source's name, on its filter chip and on every one of its rows.
      The page merges all catalogues into ONE list, so that dot is what tells
      a module's entries apart from a built-in's; without it the source falls
      back to a shared neutral colour and looks like every other third party.
      Only a literal ``#rgb``/``#rrggbb`` is accepted -- the value ends up in a
      style attribute.

    The result is stored in the database and refreshed in the background
    roughly once a day (see ``web/catalogue_store.py``), so ``fetch`` may do
    real network work -- and should, rather than holding a copy in the module.
    It never runs inside a user's request.
    """
    if not callable(fetch):
        raise ValueError("register_catalogue: fetch must be callable")
    source_id = str(source_id or "").strip().lower()
    if not source_id:
        raise ValueError("register_catalogue: source_id is required")
    if source_id in BUILTIN_CATALOGUES:
        raise ValueError("register_catalogue: %r is a built-in catalogue" % source_id)
    for existing_id, entry in _EXTRA_CATALOGUES.items():
        if entry["source_id"] == source_id and existing_id != item_id:
            raise ValueError("register_catalogue: source id already registered: %r" % source_id)
    _EXTRA_CATALOGUES[item_id] = {
        "source_id": source_id,
        "label": str(label or source_id),
        "kind": kind if kind in ("anime", "series") else "series",
        "color": _safe_color(color),
        "fetch": fetch,
    }
    logger.info("[Catalogue] Registered third-party catalogue: %s (%s)", source_id, item_id)


def unregister_catalogue(item_id) -> None:
    """Drop a catalogue previously added via :func:`register_catalogue`."""
    removed = _EXTRA_CATALOGUES.pop(item_id, None)
    if removed:
        for hook in list(UNREGISTER_HOOKS):
            try:
                hook(removed["source_id"])
            except Exception as exc:
                logger.debug("[Catalogue] unregister hook failed: %s", exc)
        logger.info("[Catalogue] Unregistered third-party catalogue: %s (%s)",
                    removed["source_id"], item_id)


def thirdparty_catalogue_ids() -> set:
    """item_ids that currently own a catalogue -- read-only counterpart of
    :func:`unregister_catalogue` for the Modulmanager's capability list."""
    return set(_EXTRA_CATALOGUES)


def all_catalogues() -> dict:
    """{source_id: entry} for every catalogue that exists right now, built-ins
    first. A module that is switched off is left out, same rule as
    providers.all_providers()."""
    out = {sid: dict(meta, source_id=sid) for sid, meta in BUILTIN_CATALOGUES.items()}
    try:
        from .module_gate import filter_enabled
        extra = filter_enabled(_EXTRA_CATALOGUES)
    except Exception:
        extra = _EXTRA_CATALOGUES
    for entry in extra.values():
        out.setdefault(entry["source_id"], entry)
    return out
