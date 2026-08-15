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
# The user-arranged Dashboard grid, per card, comma separated. Three shapes
# are accepted (format v3, static/home_panels.js's parseLayout()):
#   v1 (oldest): "<card id>:<order>:<span>"           span = one of 3 tracks
#   v2 (CSS-Grid era): "<card id>:<order>:<colspan>:<rowspan>"
#                       colspan = 1-12, rowspan = 1-40 row units or "a" auto
#   v3 (current, free-position engine): "<card id>:<x>:<y>:<w>:<h>"
#                       x = 0-11 (column), y = 0-999 (row, unbounded upward
#                       in practice), w = 2-12 (column span), h = 3-80 (row
#                       span). All in the JS engine's grid units (COLS=12,
#                       ROW_H=24px, GAP=18px) -- see home_panels.js.
# v1/v2 rows are still accepted so an account that saved a layout before an
# upgrade does not lose it on the next read (static/home_panels.js migrates
# them once, client-side, and re-saves as v3); this file only ever WRITES v3
# from here on. Card ids include module panel ids, hence the wider charset;
# every number is bounded so the string cannot grow without limit.
_DASH_CARD_RE_V1 = re.compile(r"^[A-Za-z0-9_.-]{1,32}:\d{1,5}:[123]$")
_DASH_CARD_RE_V2 = re.compile(r"^[A-Za-z0-9_.-]{1,32}:\d{1,5}:(?:[1-9]|1[0-2]):(?:[1-9]|[1-3]\d|40|a)$")
_DASH_CARD_RE_V3 = re.compile(
    r"^[A-Za-z0-9_.-]{1,32}:(?:[0-9]|1[01]):[0-9]{1,3}:(?:[2-9]|1[0-2]):(?:[3-9]|[1-7][0-9]|80)$")
# Kept for anything that imported the old name directly (module code, if any)
# and as the "one entry" shape callers may want to validate on its own.
_DASH_CARD_RE = re.compile(r"^(?:%s|%s|%s)$" % (
    _DASH_CARD_RE_V1.pattern.strip("^$"), _DASH_CARD_RE_V2.pattern.strip("^$"),
    _DASH_CARD_RE_V3.pattern.strip("^$")))


def _valid_dash_layout(value: str) -> bool:
    if value == "":
        return True                      # "" = back to the built-in order
    # A v3 entry ("id:x:y:w:h", up to 32+1+2+1+3+1+2+1+2 = 45 chars) is only
    # marginally longer than the old v2 one (44 chars) -- 40 cards at the
    # longest realistic v3 entry is 40*45 + 39 separators = 1839, still
    # comfortably under 2000, so the cap does not need to move again.
    if len(value) > 2000:
        return False
    parts = value.split(",")
    if len(parts) > 40:                  # far more cards than the grid can hold
        return False
    return all(
        _DASH_CARD_RE_V1.match(part) or _DASH_CARD_RE_V2.match(part) or _DASH_CARD_RE_V3.match(part)
        for part in parts
    )



# Which base-id cards the account has closed (the "x" on a card) -- comma
# separated ids, same charset as a v3 layout row's id part. Only ever holds
# BASE ids (no ".", see home_panels.js): an extra instance ("queue.2") is
# simply not re-created by forgetCard()/the next poll, so it needs no entry
# here, only a base id's Add-menu re-render needs suppressing.
_DASH_HIDDEN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def _valid_dash_hidden(value: str) -> bool:
    if value == "":
        return True                      # "" = nothing hidden
    if len(value) > 2000:                # same cap reasoning as _valid_dash_layout
        return False
    parts = value.split(",")
    if len(parts) > 40:                  # far more cards than the grid can hold
        return False
    return all(_DASH_HIDDEN_ID_RE.match(part) for part in parts)


# Per-card placement/order override for the Dashboard sections layout: which
# of the 4 fixed sections a card was dragged into, and where among its
# section-mates it landed. "<card id>:<section>" pairs, comma separated, list
# ORDER doubles as the render order within each section (see
# static/home_panels.js's cardLayout()/saveCardLayout()). Absent = every card
# stays in its built-in section (SECTION_OF) in its built-in order, same
# "nothing stored yet" convention as home_dash_layout/home_dash_hidden.
_DASH_SECTION_IDS = ("mediaforge", "system", "stats", "modules")
_DASH_SECTION_ITEM_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,32}:(?:%s)$" % "|".join(_DASH_SECTION_IDS))


