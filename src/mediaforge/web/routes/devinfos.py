"""Dev Infos routes -- a changelog/status feed pulled from a fixed,
unauthenticated admin server (a separate standalone app, unrelated to this
codebase) and cached locally.

The feed source (devinfos_monitor.DEVINFOS_SERVER_URL) is a hardcoded source
constant -- not admin-configurable -- so there is intentionally no settings
API here, only read-only display routes.

Extracted as a plain route-registration function (no Flask blueprint:
endpoint names stay bare so url_for() keeps working), matching every other
first-party feature module under web/routes/.
"""

from datetime import datetime

from ..db import get_devinfo_count
from ..db import get_devinfo_posts
from ..db import mark_devinfo_read
from ..devinfos_monitor import request_immediate_refresh
from ..markdown_utils import render_markdown
from flask import jsonify
from flask import render_template


def _format_devinfo_timestamp(raw):
    """Turn the devInfo server's ISO-8601 ``remote_created_at`` (e.g.
    "2026-07-11T23:26:46.305977+00:00") into "HH:MM DD.MM.YY" -- the raw ISO
    string was previously shown as-is in templates/devinfos.html and
    static/devinfos.js, which is correct but not something anyone should have
    to read. Formatted once here (Python, not duplicated in JS) since both
    the server-rendered page and the JSON status endpoint the JS refresh path
    polls go through _posts_with_rendered_html() below. Falls back to the raw
    string if it isn't parseable, rather than hiding a real timestamp."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%H:%M %d.%m.%y")
    except ValueError:
        return str(raw)


def _posts_with_rendered_html():
    """Return cached Dev Info posts with an extra ``body_html`` key holding
    the sanitized Markdown-rendered HTML for ``body`` (alongside the
    original raw ``body``, kept as-is for any consumer that wants the raw
    source), and a ``formatted_time`` key holding the human-readable
    "HH:MM DD.MM.YY" rendering of ``remote_created_at``."""
    return [
        {
            **post,
            "body_html": render_markdown(post.get("body")),
            "formatted_time": _format_devinfo_timestamp(post.get("remote_created_at")),
        }
        for post in get_devinfo_posts()
    ]


def register_devinfos_routes(app):
    """Register the Dev Infos page and its supporting status API on the
    given Flask app."""

    @app.route("/devinfos")
    def devinfos_page():
        """Dev Infos changelog/status feed -- visible to any logged-in user
        (or anyone if auth is disabled), same visibility level as other
        regular content pages. Not admin-gated.

        Route: GET /devinfos.
        """
        # Kick the poller so newly published posts show up without waiting
        # for the next scheduled 5-minute round -- rate-limited internally so
        # repeated visits/clicks cannot hammer the remote server.
        request_immediate_refresh()
        return render_template("devinfos.html", posts=_posts_with_rendered_html())

    @app.route("/api/devinfos/status")
    def api_devinfos_status():
        """Cached Dev Info post count + posts, for the sidebar badge poll
        (static/devinfos.js) and the page's own live refresh.

        ``count`` is the *unread* count (see db.get_devinfo_count()) -- what
        the sidebar badge shows. Each post in ``posts`` carries its own
        ``is_read`` flag for the page's per-post "mark as read" button.

        Route: GET /api/devinfos/status.
        """
        return jsonify({
            "count": get_devinfo_count(),
            "posts": _posts_with_rendered_html(),
        })

    @app.route("/api/devinfos/<post_id>/read", methods=["POST"])
    def api_devinfos_mark_read(post_id):
        """Mark one Dev Info post as read.

        Read state is instance-wide (see db.mark_devinfo_read()), matching
        the rest of this feature -- the underlying feed itself has no
        per-user concept either. Returns the updated unread count so the
        caller (static/devinfos.js) can update the sidebar badge immediately
        instead of waiting for its next 60s poll.

        Route: POST /api/devinfos/<post_id>/read.
        """
        existed = mark_devinfo_read(post_id)
        if not existed:
            return jsonify({"error": "unknown post id"}), 404
        return jsonify({"ok": True, "count": get_devinfo_count()})
