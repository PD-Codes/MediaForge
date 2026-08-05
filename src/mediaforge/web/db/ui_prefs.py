"""Per-account appearance preferences.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import re
import sqlite3
from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


_CREATE_USER_UI_PREFS_TABLE = """\
CREATE TABLE IF NOT EXISTS user_ui_prefs (
    user_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key)
);
"""

# Whitelist of storable keys with a validator each. Anything not listed is
# rejected by set_user_ui_prefs — the values end up in a <style>/<link> the
# browser trusts, so "user-supplied string" is not something to wave through.
_THEME_FOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_HOME_FEED_FILTER_RE = re.compile(r"^[a-z0-9_,;:-]{0,200}$")
# A media-server user id: a Jellyfin GUID or a numeric Plex account id. Kept
# strict because the value is interpolated into a request path.
_MEDIAPLAYER_USER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Home modes: "id:label|id:label|…" with the flags appended per mode. The label
# is user text, so the charset stays narrow -- it is rendered into a button.
_HOME_MODE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_HOME_MODES_RE = re.compile(r"^[\w À-ɏ.,'&+()!?/:;|=-]{0,2000}$")
_WRAPPED_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def _valid_theme_pack(value: str) -> bool:
    # '' = follow the instance default, 'default' = built-in look,
    # anything else must look like an installed theme folder name.
    return value in ("", "default") or bool(_THEME_FOLDER_RE.match(value))


USER_UI_PREF_KEYS = {
    "theme_pack": _valid_theme_pack,
    "theme_mode": lambda v: v in ("dark", "light"),
    "accent": lambda v: bool(_HEX_COLOR_RE.match(v)),
    # Library layout. Same reasoning as the appearance keys: a browser-local
    # choice is one the user loses on their next device, and the Library
    # silently fell back to the poster grid every single visit.
    "library_view": lambda v: v in ("grid", "list"),
    "library_per_page": lambda v: v in ("10", "20", "50", "100"),
    # Home feed chip filters, stored as "s:<off sources>;t:<off types>". Same
    # reasoning as the library layout: a filter that resets on every device is
    # a filter the user sets again every day. The charset is deliberately
    # narrow -- the value is only ever parsed, never rendered, but it is still
    # user input echoed back through window._USER_PREFS.
    "home_feed_filters": lambda v: bool(_HOME_FEED_FILTER_RE.match(v)),
    # Home feed layout: "o:<row order>;h:<hidden rows>;n:<cards per row>".
    # Only the parts the user actually changed are stored, so everything else
    # keeps following the instance default an admin set (Settings -> Start
    # Page). Empty string = "back to the default", which is why "" validates.
    "home_feed_layout": lambda v: bool(_HOME_FEED_FILTER_RE.match(v)),
    # eBook reader. Reading is a per-person habit -- text size, page colour and
    # whether you page or scroll -- and someone who set it up on the desktop
    # expects the same book to look the same on their phone. The ranges are
    # enforced here rather than trusted from the client, because these values
    # are echoed back through window._USER_PREFS.
    # The upper bound matches the clamp in static/reader.js. It has to: a value
    # the client is willing to produce and the server refuses does not fail
    # quietly -- /api/user/preferences rejects the WHOLE call on one bad key, so
    # a single out-of-range size threw away the paper colour and the reading
    # mode with it, and none of the reader's settings survived a reload.
    "reader_font": lambda v: v.isdigit() and 70 <= int(v) <= 220,
    "reader_theme": lambda v: v in ("dark", "sepia", "light"),
    "reader_flow": lambda v: v in ("paginated", "scrolled"),
    "reader_face": lambda v: v in ("serif", "sans", "original"),
    "reader_lead": lambda v: v in ("1.4", "1.65", "1.95"),
    "reader_width": lambda v: v in ("34", "44", "62"),
    # Which home page layout THIS account sees. "" means "follow the instance
    # default" (Settings -> Start Page, new_home_enabled), "1"/"0" overrule it.
    #
    # Per account and not just instance-wide, because the switch is an
    # invitation to try something: an admin who wanted to look at the new
    # layout for five minutes changed it for every account on the instance,
    # so nobody could try it without imposing it. The rows and filters of the
    # same page have worked this way (own value, falling back to the
    # instance default) since Start Page 2.0 -- this is the layout catching up.
    "new_home": lambda v: v in ("", "0", "1"),
    # The v1 banner that advertises the new layout: "1" once the user has
    # dismissed it, or has tried the new page at least once. Stored on the
    # account rather than in localStorage so "go away" survives the next
    # device -- the whole complaint about banners is having to close them
    # again and again.
    "new_home_promo_done": lambda v: v in ("", "0", "1"),
    # How tightly the home rows are packed. A 4K screen fits twice the cards a
    # laptop does; "comfortable" is the old, only, hardcoded size.
    "home_density": lambda v: v in ("", "comfortable", "compact", "list"),
    # Which media-server user this account is. "" = none, and the Continue
    # watching row keeps using MediaForge's own playback positions.
    # NOT validated against the server here (db.py must not do network I/O) --
    # web/mediaplayer.py's resolve_user() is the gate that matters; this only
    # keeps junk out of the table and out of window._USER_PREFS.
    "mediaplayer_user": lambda v: v == "" or bool(_MEDIAPLAYER_USER_RE.match(v)),
    # The setup checklist on a fresh install: "1" once the user dismissed it.
    "home_onboarding_done": lambda v: v in ("", "0", "1"),
    # Home modes (#8): the active preset id, plus the presets themselves as a
    # compact "id:label:flags" list. Kept in one row rather than one row per
    # preset so switching a mode is a single write.
    "home_mode": lambda v: v == "" or bool(_HOME_MODE_ID_RE.match(v)),
    "home_modes": lambda v: len(v) <= 2000 and bool(_HOME_MODES_RE.match(v)),
    # The Wrapped card is offered once per period; this remembers the last
    # period the user closed, e.g. "2026-07".
    "home_wrapped_seen": lambda v: v == "" or bool(_WRAPPED_PERIOD_RE.match(v)),
    # The "Advanced" appearance toggles. They lived in localStorage only,
    # which made them per BROWSER: the same account looked different on a
    # laptop and a phone, and clearing site data reset them silently. Same
    # reasoning as the theme/accent keys above -- a look you configured is a
    # look you expect to find again. "" = follow the default (off).
    "ui_glow_effect": lambda v: v in ("", "0", "1"),
    "ui_header_color": lambda v: v in ("", "0", "1"),
    "ui_header_color_help": lambda v: v in ("", "0", "1"),
    "ui_skeleton_loader": lambda v: v in ("", "0", "1"),
    "ui_choose_border": lambda v: v in ("", "0", "1"),
    "ui_active_download_glow": lambda v: v in ("", "0", "1"),
    "ui_click_effect": lambda v: v in ("", "0", "1"),
    "ui_icon_move": lambda v: v in ("", "0", "1"),
    # The age ceiling the home feed is filtered to. "" = no limit.
    # PROTECTED (see below): this one is a restriction, not a preference, so
    # it must not be writable through the generic preferences endpoint.
    "home_max_fsk": lambda v: v in ("", "0", "6", "12", "16", "18"),
}

# Keys that /api/user/preferences must refuse even though they live in the
# same table. A kids mode a client can switch off with one PUT to the
# generic preferences endpoint is decoration, not a restriction -- these are
# writable only through the endpoint that also checks the PIN
# (routes/home_extras.py's /api/home/mode), which passes allow_protected=True.
PROTECTED_UI_PREF_KEYS = frozenset({"home_max_fsk"})


def register_ui_pref_key(key: str, validator=None) -> None:
    """Let a module store its own per-user UI preference under *key*.

    Modules that add a UI toggle can persist it on the account through the
    same table and the same /api/user/preferences endpoint the core
    appearance settings use, instead of inventing per-browser localStorage
    state that a user loses on their next device.

        from mediaforge.web.db import register_ui_pref_key
        register_ui_pref_key("mymodule_compact_rows", lambda v: v in ("0", "1"))

    *validator* takes the string value and returns True if it may be stored;
    the default accepts any string of at most 200 characters. Prefix the key
    with the module id so two modules cannot collide. Values are echoed back
    into the page via window._USER_PREFS, so keep validators tight.
    """
    key = str(key or "").strip()
    if not key or not re.match(r"^[A-Za-z0-9_.-]{1,64}$", key):
        raise ValueError(f"Invalid UI preference key: {key!r}")
    if validator is None:
        def validator(value):
            return isinstance(value, str) and len(value) <= 200
    USER_UI_PREF_KEYS[key] = validator


def get_user_ui_prefs(user_id: int) -> dict:
    """Return every stored appearance preference for *user_id*.

    Unknown/invalid rows are dropped rather than returned: a key that was
    removed from the whitelist (or a value written by an older build that no
    longer validates) must not reach the template.
    """
    if user_id is None:
        return {}
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM user_ui_prefs WHERE user_id = ?", (user_id,)
        ).fetchall()
    except sqlite3.Error:
        return {}   # table not created yet (very first request during init)
    finally:
        conn.close()
    out = {}
    for row in rows:
        key, value = row["key"], row["value"]
        validator = USER_UI_PREF_KEYS.get(key)
        if validator and validator(value):
            out[key] = value
    return out


def set_user_ui_prefs(user_id: int, prefs: dict,
                      allow_protected: bool = False) -> "tuple[bool, str | None]":
    """Upsert appearance preferences for *user_id*.

    Returns (ok, error). Rejects the whole call on the first unknown key or
    invalid value instead of silently storing a subset — a half-applied
    appearance is harder to reason about than a failed save.

    *allow_protected* opens up PROTECTED_UI_PREF_KEYS. It defaults to False so
    the generic /api/user/preferences endpoint cannot write them by accident;
    only the endpoints that perform the matching check (the kids-mode PIN)
    pass True.
    """
    if user_id is None:
        return False, "No user in session"
    if not isinstance(prefs, dict) or not prefs:
        return False, "No preferences given"
    cleaned = {}
    for key, value in prefs.items():
        validator = USER_UI_PREF_KEYS.get(key)
        if validator is None:
            return False, f"Unknown preference: {key}"
        if key in PROTECTED_UI_PREF_KEYS and not allow_protected:
            return False, f"Preference is protected: {key}"
        value = "" if value is None else str(value)
        if not validator(value):
            return False, f"Invalid value for {key}"
        cleaned[key] = value
    conn = get_db()
    try:
        for key, value in cleaned.items():
            conn.execute(
                "INSERT INTO user_ui_prefs (user_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (user_id, key, value),
            )
        conn.commit()
        return True, None
    finally:
        conn.close()


def clear_user_ui_prefs(user_id: int) -> None:
    """Drop all appearance preferences for *user_id* (used by delete_user)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_ui_prefs WHERE user_id = ?", (user_id,))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
