"""Home panel registry (the home page's button bar and its one panel).

The home page answers "what do I want to watch". It never answered "what is
this instance doing right now" -- that lives on /queue, /stats, /settings and
four more pages, which means the one page everybody opens first is the one
page that tells them nothing.

The button bar fixes that without turning the home page into a dashboard: a
row of buttons under the search field, and *one* panel below it whose content
depends on the button. One panel at a time is the whole point -- it keeps the
page short, it keeps the poster rows where they were, and it means a visit
costs exactly one extra request instead of one per widget.

This module is the list of buttons. The built-ins are declared in
``web/routes/home_panels.py`` (they need the database and the request
context), and third-party modules add theirs through
:func:`register_home_panel` -- same ``item_id`` convention as every other
secondary registry, so ``web/thirdparties/registry.py``'s
``unregister_module()`` drops them when a module is disabled.

Deliberately free of any ``mediaforge.web`` import: this is core, the web
layer imports it, never the other way round. Same rule as
:mod:`mediaforge.home_feed`.
"""

from __future__ import annotations

from .logger import get_logger

logger = get_logger(__name__)

# Panel ids the built-in bar already owns. A module that picks one of these
# would silently shadow a core panel, so it is refused instead.
RESERVED_PANEL_IDS = frozenset({"queue", "activity", "library", "storage", "system"})

# Named client-side actions a panel may ask for instead of a link. The queue
# is a modal in base.html, not a page (there is no /queue route), so "open the
# queue" cannot be expressed as an href. Keeping this a fixed list -- rather
# than letting a payload name a JS function -- means a module panel can open
# the queue but nothing else.
PANEL_ACTIONS = frozenset({"queue"})

# How many list entries one panel may return. A panel is a glance, not a page:
# anything longer belongs behind its "open" link, and an unbounded list from a
# module would push the poster rows off the screen.
PANEL_MAX_ITEMS = 12

# item_id -> entry dict
_EXTRA_HOME_PANELS: dict = {}


