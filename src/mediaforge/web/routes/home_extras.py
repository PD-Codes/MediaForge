"""Home page 2.1 endpoints: media-server profile link, Wrapped, onboarding,
search suggestions.

These live in their own module rather than in browse.py because they answer
questions *about* the home page instead of filling one of its rows:

    /api/mediaplayer/users     which Jellyfin/Plex user is this account?
    /api/mediaplayer/image     artwork proxy for the media server
    /api/home/wrapped          the monthly recap card
    /api/home/onboarding       "what is still missing on this instance?"
    /api/home/suggest          the instant search dropdown

All of them are read-only and all of them degrade to an empty answer rather
than an error: none is important enough to break the home page over.

Registered from routes/browse.py (like routes/home_panels.py), so the home
page keeps a single registration entry point in create_app().
"""

from __future__ import annotations

from flask import Response
from flask import jsonify
from flask import request

from ...logger import get_logger
from .. import mediaplayer
from ..db import get_setting

logger = get_logger(__name__)

# How many suggestions the dropdown shows per group. Deliberately small: a
# list you have to scan is not faster than pressing Enter.
_SUGGEST_PER_GROUP = 4


def _current_user():
    from ..request_context import get_current_user_info
    try:
        return get_current_user_info()
    except Exception:
        return None, False


def _user_prefs():
    from flask import session
    from ..db import get_user_ui_prefs
    try:
        uid = session.get("user_id")
        return get_user_ui_prefs(uid) if uid is not None else {}
    except Exception:
        return {}


