"""The one place that answers "what may this session see?".

Two things can limit an account, and they must not be two implementations:

* the **kids role** (``users.role = 'kids'``) — assigned by an admin, permanent
  for that account, with nothing to switch off. This is the case for a child
  who has their own login.
* the **kids mode** (the per-account ``home_max_fsk`` preference, entered from
  the home page and left with a PIN) — for a shared account where a child is
  sitting in front of it right now.

``ceiling()`` collapses both into one number, and every enforcement point in
the app asks it rather than looking at roles or preferences itself. The point
of a single function is that adding a *third* way to be limited later cannot
miss a page: there is exactly one thing to change.

What is enforced where:

    home feed         routes/browse.py      (_feed_apply_age_limit)
    search            routes/search.py
    advanced search   routes/search.py
    library + player  routes/library.py, routes/stream.py
    downloads         routes/queue.py
    settings/modules  web/app.py (the blanket route gate)

The honest limit of all of this: it can only judge a title TMDB has a
certification for. Anything unrated is *shown*, deliberately — dropping every
unrated title would empty the app on an instance without a TMDB key, and an
empty app is one people switch the protection off for. The one restriction
that does not depend on ratings at all is the adult source, which is excluded
by media type before anything is fetched.
"""

from __future__ import annotations

from .db import get_setting

# What "kids" means when nobody said otherwise. Matches the default of the
# home-page mode, so a role and a mode limit to the same thing.
_DEFAULT_KIDS_FSK = "6"


def _session_role() -> str:
    from flask import session
    try:
        return str(session.get("user_role") or "user")
    except Exception:
        return "user"


def is_kids_account() -> bool:
    """True when the logged-in account IS a kids account (the role).

    Distinct from being *in* kids mode: a kids account cannot leave, is never
    offered the mode switch, and is refused everywhere the mode only filters.
    """
    return _session_role() == "kids"


def kids_max_fsk() -> str:
    """The instance's configured age limit, as a string ("" never happens)."""
    value = (get_setting("home_kids_max_fsk", _DEFAULT_KIDS_FSK) or "").strip()
    return value if value in ("0", "6", "12", "16") else _DEFAULT_KIDS_FSK


def ceiling():
    """The age ceiling for this request, or ``None`` when there is none.

    The role wins over the preference and is not merely combined with it: a
    kids account that also had a *higher* mode value stored would otherwise
    talk itself out of its own restriction.
    """
    if is_kids_account():
        return int(kids_max_fsk())

    from flask import session
    from .db import get_user_ui_prefs
    try:
        uid = session.get("user_id")
        raw = (get_user_ui_prefs(uid).get("home_max_fsk") or "").strip() if uid is not None else ""
    except Exception:
        raw = ""
    return int(raw) if raw.isdigit() else None


def has_session() -> bool:
    """Whether this request carries a logged-in account at all.

    Not every request does: the external REST API authenticates with a key,
    the calendar feed with a query-string token, and a module can register a
    route with either. ``ceiling()`` answers ``None`` for those -- correctly,
    since there is no account whose preference could be read -- and callers
    that only asked "is there a limit?" then read that as "no limit".
    """
    from flask import session
    try:
        return session.get("user_id") is not None
    except Exception:
        return False


def allows_adult() -> bool:
    """Whether the 18+ source may be fetched for this request at all.

    A request with no session is refused rather than waved through. The rule
    used to be "no ceiling means no limit", and with no session there is no
    ceiling -- so any route authenticating by something other than a cookie
    (the v1 API, the ICS feed, a module's own endpoint) got the adult source
    by default, without anyone having decided that.

    Opting in to the adult source is a per-account decision, and an account is
    exactly what these requests do not have. Refusing is the only answer that
    does not invent one.
    """
    if not has_session():
        return False
    limit = ceiling()
    return limit is None or limit >= 18


def rating_of(item) -> "int | None":
    """The age rating carried by a result/library item, or None if unrated.

    Accepts the two shapes the app passes around: a browse/search result with
    an inlined ``tmdb`` dict, and a library title with a flat ``fsk``.
    """
    if not isinstance(item, dict):
        return None
    value = item.get("fsk")
    if value in (None, ""):
        value = (item.get("tmdb") or {}).get("fsk") if isinstance(item.get("tmdb"), dict) else None
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def permits(item, limit=None) -> bool:
    """Whether *item* may be shown. Unrated items pass — see the module note."""
    limit = ceiling() if limit is None else limit
    if limit is None:
        return True
    rating = rating_of(item)
    return rating is None or rating <= limit


def filter_items(items, limit=None) -> list:
    """Drop everything rated above the ceiling. Safe on None/empty."""
    if not items:
        return items if isinstance(items, list) else []
    limit = ceiling() if limit is None else limit
    if limit is None:
        return items
    return [item for item in items if permits(item, limit)]


def filter_library_titles(titles, limit=None) -> list:
    """Drop library titles rated above the ceiling.

    A scanned title carries no rating -- the library knows file names, not
    certifications -- so the ratings come from the TMDB cache, looked up in ONE
    bulk query for the whole shelf rather than per title. A library with
    thousands of entries is exactly where a per-item lookup would turn a page
    load into a minute.

    Uncached titles are kept, for the same reason unrated ones are (see the
    module note). This is the weakest of the enforcement points and says so:
    it is a filter over metadata that may not exist, not a permission check.
    The one that cannot be talked around is the playback gate in
    routes/stream.py, which refuses the file itself.
    """
    if not titles:
        return titles if isinstance(titles, list) else []
    limit = ceiling() if limit is None else limit
    if limit is None:
        return titles

    from flask import session
    from .db import get_tmdb_cache_bulk
    country = get_setting("cineinfo_country", "DE") or "DE"
    try:
        ui_lang = session.get("ui_language", "de")
    except Exception:
        ui_lang = "de"

    names = [str(t.get("folder") or "") for t in titles if isinstance(t, dict)]
    keys = {name: name + "|||" + country + "|||" + ui_lang for name in names if name}
    try:
        cached = get_tmdb_cache_bulk(list(keys.values())) or {}
    except Exception:
        return titles          # no cache, no opinion -- do not hide the library

    out = []
    for title in titles:
        if not isinstance(title, dict):
            continue
        hit = cached.get(keys.get(str(title.get("folder") or ""), ""))
        if permits(hit or {}, limit):
            out.append(title)
    return out