def register_home_panel(item_id, panel_id, label, view,
                        badge=None, admin_only=False, icon=None,
                        badge_label=None, badge_suffix=None, badge_tone=None,
                        multi=False):
    """Add a button (and its panel) to the home page's button bar.

    - ``item_id``: the id the module already passed to ``register_thirdparty()``.
      Registrations are keyed by it so ``unregister_module()`` can drop them
      automatically.
    - ``panel_id``: the id used in the API payload and stored as the user's
      last choice. Must not collide with a built-in
      (:data:`RESERVED_PANEL_IDS`) or with another registered panel.
    - ``label``: what the button says. Already translated by the module --
      the core cannot translate a string it has never seen.
    - ``view``: ``fn()`` returning the panel body as a dict, see below. Called
      on demand, only when the user actually opens the panel.
    - ``badge``: optional ``fn()`` returning an int (or ``None``). This one
      runs on *every* home page visit for every registered panel, so it must
      be cheap -- a COUNT, not a scrape. Returning ``0``/``None`` shows no
      badge.
    - ``badge_label``: optional, and strongly recommended whenever ``badge``
      is set: what the number MEANS, as the button's tooltip. Already
      translated, like ``label``. A bare number next to a word is guesswork --
      the built-in System button showed "58" for months and was read as a
      version and as an error code before anyone worked out it counted failed
      downloads. Use ``{}`` where the number should appear; without it the
      count is appended.
    - ``badge_suffix``: optional unit shown after the number, up to four
      characters ("%", "GB"). Without one a badge is always read as "how many
      things are waiting for me", which is wrong for a level.
    - ``badge_tone``: how loud the badge is -- ``"info"`` (default, the accent
      colour, "there is something here"), ``"err"`` (something is wrong),
      ``"level"`` (a percentage that turns amber at 90 and red at 95), or
      ``"muted"`` (a plain fact, e.g. a total). Anything else falls back to
      ``"info"``.
    - ``admin_only``: when true the panel is not listed for, and not readable
      by, a non-admin account. Enforced server-side in both routes; hiding a
      button in the template would leave the data one fetch away.
    - ``icon``: optional inline SVG path data (the ``d`` attribute of a single
      path, stroked, 24x24 viewBox). Anything else is dropped -- this ends up
      inside an ``<svg>``, and a module is not a trusted source of markup.
    - ``multi``: whether the Dashboard's "Add widget" menu may add more than
      one card of this panel. Every instance shows the same ``view()`` output
      -- there is no per-instance configuration -- so this only makes sense
      for a panel whose data is itself a shuffle/sample or otherwise varies
      each time it renders. Defaults to ``False`` (one card, offered only
      while none is on the board).

    The ``view`` dict may carry, all optional:

        {
          "stats": [{"label": str, "value": str, "tone": "ok|warn|err"}],
          "items": [{"title": str, "sub": str, "percent": int,
                     "href": str, "action": str, "tone": "ok|warn|err"}],
          "link":  {"href": str, "label": str},
          "empty": str,
        }

    Everything is plain text and is escaped by the client -- there is no way
    to hand HTML through this on purpose. ``href`` must be a site-relative
    path starting with ``/``; anything else is dropped, so a panel cannot turn
    into an off-site redirect. ``action`` is an alternative to ``href`` for the
    few things that are modals rather than pages (:data:`PANEL_ACTIONS`) --
    "queue" opens the queue hub; anything else is dropped.
    """
    if not callable(view):
        raise ValueError("register_home_panel: view must be callable")
    if badge is not None and not callable(badge):
        raise ValueError("register_home_panel: badge must be callable or None")
    panel_id = str(panel_id or "").strip().lower()
    if not panel_id or not all(c.isalnum() or c in "_-" for c in panel_id):
        raise ValueError("register_home_panel: panel_id must be alphanumeric")
    if panel_id in RESERVED_PANEL_IDS:
        raise ValueError("register_home_panel: %r is a built-in panel id" % panel_id)
    for existing_id, entry in _EXTRA_HOME_PANELS.items():
        if entry["panel_id"] == panel_id and existing_id != item_id:
            raise ValueError("register_home_panel: panel id already registered: %r" % panel_id)

    _EXTRA_HOME_PANELS[item_id] = {
        "panel_id": panel_id,
        "label": str(label or panel_id),
        "view": view,
        "badge": badge,
        "badge_label": str(badge_label or "")[:120],
        "badge_suffix": str(badge_suffix or "")[:4],
        "badge_tone": (badge_tone if badge_tone in ("info", "err", "level", "muted")
                       else "info"),
        "admin_only": bool(admin_only),
        "icon": _safe_icon(icon),
        "multi": bool(multi),
    }
    logger.info("[HomePanels] Registered third-party panel: %s (%s%s)",
                panel_id, item_id, ", admin only" if admin_only else "")


def unregister_home_panel(item_id) -> None:
    """Drop a panel previously added via :func:`register_home_panel`."""
    removed = _EXTRA_HOME_PANELS.pop(item_id, None)
    if removed:
        logger.info("[HomePanels] Unregistered third-party panel: %s (%s)",
                    removed["panel_id"], item_id)


def thirdparty_home_panel_ids() -> set:
    """item_ids that currently own a home panel. Read-only counterpart of
    :func:`unregister_home_panel`, used by the Modulmanager's capability list."""
    return set(_EXTRA_HOME_PANELS)


def iter_home_panels() -> list:
    """Every registered third-party panel, as a list of copies.

    Copies, because the caller merges these with the built-ins and tags them
    with per-request state (badge value, reachable) -- mutating the registry
    from inside a request would leak that state into the next one.

    Panels belonging to a switched-off module are left out -- see
    module_gate.py for why the enabled check lives at the point of use.
    """
    from .module_gate import filter_enabled

    return [
        {
            "panel_id": entry["panel_id"],
            "label": entry["label"],
            "view": entry["view"],
            "badge": entry["badge"],
            "badge_label": entry["badge_label"],
            "admin_only": entry["admin_only"],
            "icon": entry["icon"],
            "multi": entry["multi"],
            "item_id": item_id,
        }
        for item_id, entry in filter_enabled(_EXTRA_HOME_PANELS).items()
    ]


def _safe_icon(value) -> str:
    """Allow only SVG path data through -- the value is rendered into a path
    element, and a module is not a trusted source of markup. Same reasoning as
    home_feed._safe_color()."""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) > 400:
        return ""
    allowed = set("MmLlHhVvCcSsQqTtAaZz0123456789 .,-+eE")
    if any(c not in allowed for c in text):
        return ""
    return text