def register_home_extras_routes(app):
    """Register the home-page 2.1 endpoints on *app*."""

    # ------------------------------------------------------------ media server
    @app.route("/api/mediaplayer/users")
    def api_mediaplayer_users():
        """The media server's user list, for the profile dropdown.
        GET /api/mediaplayer/users.

        Names only -- no tokens, no e-mail addresses, nothing that identifies
        a person beyond the display name every other user of that server can
        already see in its own picker. The list is what the *linking* UI
        offers; the check that actually protects a history is
        mediaplayer.resolve_user(), which every read goes through.
        """
        if not mediaplayer.is_configured():
            return jsonify({"configured": False, "users": [], "linked": "",
                            "server": ""})
        cfg = mediaplayer.config()
        return jsonify({
            "configured": True,
            "server": cfg.get("kind", ""),
            "users": mediaplayer.list_users(),
            "linked": (_user_prefs().get("mediaplayer_user") or ""),
        })

    @app.route("/api/mediaplayer/image")
    def api_mediaplayer_image():
        """Artwork proxy for Jellyfin/Plex. GET /api/mediaplayer/image?path=…

        Exists so the browser never needs the media server's address or its
        token: Plex refuses an image without one, and a Jellyfin URL handed
        to the client breaks for everybody who reaches MediaForge from
        outside that LAN. The path is validated inside mediaplayer.py --
        server-relative only, no protocol-relative and no absolute URLs.
        """
        data, ctype = mediaplayer.image_bytes(request.args.get("path", ""))
        if not data:
            return Response(status=404)
        resp = Response(data, mimetype=ctype)
        # Artwork for a resume point changes about as often as the episode
        # does, and the row re-renders on every home page visit.
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    # ------------------------------------------------------------ Wrapped card
    @app.route("/api/home/wrapped")
    def api_home_wrapped():
        """The recap card. GET /api/home/wrapped?period=YYYY-MM.

        Two halves that stand on their own:

        * *watched* comes from the linked Jellyfin/Plex user, and is simply
          absent when no profile is linked or the server cannot be reached.
          It is never faked from MediaForge's own playback positions -- those
          only know about files MediaForge itself downloaded and played, so
          they would understate the number and quietly make the card lie.
        * *downloaded* is always MediaForge's own: how many titles, how much
          data, the biggest single download, the busiest source.
        """
        username, _is_admin = _current_user()
        period = str(request.args.get("period", "") or "").strip()
        start, end, start_iso, end_iso, period = _period_bounds(period)

        out = {"period": period, "watched": {"available": False},
               "downloaded": _wrapped_downloads(username, start_iso, end_iso)}

        linked = (_user_prefs().get("mediaplayer_user") or "").strip()
        if linked and mediaplayer.is_configured():
            try:
                out["watched"] = mediaplayer.watch_stats(linked, start, end)
            except Exception:
                logger.debug("[Wrapped] media-server stats failed", exc_info=True)
        return jsonify(out)

    # ------------------------------------------------------------- onboarding
    @app.route("/api/home/onboarding")
    def api_home_onboarding():
        """What a fresh instance still needs. GET /api/home/onboarding.

        Every step reports done/not-done plus the page that fixes it. Steps
        only an admin can act on are omitted for a normal account rather than
        shown greyed out -- a checklist you are not allowed to finish is not a
        checklist, it is a complaint.
        """
        _username, is_admin = _current_user()
        steps = []

        def step(key, done, link, admin_only=False):
            if admin_only and not is_admin:
                return
            steps.append({"key": key, "done": bool(done), "link": link})

        try:
            from .browse import _feed_source_enabled, _FEED_BUILTIN_META
            any_source = any(_feed_source_enabled(sid)
                             for sid, _label, _color in _FEED_BUILTIN_META)
        except Exception:
            any_source = True
        # Every link here is a real tab id, checked against the templates:
        # #sources and #library exist on /settings, the TMDB key lives on
        # /integrations#cineinfo (there is no CineInfo tab under Settings at
        # all), and the media-server picker is on /profile. A checklist that
        # sends you to a page that does not scroll anywhere is worse than no
        # checklist.
        step("sources", any_source, "/settings#sources", admin_only=True)
        step("tmdb", bool((get_setting("cineinfo_tmdb_api_key", "") or "").strip()),
             "/integrations#cineinfo", admin_only=True)

        # "Has a library" is asked of the CACHE, not of the configured paths:
        # a path that was added but never scanned is exactly the state this
        # step is meant to catch.
        try:
            from ..db import get_all_library_cache
            has_library = bool(get_all_library_cache())
        except Exception:
            has_library = True
        step("library", has_library, "/settings#library", admin_only=True)

        try:
            from ..thirdparties.registry import known_module_names
            has_module = bool(known_module_names())
        except Exception:
            has_module = True
        step("modules", has_module, "/extensions")

        # /settings#account never existed -- there is no such tab, and
        # /settings redirects a non-admin anyway, so this step sent exactly
        # the people who need it to a page they cannot open. The picker lives
        # on the profile page.
        step("mediaplayer",
             bool((_user_prefs().get("mediaplayer_user") or "").strip())
             or not mediaplayer.is_configured(),
             "/profile#mediaplayer")

        done = (_user_prefs().get("home_onboarding_done") or "") == "1"
        return jsonify({
            "steps": steps,
            "open": len([s for s in steps if not s["done"]]),
            "dismissed": done,
        })

    # -------------------------------------------------------------- home modes
    @app.route("/api/home/mode", methods=["POST"])
    def api_home_mode():
        """Switch the active home mode. POST {"mode": "...", "max_fsk": "12",
        "pin": "…"}.

        The age ceiling lives here rather than in /api/user/preferences on
        purpose (see db.PROTECTED_UI_PREF_KEYS): *lowering* it is always
        allowed, but *raising* it -- which is what leaving a kids mode means
        -- needs the PIN an admin set under Settings, when one is set. A kids
        mode that any client can turn off with one request is decoration.
        """
        from flask import session
        from ..db import set_user_ui_prefs

        uid = session.get("user_id")
        if uid is None:
            return jsonify({"ok": False, "error": "no session"}), 403

        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode", "") or "").strip()
        raw_fsk = str(data.get("max_fsk", "") or "").strip()
        if raw_fsk not in ("", "0", "6", "12", "16", "18"):
            return jsonify({"ok": False, "error": "invalid limit"}), 400

        # Entering a restricted mode requires that an admin actually turned it
        # on and set a PIN. Checked here and not only in the UI: a mode you
        # can enter but nobody can leave (because no PIN exists) would lock
        # the account out of its own home page.
        pin = (get_setting("home_kids_pin", "") or "").strip()
        kids_on = get_setting("home_kids_enabled", "0") == "1" and bool(pin)
        if raw_fsk and not kids_on:
            return jsonify({"ok": False, "error": "kids-disabled"}), 409

        prefs = _user_prefs()
        current = (prefs.get("home_max_fsk") or "").strip()
        # "" means "no ceiling", so it is the highest value there is -- an
        # ordering the plain string comparison would get exactly backwards.
        def _rank(value):
            return 99 if not value else int(value)

        if _rank(raw_fsk) > _rank(current):
            # No PIN configured means nobody ever armed this, so there is
            # nothing to check -- and nothing to prompt for either. Asking for
            # a PIN that does not exist and then accepting whatever is typed
            # is worse than not asking: it teaches that the lock works.
            if pin:
                given = str(data.get("pin", "") or "").strip()
                # Constant-time compare: this is a short secret being checked
                # by an endpoint anyone may call as often as they like.
                import hmac
                if not given or not hmac.compare_digest(given, pin):
                    return jsonify({"ok": False, "error": "pin"}), 403

        patch = {"home_max_fsk": raw_fsk}
        if mode:
            patch["home_mode"] = mode
        ok, err = set_user_ui_prefs(uid, patch, allow_protected=True)
        if not ok:
            return jsonify({"ok": False, "error": err or "save failed"}), 400
        return jsonify({"ok": True, "mode": mode, "max_fsk": raw_fsk})

    # -------------------------------------------------------------- suggestions
    @app.route("/api/home/suggest")
    def api_home_suggest():
        """Instant search suggestions. GET /api/home/suggest?q=…

        Three groups, cheapest first: what is already in the library (a local
        dict lookup), what the TMDB cache already knows (no network call --
        the point of this endpoint is that it answers between keystrokes),
        and the account's own recent searches.

        Deliberately does NOT ask the providers: a scrape per keystroke is
        what the full search is for, and five sites cannot answer in the time
        a dropdown has.
        """
        query = str(request.args.get("q", "") or "").strip()
        if len(query) < 2:
            return jsonify({"query": query, "groups": []})
        needle = query.lower()
        groups = []

        library = _suggest_library(needle)
        if library:
            groups.append({"key": "library", "items": library})

        username, _is_admin = _current_user()
        favourites = _suggest_favourites(needle, username)
        if favourites:
            groups.append({"key": "watchlist", "items": favourites})

        return jsonify({"query": query, "groups": groups})


