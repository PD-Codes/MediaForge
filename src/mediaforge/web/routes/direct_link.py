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

import re

from flask import jsonify
from flask import request

from .. import runtime_state
from ..auth import get_current_user
from ..db import add_to_queue
from ..db import is_series_queued_or_running
from ..queue_worker import _dl_lock

# Provider (site) name as returned by mediaforge.providers.resolve_provider ->
# the source key the frontend knows (and gates, in hanime's case).
_PROVIDER_TO_SOURCE = {
    "AniWorld": "aniworld",
    "SerienStream": "sto",
    "FilmPalast": "filmpalast",
    "Megakino": "megakino",
    "MegakinoFilm": "megakino",
    "Hanime": "hanime",
}

# Cut a season/episode URL back to its series page:
#   .../anime/stream/<slug>/staffel-1/episode-3 -> .../anime/stream/<slug>
#   .../serie/<slug>/staffel-1/episode-3        -> .../serie/<slug>
_SERIES_TRIM = re.compile(
    r"^(https?://[^/]+/(?:anime/stream|serie(?:/stream)?)/[a-zA-Z0-9\-]+)(?:/.*)?$",
    re.IGNORECASE,
)


def _series_url_for(url, source):
    """The series/movie landing URL the detail modal should be opened with."""
    if source in ("aniworld", "sto"):
        m = _SERIES_TRIM.match(url)
        return m.group(1) if m else url
    # megakino (?episode=N) and hanime (?ep=N) use synthetic query episodes;
    # filmpalast has no series concept at all — its /stream/<slug> page IS the
    # movie. In all three cases the bare page URL is what openSeries() wants.
    return url.split("?")[0].split("#")[0]


def register_direct_link_routes(app):
    """Register the Direct Link classify, probe and queue-download endpoints."""

    @app.route("/api/direct-link/classify", methods=["POST"])
    def api_direct_link_classify():
        """Decide what a pasted URL actually is.

        POST /api/direct-link/classify. Called from static/app.js's
        submitDirectLink() as the FIRST step, before any probing: a link to one
        of MediaForge's own scraper sites must go through the normal
        series/season flow (with its provider + language pickers), and only
        everything else is a "direct link" in the yt-dlp sense.

        The lookup runs against the same single source of truth the rest of the
        app uses -- mediaforge.providers.resolve_provider() and its URL
        patterns -- instead of a second, hand-maintained set of regexes in the
        frontend that silently missed sites (FilmPalast) and every mirror
        domain. Mirror hosts (serienstream.to, a bare origin IP, ...) are first
        rewritten back to the site's canonical host (see mediaforge.mirrors), so
        a link copied from a mirror opens the series just like the primary
        domain does.

        Returns either:
            {"kind": "site", "source": "sto", "series_url": "https://s.to/serie/x"}
        or:
            {"kind": "generic"}   -- not one of our sites: probe it with yt-dlp
        """
        from ...mirrors import canonical_host, map_url, site_for_url
        from ...providers import normalize_url, resolve_provider

        data = request.get_json(silent=True) or {}
        raw = str(data.get("url", "")).strip()
        if not raw:
            return jsonify({"error": "url is required"}), 400

        url = normalize_url(raw)

        # A mirror domain (or bare IP) points at the same site — normalize it
        # back to the canonical host so the URL patterns below match.
        site = site_for_url(url)
        if site:
            host = canonical_host(site)
            if host:
                url = map_url(url, host)

        try:
            provider = resolve_provider(url)
        except ValueError:
            return jsonify({"kind": "generic", "url": url})

        source = _PROVIDER_TO_SOURCE.get(provider.name)
        if not source:
            return jsonify({"kind": "generic", "url": url})

        return jsonify({
            "kind": "site",
            "source": source,
            "url": url,
            "series_url": _series_url_for(url, source),
        })

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
