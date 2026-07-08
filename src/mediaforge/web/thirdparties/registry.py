"""Shared registry third-party integrations use to plug into two places
without any other file needing to change:

1. The sidebar's Discover section (a link, shown only while enabled).
2. The Integrations -> Third Party settings tab (a collapsible card with a
   generic enable/disable toggle backed by the shared
   ``/api/settings/thirdparty/<id>`` API registered once in
   :func:`register_generic_settings_routes`).

A thirdparty's own ``__init__.py`` calls :func:`register_thirdparty` once,
from its ``register(app)`` function — see
``web/thirdparties/anime_seasons/__init__.py`` for a full worked example.
That single call is enough for both integration points: nothing in app.py,
base.html or integrations.html needs to be touched to add a new one.
"""

_ITEMS: list = []


def register_thirdparty(*, item_id: str, label: str, endpoint: str, icon_svg: str,
                         enabled_setting_key: str, badges=None, description: str = "",
                         enable_label: "str | None" = None, enable_desc: str = "",
                         page_id: "str | None" = None, extra_settings: "list | None" = None) -> None:
    """Register (or replace) a third-party integration.

    - item_id: unique key; re-registering the same id replaces it instead of
      duplicating it (safe under the debug reloader).
    - label: English source string for the sidebar link / card title,
      translated at render time via flask_babel.gettext (same catalog as
      the Jinja ``_()`` calls).
    - endpoint: Flask endpoint name (blueprint-qualified, e.g.
      "anime_seasons.anime_seasons_page"), resolved with url_for() when
      building the sidebar link.
    - icon_svg: raw ``<svg>...</svg>`` markup for the sidebar link
      (stroke="currentColor" so it inherits the sidebar's icon color).
    - enabled_setting_key: app_settings key gating both the sidebar link
      and (indirectly) whatever the integration's own routes check.
    - badges: list of (text, css_color) tuples shown as small pills on the
      settings card header, e.g. [("Jikan", "#2e51a2"), ("Menu", "#7c3aed")].
    - description: hint text shown at the top of the settings card.
    - enable_label / enable_desc: label/description for the card's enable
      toggle row. enable_label defaults to "Enable {label}".
    - page_id: value for the sidebar-link's data-page attribute (used to
      highlight the active link). Defaults to item_id.
    - extra_settings: optional list of additional boolean toggle rows shown
      below the master enable toggle, e.g.
      ``[{"key": "anime_seasons_show_adult", "label": "Show adult content",
      "description": "...", "default": "0"}]``. Each dict needs "key" (the
      app_settings key this toggle reads/writes) and "label"; "description"
      and "default" ("0"/"1", defaults to "0") are optional. These are read
      and saved generically by the same
      ``/api/settings/thirdparty/<item_id>`` GET/PUT pair the master toggle
      uses (see register_generic_settings_routes) — no per-integration route
      needed just for a plain on/off setting. An integration that needs
      something more elaborate than a checkbox (free text, a dropdown, a
      test button, ...) should still add its own routes instead.
    """
    global _ITEMS
    _ITEMS = [i for i in _ITEMS if i["id"] != item_id]
    _ITEMS.append({
        "id": item_id,
        "label": label,
        "endpoint": endpoint,
        "icon_svg": icon_svg,
        "enabled_setting_key": enabled_setting_key,
        "badges": list(badges or []),
        "description": description,
        "enable_label": enable_label or f"Enable {label}",
        "enable_desc": enable_desc,
        "page_id": page_id or item_id,
        "extra_settings": list(extra_settings or []),
    })


def get_thirdparty(item_id: str):
    for item in _ITEMS:
        if item["id"] == item_id:
            return item
    return None


def resolve_discover_menu_items() -> list:
    """Return the currently-enabled sidebar entries, ready for base.html:
    ``[{url, label, icon, page}, ...]``. Called from app.py's context
    processors on every request."""
    from flask import url_for
    from flask_babel import gettext as _gt
    from ..db import get_setting

    out = []
    for item in _ITEMS:
        try:
            if get_setting(item["enabled_setting_key"], "0") != "1":
                continue
            out.append({
                "url": url_for(item["endpoint"]),
                "label": _gt(item["label"]),
                "icon": item["icon_svg"],
                "page": item["page_id"],
            })
        except Exception:
            # A missing endpoint or a transient DB hiccup should never break
            # the sidebar for every other page.
            continue
    return out


def resolve_settings_cards() -> list:
    """Return every registered integration (enabled or not), ready for
    integrations.html's Third Party tab. The toggle's current on/off state
    is fetched client-side (see static/integrations.js), so this doesn't
    need to touch the DB."""
    from flask_babel import gettext as _gt

    out = []
    for item in _ITEMS:
        out.append({
            "id": item["id"],
            "title": _gt(item["label"]),
            "badges": [(_gt(text), color) for text, color in item["badges"]],
            "description": _gt(item["description"]) if item["description"] else "",
            "enable_label": _gt(item["enable_label"]),
            "enable_desc": _gt(item["enable_desc"]) if item["enable_desc"] else "",
            "extra_settings": [
                {
                    "key": s["key"],
                    "label": _gt(s["label"]),
                    "description": _gt(s["description"]) if s.get("description") else "",
                }
                for s in item.get("extra_settings", [])
            ],
        })
    return out


def register_generic_settings_routes(app) -> None:
    """One shared GET/PUT pair covering the simple "just an enable toggle"
    case every registered thirdparty gets for free. An integration that
    needs more than a single toggle (extra fields, test buttons, ...) can
    still add its own additional routes in its own routes.py — this generic
    pair only ever touches ``enabled_setting_key``."""
    from flask import jsonify, request
    from ..db import get_setting, set_setting

    @app.route("/api/settings/thirdparty/<item_id>", methods=["GET"])
    def api_thirdparty_settings_get(item_id):
        item = get_thirdparty(item_id)
        if not item:
            return jsonify({"error": "unknown"}), 404
        extra = {
            s["key"]: get_setting(s["key"], s.get("default", "0"))
            for s in item.get("extra_settings", [])
        }
        return jsonify({"enabled": get_setting(item["enabled_setting_key"], "0"), "extra": extra})

    @app.route("/api/settings/thirdparty/<item_id>", methods=["PUT"])
    def api_thirdparty_settings_put(item_id):
        item = get_thirdparty(item_id)
        if not item:
            return jsonify({"error": "unknown"}), 404
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            set_setting(item["enabled_setting_key"], "1" if str(data["enabled"]) == "1" else "0")
        # Only ever writes keys this item itself registered via
        # extra_settings -- an unrecognized key in the "extra" payload is
        # silently ignored rather than allowing an arbitrary app_settings
        # write from client input.
        extra_keys = {s["key"] for s in item.get("extra_settings", [])}
        for key, value in (data.get("extra") or {}).items():
            if key in extra_keys:
                set_setting(key, "1" if str(value).lower() in ("true", "1") else "0")
        return jsonify({"ok": True})
