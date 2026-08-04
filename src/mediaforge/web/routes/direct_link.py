"""Direct Link routes (GitHub issue #8): probe a raw media URL (e.g. an
.m3u8 HLS master playlist, or any other yt-dlp-supported link) for its
available quality variants, then queue a download using the variant the
user picked.

Telemetry: flag.direct_link (stage-2 usage counter) is submitted once per
download the user actually starts; direct_link.urls (stage-4, the
query-stripped URL) only when the link's ORIGIN could be established
server-side and is not the age-gated 18+ provider -- see
_report_direct_link_download() below. The classify and probe steps stay silent:
both can run repeatedly for a single link the user is still deciding on, so
counting them would report "a request came in" instead of "the feature was
used".

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
from ...logger import get_logger
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events
from ...telemetry.sanitize import is_adult_provider


logger = get_logger(__name__)

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


# mirrors/providers spell the age-gated 18+ site "hanime", and the probe step
# may label it with its hoster name ("Hanime"). Telemetry's hard-coded guard
# (sanitize.is_adult_provider) matches the literal "hanime_tv" only, so every
# spelling that can reach this module is translated below -- otherwise the guard
# silently never fires.
_ADULT_SPELLINGS = frozenset({"hanime", "hanime_tv", "hanimetv", "hanime.tv"})


def _telemetry_spelling(name):
    """Normalize a site/provider/hoster label to the spelling the telemetry
    adult guard uses, or None for an empty label."""
    key = str(name or "").strip().lower()
    if not key:
        return None
    return "hanime_tv" if key in _ADULT_SPELLINGS else key


def _site_source_for(url):
    """The source key of the MediaForge scraper site *url* belongs to (the same
    values api_direct_link_classify() returns), or None when the URL is not one
    of them.

    Deliberately the exact same three steps as that endpoint -- normalize, map a
    mirror host back to the canonical one, then match through
    providers.resolve_provider() -- so the origin is decided by one source of
    truth instead of a second host list that would miss every mirror domain.
    When no URL pattern matches but mirrors still recognizes the host, that
    host's site wins: a bare site URL must not read as "origin unknown".

    Fully guarded: if the lookup itself fails the answer is None, i.e. "could
    not be established" -- never an optimistic guess.
    """
    try:
        from ...mirrors import canonical_host, map_url, site_for_url
        from ...providers import normalize_url, resolve_provider
        url = normalize_url(str(url or "").strip())
        if not url:
            return None
        site = site_for_url(url)
        if site:
            host = canonical_host(site)
            if host:
                url = map_url(url, host)
        try:
            provider = resolve_provider(url)
        except ValueError:
            return site
        # A provider outside the map is exactly what classify() answers
        # "generic" for, so it does not establish an origin on its own either.
        return _PROVIDER_TO_SOURCE.get(provider.name) or site
    except Exception:
        return None


def _telemetry_provider_for(url, source_provider=None):
    """Best-effort ORIGIN of a pasted link, in the spelling the telemetry adult
    guard uses, or None when the origin cannot be established.

    Two independent signals, both evaluated SERVER-side -- the /classify step
    runs in the frontend and this route can be POSTed to directly, so nothing
    the client may or may not have done can be assumed:

      * the URL itself, via _site_source_for(): catches a scraper-site link
        pasted straight into the Direct Link dialog, mirror domains included;
      * ``source_provider``: the hoster the probe step detected ("VOE",
        "Hanime", ...) and the client sent back with the download.

    If EITHER signal names the age-gated site the answer is "hanime_tv", so one
    signal can never vote the other's 18+ verdict away. If neither names
    anything, None means "undetermined", which the caller treats as "do not send
    the URL" and NOT as "safe": Direct Link exists for raw stream/CDN URLs, and a
    signed hanime .m3u8 is served from a host that has nothing to do with
    hanime.tv (see extractors/provider/hanime.py and models/hanime_tv/
    episode.py), so a host-based check alone cannot clear such a link.
    """
    site = _telemetry_spelling(_site_source_for(url))
    hoster = _telemetry_spelling(source_provider)
    if is_adult_provider(site) or is_adult_provider(hoster):
        return "hanime_tv"
    return site or hoster


def _report_direct_link_download(url, source_provider=None):
    """Submit the stage-2 usage counter (flag.direct_link) and -- only for a link
    whose origin is both established and harmless -- the stage-4 URL event
    (direct_link.urls) for one queued direct-link download.

    Fires once per download the user actually starts, never for classify/probe.
    The URL is query-stripped inside events.build_direct_link_event() (that is
    where hoster session tokens live); no title, path or queue id is sent.

    Three outcomes, decided by _telemetry_provider_for():

      * the age-gated 18+ provider -> nothing at all, since flag.hanime_tv is
        the only data point that ever exists for it (telemetry/sanitize.py:
        is_adult_provider);
      * origin undetermined -> flag.direct_link only. The counter carries no URL
        and is therefore harmless, while the URL is withheld because an
        unidentified raw CDN link may well BE that provider's content --
        build_direct_link_event() takes no provider and cannot run the guard on
        our behalf (see its docstring). "In doubt, do not send" is the same rule
        syncplay_rooms._room_content_provider() follows.
      * a known, non-adult origin -> counter plus URL.

    Wrapped in its own try/except so a telemetry bug can never break the
    download endpoint.
    """
    try:
        provider = _telemetry_provider_for(url, source_provider)
        if is_adult_provider(provider):
            return
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.direct_link"))
        if not provider:
            return
        telemetry_client.submit(telemetry_events.build_direct_link_event(url))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit direct-link events", exc_info=True)


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

        from ...models.direct_link.probe import UnsafeUrlError, probe_direct_link_formats
        try:
            result = probe_direct_link_formats(url)
        except UnsafeUrlError as e:
            # SSRF gate (shared with the HLS proxy): never echo which internal
            # address was refused, that alone would make this a LAN scanner.
            logger.warning(f"[DirectLink] Probe rejected an unsafe URL: {e}")
            return jsonify({"error": "This URL cannot be probed."}), 400
        except Exception:
            # The upstream exception can carry internal hostnames/IPs and
            # connection details -- log it, but return a generic message.
            logger.exception(f"[DirectLink] Probe failed for {url}")
            return jsonify({"error": "Could not read this link."}), 400
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
        # Telemetry: one usage counter per started download, plus the URL only
        # when the origin could be pinned down server-side.
        _report_direct_link_download(url, source_provider)
        return jsonify({"queue_id": queue_id})