def _valid_dash_section_layout(value: str) -> bool:
    if value == "":
        return True
    if len(value) > 2000:                # same cap reasoning as _valid_dash_layout
        return False
    parts = value.split(",")
    if len(parts) > 40:                  # far more cards than the board can hold
        return False
    return all(_DASH_SECTION_ITEM_RE.match(part) for part in parts)


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
    # Dashboard card layout: which card sits where, how many of the 12 grid
    # columns it spans, and how tall it is (row units, or auto). Per account
    # rather than per browser for the same reason as the rest of this table --
    # an arrangement you made once is one you expect to find again on the next
    # device. Cards the user never moved are absent and keep their built-in
    # place, so a new card in a later release needs no migration.
    "home_dash_layout": _valid_dash_layout,
    # Whether the account has locked the dashboard grid against further
    # drag/resize (grip and resize handle hidden, pointer handlers become
    # no-ops -- see home_panels.js). Per account, like the layout itself:
    # "I arranged it, stop me from bumping it by accident" is a choice one
    # makes for their own view, not the whole instance's.
    "home_dash_locked": lambda v: v in ("", "1"),
    # Base-id cards the account closed with the card's "x" button. Per account
    # for the same reason as the layout itself: closing a card is an
    # arrangement choice, and the next poll/load must not just recreate it.
    "home_dash_hidden": _valid_dash_hidden,
    # Sections layout only: which section a card was dragged into and where
    # it landed among its section-mates. See _valid_dash_section_layout's own
    # comment above for the format.
    "home_dash_section_layout": _valid_dash_section_layout,
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
    # Which of the two home tabs this account last had open. Unknown keys make
    # the preferences endpoint reject the WHOLE request, so a tab switch would
    # otherwise have silently discarded every setting sent with it.
    "home_tab": lambda v: v in ("", "dash", "disc"),
    # How the Dashboard and Discover sections are arranged on the two-tab
    # home page. "" (or "1") is the default -- both as tabs, switched with
    # the tab pill. "0" drops the Dashboard entirely and always opens on
    # Discover, for people who just want the browse page. "all" renders both
    # stacked on ONE page (Dashboard above Discover) with no tab pill and no
    # switching at all -- the pre-tabs layout's shape, but built from the
    # same two sections rather than the old one-block-per-source rows (see
    # the "All in one page" thread: that would have meant fetching Discover
    # twice, once through home_feed.js's single batched request and once
    # through the classic layout's separate per-source loaders). Set from
    # "Customise this page", visible on both tabs.
    "home_dash_enabled": lambda v: v in ("", "0", "1", "all"),
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
    # "Could be for you" (the Discover-tab recommendation hero + rail),
    # split into two independent switches: "foryou_hidden" (its original
    # name, predating the split) hides the RAIL, "foryou_hero_hidden" hides
    # the rotating hero banner above it. "1" hides, "" (default) shows. Per
    # account, like every other Discover toggle -- a household member who
    # finds the guesses noisy should not have to also turn them off for
    # everyone else. Each also has an instance-default counterpart a fresh
    # account starts from (get_setting("foryou_hidden_default")/
    # ("foryou_hero_hidden_default"), same "account overrules instance"
    # relationship home_dash_enabled has -- see app.py's index()).
    "foryou_hidden": lambda v: v in ("", "0", "1"),
    "foryou_hero_hidden": lambda v: v in ("", "0", "1"),
    # TMDB ids the account dismissed with "Not interested" on that same row,
    # comma separated. Persisted so a skip survives a reload instead of only
    # lasting until the next visit -- the row used to forget it the moment
    # the tab was closed, and static/home_foryou.js's own "for_you" score
    # keeps re-nominating the same title as long as the library that produced
    # it does not change. Capped generously (recommend.MAX_ROW is 20, so a
    # few hundred covers years of use) so the value cannot grow without limit.
    "foryou_skipped": lambda v: v == "" or (
        len(v) <= 3000 and len(v.split(",")) <= 300
        and all(part.isdigit() for part in v.split(","))),
    # Dashboard layout: "" (default) groups every card into four fixed,
    # collapsible sections (MediaForge/System/Statistik/Module) with no
    # drag/resize; "grid" keeps the free-position card grid (Beta) that used
    # to be the only option. Per account, same reasoning as every other
    # Dashboard/Discover choice on this list.
    "home_dash_view": lambda v: v in ("", "grid"),
    # Section order for the view above, e.g. "system,mediaforge,stats,modules".
    # Missing sections keep the built-in order appended after the ones named
    # here (see static/home_panels.js's applySectionOrder()).
    "home_dash_section_order": lambda v: v == "" or bool(
        re.match(r"^(mediaforge|system|stats|modules)"
                 r"(,(mediaforge|system|stats|modules)){0,3}$", v)),
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
