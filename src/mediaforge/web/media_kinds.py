"""The media kinds MediaForge keeps separate libraries for.

Until now "the library" was one page that scanned every configured path for
videos *and* books and painted both into the same grid. That conflates two
questions the rest of the app keeps asking separately: "what may live in this
folder?" (a property of the path) and "what is the user looking at right now?"
(a property of the page). Both answers are enumerated here, once, so the
sidebar, the overview hub, the Settings path table, the scanner and any
third-party module read the same list instead of hard-coding their own.

Adding a kind means adding one entry below plus its scanner; nothing else in
the navigation has to be touched.

Labels deliberately live in the TEMPLATES, not here. babel.cfg extracts from
Jinja2 only (see the comment in that file -- pybabel's Python tokenizer chokes
on this project's f-strings), so a `_("Movies & Series")` in this module would
never reach messages.po and would silently fall back to English in the German
UI. `label_en` below is the API/log fallback; the translated string is the
literal `{{ _('...') }}` in templates/_library_nav.html.
"""

KIND_VIDEO = "video"
KIND_BOOK = "book"
KIND_MANGA = "manga"
KIND_COMIC = "comic"
KIND_MUSIC = "music"

# Ordered: this is the order of the sidebar sub-menu and the hub tiles.
#   slug       -- stable id, used in URLs (/library/<slug>), the DB column and
#                 the cache; never translate or rename it.
#   label_en   -- fallback only, see the module docstring.
#   available  -- False renders the entry greyed out with a "soon" badge and
#                 refuses to serve its page. Kept in the list on purpose: a
#                 planned section the user can see is a roadmap, a hidden one
#                 is a surprise.
#   scans      -- True if a path assigned to this kind actually has a scanner
#                 today. Only these are offered in the Settings multiselect,
#                 because assigning a folder to a kind that nothing indexes
#                 looks like a broken setting rather than a pending feature.
#   url        -- the /library/<url> path segment. Separate from `slug`
#                 because the slug is also a JSON key and a DB value, where the
#                 singular reads correctly ("this file is a book"), while the
#                 URL names a collection ("/library/books"). Renaming a slug
#                 later would invalidate stored rows; renaming a URL only
#                 breaks a bookmark.
MEDIA_KINDS = (
    {"slug": KIND_VIDEO, "url": "video",  "label_en": "Movies & Series", "available": True,  "scans": True},
    {"slug": KIND_BOOK,  "url": "books",  "label_en": "eBooks",          "available": True,  "scans": True},
    {"slug": KIND_MANGA, "url": "manga",  "label_en": "Manga",           "available": False, "scans": False},
    {"slug": KIND_COMIC, "url": "comics", "label_en": "Comics",          "available": True,  "scans": True},
    {"slug": KIND_MUSIC, "url": "music",  "label_en": "Music",           "available": False, "scans": False},
)

_BY_SLUG = {k["slug"]: k for k in MEDIA_KINDS}
_BY_URL = {k["url"]: k for k in MEDIA_KINDS}

ALL_SLUGS = tuple(k["slug"] for k in MEDIA_KINDS)
AVAILABLE_SLUGS = tuple(k["slug"] for k in MEDIA_KINDS if k["available"])
SCANNABLE_SLUGS = tuple(k["slug"] for k in MEDIA_KINDS if k["scans"])

# What a path is assigned to when nobody said otherwise -- and what every
# path already configured on an existing instance is migrated to.
#
# Note this is NOT what the old scanner did: before media_kinds, every path was
# walked for videos *and* books. Anyone who kept eBooks in a path therefore has
# to tick "eBooks" for it once after updating, or the book library shows up
# empty. That is a deliberate product decision (a path means one thing until
# its owner says otherwise), so the empty state of the book library links
# straight to the path settings instead of just saying "nothing here".
DEFAULT_KINDS = (KIND_VIDEO,)
DEFAULT_KINDS_CSV = ",".join(DEFAULT_KINDS)


def get_kind(slug):
    """The registry entry for a slug, or None."""
    return _BY_SLUG.get(str(slug or "").strip().lower())


def get_kind_by_url(url):
    """The registry entry for a /library/<url> segment, or None."""
    return _BY_URL.get(str(url or "").strip().lower())


def is_available(slug):
    """True if this kind has a real, reachable library page."""
    entry = get_kind(slug)
    return bool(entry and entry["available"])


def parse_kinds(value):
    """A stored media_kinds value -> an ordered list of known slugs.

    Accepts the CSV kept in the DB as well as a list coming straight off a
    JSON body, so callers do not each re-implement the split. Unknown and
    duplicate entries are dropped rather than raising: a kind removed in a
    later release must not make an existing path unreadable.

    An empty value falls back to DEFAULT_KINDS_CSV, never to "none". A row
    that somehow ends up blank -- a failed migration, a hand-edited DB, a
    restored backup from before this column existed -- has to keep showing its
    content. Failing open here is the difference between a wrong-looking
    setting and a library that appears to have lost every file in it.
    """
    raw = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out = []
    for item in raw:
        slug = str(item).strip().lower()
        if slug in _BY_SLUG and slug not in out:
            out.append(slug)
    return out or list(DEFAULT_KINDS)


def normalize_kinds(value):
    """Validate an incoming assignment and return it as the CSV the DB stores.

    Only scannable kinds survive. The UI does not offer the others, so their
    presence in a request body means either a stale client or a hand-crafted
    one; in both cases storing them would create a path that claims to hold
    manga while nothing on the server can ever index it.
    """
    kinds = [k for k in parse_kinds(value) if k in SCANNABLE_SLUGS]
    return ",".join(kinds or DEFAULT_KINDS)


def path_has_kind(value, kind):
    """True if a path assigned `value` should be scanned/listed for `kind`."""
    return kind in parse_kinds(value)


def kinds_for_api():
    """The registry in the shape the frontend and external modules consume."""
    return [
        {
            "slug": k["slug"],
            "url": k["url"],
            "label_en": k["label_en"],
            "available": k["available"],
            "scans": k["scans"],
        }
        for k in MEDIA_KINDS
    ]
