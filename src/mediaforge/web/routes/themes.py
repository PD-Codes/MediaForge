"""Theme pack routes — listing, activation and asset delivery.

Same plain register_themes_routes(app) convention as every other routes file
(no blueprint). Three groups:

- ``GET /api/themes`` — every installed theme plus the instance default.
  Login-level (secure_endpoints wraps it): regular users need it to populate
  their personal theme picker in Settings → Design.
- ``PUT /api/themes/active`` — the admin's instance-wide default. Admin-only
  (listed in app.py's _admin_only).
- ``GET /themes/<folder>/bundle.css`` and ``GET /themes/<folder>/<asset>`` —
  the stylesheets and assets themselves. Exempt from auth like /static: the
  login page renders through base.html's head too, and a stylesheet request
  carrying no session must not bounce to a login redirect. Nothing here is
  sensitive — themes are, by validation, CSS/fonts/images only.

Security notes (the parts worth re-reading before changing anything):

- Assets are served with send_from_directory, which resolves and confines the
  path — plus an explicit folder-name check so ``..`` never even reaches it.
- Only whitelisted extensions are served (themes.ALLOWED_EXTENSIONS minus the
  signature file); a file that somehow bypassed install validation is still
  refused at the door here. Defense in depth, not redundancy.
- SVG gets ``Content-Security-Policy`` and ``X-Content-Type-Options`` headers:
  an SVG can carry <script>, and while a stylesheet reference never executes
  it, a user navigating to the asset URL directly would. The CSP turns that
  into an inert image.
"""

from flask import jsonify, make_response, request, send_from_directory

from .. import themes


# The signature file is verified at install time; serving it adds nothing and
# leaks key ids to anonymous visitors, so it stays private.
_UNSERVED = {".sig"}


def register_themes_routes(app):
    """Register theme pack API + asset routes on the app."""

    @app.route("/api/themes")
    def api_themes():
        """Installed theme packs + the instance default. Route: GET /api/themes.

        Available to every logged-in user (not admin-gated): the personal
        theme override picker in Settings → Design is built from this.
        """
        return jsonify({
            "ok": True,
            "themes": themes.installed_themes(),
            "active": (themes.active_theme() or {}).get("folder", ""),
            "builtin_id": themes.BUILTIN_THEME_ID,
        })

    @app.route("/api/themes/active", methods=["PUT"])
    def api_themes_active():
        """Set the instance-wide default theme. Route: PUT /api/themes/active.

        Admin-only (app.py's _admin_only): this changes what every user who
        has not set a personal override sees.
        """
        data = request.get_json(silent=True) or {}
        ok, error = themes.set_active_theme(str(data.get("folder") or ""))
        if not ok:
            return jsonify({"ok": False, "error": error}), 400
        active = themes.active_theme()
        return jsonify({"ok": True, "active": (active or {}).get("folder", "")})

    @app.route("/themes/<folder>/bundle.css")
    def theme_bundle_css(folder):
        """All of a theme's declared stylesheets as one response.

        The URL shape is deliberately constructible from the folder name alone
        (no manifest knowledge needed) so base.html's pre-paint override script
        can swap themes client-side. ETag'd on version+mtime so a theme update
        busts caches without any build step.
        """
        css, etag = themes.bundle_css(folder)
        if css is None:
            return ("/* no such theme */", 404, {"Content-Type": "text/css; charset=utf-8"})
        if request.if_none_match.contains(etag):
            resp = make_response("", 304)
            resp.set_etag(etag)
            return resp
        resp = make_response(css)
        resp.headers["Content-Type"] = "text/css; charset=utf-8"
        resp.set_etag(etag)
        # Cacheable but always revalidated: a theme update must show up on the
        # next reload, and the 304 path above makes revalidation nearly free.
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/themes/<folder>/<path:asset>")
    def theme_asset(folder, asset):
        """One raw theme file (font, image, individual CSS). See module
        docstring for the confinement story."""
        # Only VALID installed themes serve assets at all. This is what makes
        # install-time validation (no scripts, no symlinks) actually binding
        # for hand-copied folders too: an invalid folder is listed in the
        # Modulmanager with its reasons, but nothing in it is ever served.
        theme = themes.theme_by_folder(folder)
        if theme is None or not theme["valid"]:
            return jsonify({"error": "not found"}), 404
        root = themes.themes_dir() / theme["folder"]

        suffix = ("." + asset.rsplit(".", 1)[-1].lower()) if "." in asset else ""
        if suffix in _UNSERVED or suffix not in themes.ALLOWED_EXTENSIONS:
            return jsonify({"error": "not found"}), 404

        # send_from_directory confines `asset` inside root; the symlink case
        # is already impossible here (a theme containing one is invalid), the
        # resolve check is belt and braces against TOCTOU-style edits.
        target = (root / asset)
        try:
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                return jsonify({"error": "not found"}), 404
        except OSError:
            return jsonify({"error": "not found"}), 404

        resp = send_from_directory(root, asset, max_age=3600)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if suffix == ".svg":
            # An <img>/CSS reference never runs SVG scripts, but a direct
            # navigation would. This header makes even that path inert.
            resp.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:;")
        return resp
