"""Direct Link routes (GitHub issue #8): probe a raw media URL (e.g. an
.m3u8 HLS master playlist, or any other yt-dlp-supported link) for its
available quality variants, then queue a download using the variant the
user picked.

TODO(telemetry): wire up flag.direct_link (usage counter) and
direct_link.urls (the URLs used, query-stripped -- see
telemetry.events.build_direct_link_event()) -- see telemetry/registry.py.
Registry-only for now.

Kept as its own route module rather than folded into routes/queue.py's
POST /api/download, since this feature has a different data shape (a
single raw URL + a yt-dlp format selector, no series/season/provider/
dub-sub-language concept) from the scraper-based download flow.

No Flask blueprint, matching the rest of web/routes/ (see queue.py's
module docstring): endpoint names stay bare so url_for() keeps working.
"""

from flask import jsonify
from flask import request

from .. import runtime_state
from ..auth import get_current_user
from ..db import add_to_queue
from ..db import is_series_queued_or_running
from ..queue_worker import _dl_lock


def register_direct_link_routes(app):
    """Register the Direct Link probe and queue-download endpoints."""

    @app.route("/api/direct-link/probe", methods=["POST"])
    def api_direct_link_probe():
        """Run yt-dlp against a raw URL (no download) and return the
        available quality variants.

        POST /api/direct-link/probe. Called from static/app.js's
        startDirectLinkProbe(), when the URL pasted into the Direct Link
        modal doesn't match one of the known scraper-site patterns.
        """
        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        if not url:
            return jsonify({"error": "url is required"}), 400

        from ...models.direct_link.probe import probe_direct_link_formats
        try:
            result = probe_direct_link_formats(url)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(result)

    @app.route("/api/direct-link/download", methods=["POST"])
    def api_direct_link_download():
        """Queue a Direct Link download job.

        POST /api/direct-link/download. Called from static/app.js's
        submitDirectLinkDownload(), once the user has picked a quality
        variant and entered a filename/save-location in the finalize modal.

        Direct-link jobs are stored as regular download_queue rows so the
        existing queue UI, history and worker retry/watchdog logic all keep
        working unchanged: episodes=[url] (single entry), provider='Direct'
        (the sentinel web/queue_worker.py checks to bypass
        mediaforge.providers.resolve_provider() and use DirectLinkEpisode
        instead), language='Original' (not applicable here, but the column
        is NOT NULL), format_id carries the yt-dlp format selector chosen in
        the format-picker modal, and source_provider carries the embed host
        (e.g. "VOE") the probe step detected, if any -- DirectLinkEpisode
        re-resolves through that host fresh at actual download time rather
        than reusing the (possibly short-lived, signed) URL from probing.
        """
        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        title = str(data.get("title", "")).strip() or "Direct Download"
        format_id = str(data.get("format_id", "")).strip() or "bestvideo+bestaudio/best"
        source_provider = str(data.get("provider", "")).strip() or None
        if not url:
            return jsonify({"error": "url is required"}), 400

        username = None
        if runtime_state.AUTH_ENABLED:
            user = get_current_user()
            if user:
                username = (
                    user.get("username")
                    if isinstance(user, dict)
                    else getattr(user, "username", None)
                )

        custom_path_id = data.get("custom_path_id")

        # Same lock routes/queue.py's api_download() uses, so a direct-link
        # job and a scraper-site job can't race on the duplicate check.
        with _dl_lock:
            if is_series_queued_or_running(url, requested_episodes=[url]):
                return jsonify({"error": "Dieser Link befindet sich bereits in der Warteschlange."}), 400

            queue_id = add_to_queue(
                title,
                url,
                [url],
                "Original",
                "Direct",
                username,
                custom_path_id=custom_path_id,
                format_id=format_id,
                source_provider=source_provider,
            )
        return jsonify({"queue_id": queue_id})
