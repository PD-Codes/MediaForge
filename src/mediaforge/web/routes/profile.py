"""The account's own profile page.

Everything that is *yours* rather than the instance's, in one place:
appearance, language, your home page, your Jellyfin/Plex identity, your
password.

It exists as its own route rather than as a tab on /settings because
/settings is admin-only (see app.py's `_admin_only`). Until now that meant a
normal account could not reach its own theme, its own accent colour or its own
media-server profile at all — those controls existed, but only on a page that
redirects everyone who is not an operator. The Jellyfin/Plex picker landed in
the Start Page modal for exactly that reason, which is the wrong home for it.

Nothing here is admin-gated and nothing here touches instance settings: every
write goes to the current session's own account.
"""

from __future__ import annotations

from flask import jsonify
from flask import render_template
from flask import request
from flask import session

from ...logger import get_logger
from ..db import get_user_by_id
from ..db import set_user_password
from ..db import verify_user

logger = get_logger(__name__)


def register_profile_routes(app):
    """Register the profile page and its one write endpoint."""

    @app.route("/profile")
    def profile_page():
        """Serve GET /profile."""
        account = get_user_by_id(session.get("user_id")) or {}
        return render_template("profile.html", account=account)

    @app.route("/api/user/password", methods=["POST"])
    def api_user_password():
        """Change the signed-in account's own password.
        POST {"current": "...", "new": "..."}.

        The current password is required even though the session is already
        authenticated: a session left open on a shared machine is exactly the
        situation where someone else would change it, and re-asking costs one
        field.
        """
        uid = session.get("user_id")
        username = session.get("user_name")
        if uid is None or not username:
            return jsonify({"ok": False, "error": "no session"}), 403

        account = get_user_by_id(uid) or {}
        if account.get("auth_method") and account["auth_method"] != "local":
            # An SSO account has no password here to change -- the identity
            # provider owns it, and pretending otherwise would store one that
            # can never be used to log in.
            return jsonify({"ok": False, "error": "sso"}), 400

        data = request.get_json(silent=True) or {}
        current = str(data.get("current", "") or "")
        new_password = str(data.get("new", "") or "")

        # verify_user() returns a (user_or_None, error) PAIR, and a tuple is
        # always truthy -- `if not verify_user(...)` was never true, so the
        # check did nothing at all and any string was accepted as the current
        # password. Unpacked deliberately rather than indexed, so the same
        # mistake cannot come back silently.
        verified, _reason = verify_user(username, current)
        if not verified:
            return jsonify({"ok": False, "error": "wrong-password"}), 403
        if new_password == current:
            return jsonify({"ok": False, "error": "unchanged"}), 400

        ok, err = set_user_password(uid, new_password)
        if not ok:
            return jsonify({"ok": False, "error": err or "failed"}), 400
        return jsonify({"ok": True})
