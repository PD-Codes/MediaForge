"""Extensions overview — admin page listing every discovered
web/thirdparties/<name>/ folder and its load status.

Extracted as a plain route-registration function (no Flask blueprint,
same convention as routes/integrations.py) so it can be dropped into
create_app() with one register_extensions_routes(app) call.

Unlike a plugin's own settings card (which only shows up once it
successfully registered), this page reads
web/thirdparties/registry.py's resolve_extensions_overview(), which is
fed by every phase of web/thirdparties/__init__.py's
discover_and_register() — so a folder that failed to import, had no
register(app), or was skipped for an unmet DEPENDS_ON still shows up
here, with the reason. It's meant as the "why isn't my integration
showing up" page.
"""

from flask import jsonify, render_template

from ..thirdparties import rescan_new_modules
from ..thirdparties.registry import resolve_extensions_overview


def register_extensions_routes(app):
    """Register the Extensions overview page on the given Flask app."""

    @app.route("/extensions")
    def extensions_page():
        """Render the Extensions overview page. Route: GET /extensions."""
        return render_template("extensions.html", extensions=resolve_extensions_overview())

    @app.route("/api/extensions/rescan", methods=["POST"])
    def api_extensions_rescan():
        """Modulmanager's "Refresh" button -- scans web/thirdparties/ for
        folders not yet registered and registers them live, no app restart
        needed. See web/thirdparties/__init__.py's rescan_new_modules()
        docstring for exactly what this can and can't do (adding a new
        folder: yes; picking up code changes to or fully removing an
        already-registered one: no, both still need a restart)."""
        new_names = rescan_new_modules(app)
        return jsonify({"new_modules": new_names, "extensions": resolve_extensions_overview()})
