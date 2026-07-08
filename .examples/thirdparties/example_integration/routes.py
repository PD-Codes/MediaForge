"""Example Integration — Third Party integration reference implementation.

Fully self-contained: its own Blueprint (own templates/ and static/ folders,
so nothing lives in the shared web/templates or web/static trees), its own
service module (service.py), and its own register(app) entry point (see
__init__.py) that plugs into ..registry for the sidebar entry and the
Integrations -> Third Party settings card.

This file intentionally mirrors web/thirdparties/anime_seasons/routes.py's
shape (a real, shipped integration) but with the smallest possible amount
of actual logic, so it's easy to read top to bottom and copy from.
"""

from flask import Blueprint, jsonify, redirect, render_template, url_for

from ...db import get_setting
from .service import get_items
from ....logger import get_logger

logger = get_logger(__name__)

SETTING_KEY = "example_integration_enabled"

bp = Blueprint(
    "example_integration",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/thirdparties/example_integration/static",
)


def _enabled() -> bool:
    return get_setting(SETTING_KEY, "0") == "1"


@bp.route("/example-integration")
def index():
    """Serve GET /example-integration: the demo grid page, or redirect home
    if the integration is disabled in Settings -> Integrations -> Third
    Party. Every page route in an integration should start with this same
    enabled-check — the sidebar link is hidden while disabled, but the URL
    itself would still work without this guard."""
    if not _enabled():
        return redirect(url_for("index"))
    return render_template("example_integration.html")


@bp.route("/api/example-integration/items")
def api_items():
    """Return the (cached) demo item list as JSON. Route: GET
    /api/example-integration/items. Called from
    static/example_integration.js's loadItems()."""
    if not _enabled():
        return jsonify({"error": "disabled", "items": []}), 403
    return jsonify({"items": get_items()})