# ------------------------------------------------------------------ helpers

def _period_bounds(period):
    """Return ``(start_epoch, end_epoch, start_iso, end_iso, "YYYY-MM")``.

    Defaults to the month that just ended: a recap of a month that is three
    days old is not a recap. Two representations because the two halves of the
    card read different stores -- the media servers speak epoch seconds, the
    download_history table stores "YYYY-MM-DD HH:MM:SS" text.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    text = str(period or "")
    valid = (len(text) == 7 and text[4] == "-"
             and text[:4].isdigit() and text[5:].isdigit()
             and 1 <= int(text[5:]) <= 12
             and 2000 <= int(text[:4]) <= now.year + 1)
    if valid:
        year, month = int(text[:4]), int(text[5:])
    else:
        month, year = (month - 1, year) if month > 1 else (12, year - 1)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
           else datetime(year, month + 1, 1, tzinfo=timezone.utc))
    fmt = "%Y-%m-%d %H:%M:%S"
    return (int(start.timestamp()), int(end.timestamp()),
            start.strftime(fmt), end.strftime(fmt), "%04d-%02d" % (year, month))


def _wrapped_downloads(username, start_iso, end_iso):
    """MediaForge's own numbers for the period. Thin wrapper: the aggregation
    itself is one SQL round trip in db.get_download_period_recap()."""
    try:
        from ..db import get_download_period_recap
        return get_download_period_recap(username, start_iso, end_iso)
    except Exception:
        logger.debug("[Wrapped] download history unavailable", exc_info=True)
        return {"count": 0, "size_mb": 0.0, "top_sources": [], "top_titles": [],
                "biggest": None}


def _suggest_library(needle):
    """Titles already on disk. Substring match on the folder name -- the same
    thing the Library page's own search does."""
    out = []
    try:
        from .library import lib_iter_cached_titles, lib_path_keys_for_kind
        from ..db import get_all_library_cache
        from ..media_kinds import KIND_VIDEO
        active = lib_path_keys_for_kind(KIND_VIDEO)
        seen = set()
        for path_key, entry in (get_all_library_cache() or {}).items():
            if path_key not in active:
                continue
            for title in lib_iter_cached_titles(entry.get("data")):
                name = str(title.get("folder") or "")
                low = name.lower()
                if needle not in low or low in seen:
                    continue
                seen.add(low)
                out.append({
                    "title": name,
                    "sub": ("movie" if title.get("is_movie") else "series"),
                    "episodes": title.get("total_episodes") or 0,
                    "href": "/library?q=" + name,
                })
                if len(out) >= _SUGGEST_PER_GROUP:
                    return out
    except Exception:
        logger.debug("[Suggest] library lookup failed", exc_info=True)
    return out


def _suggest_favourites(needle, username):
    """The account's own watchlist.

    This is the second group instead of "titles from the sources": a provider
    scrape per keystroke is what the full search is for, and five sites cannot
    answer in the time a dropdown has. The favourites table is a local lookup
    that already carries a real URL, so picking an entry opens the series
    directly rather than starting a search for its own name.
    """
    out = []
    try:
        from ..db import get_favourites
        for fav in (get_favourites(added_by=username) or []):
            name = str(fav.get("title") or "")
            if needle not in name.lower():
                continue
            out.append({
                "title": name,
                "sub": str(fav.get("provider") or ""),
                "url": str(fav.get("series_url") or ""),
                "href": "",
            })
            if len(out) >= _SUGGEST_PER_GROUP:
                break
    except Exception:
        logger.debug("[Suggest] favourites lookup failed", exc_info=True)
    return out
