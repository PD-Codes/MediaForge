"""Catalogue page and its API.

The full A-Z list of a source site, for bulk selection. See
``mediaforge/catalogue.py`` for where the lists come from and why they hold
titles only, and ``web/catalogue_worker.py`` for what happens after the user
confirms a selection.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from flask import jsonify, render_template, request

from ...catalogue import all_catalogues, cached_catalogue, get_catalogue
from ...logger import get_logger
from .. import catalogue_worker
from ..db import get_autosync_jobs, get_queue
from ..source_policy import source_enabled

logger = get_logger(__name__)

# There is deliberately NO cap on how many series one request may carry.
# Selecting the entire catalogue is a legitimate thing to want, and the parts
# that a huge selection could actually hurt are handled where they belong
# rather than by refusing the request: the expansion runs one series at a time
# in a background job (catalogue_worker), the page warns before submitting,
# and the job can be stopped at any point. What is still validated is that
# every url IS in a catalogue -- that is not a size limit, it is what keeps
# this from being a "scrape whatever I send you" endpoint.

# Concurrent bulk jobs. More than a couple would defeat the whole point of
# expanding series one at a time (see catalogue_worker._SERIES_DELAY).
MAX_ACTIVE_JOBS = 2


def _catalogue_sources():
    """Every catalogue that exists, with the enabled state the rest of the app
    uses. A source switched off in Settings is listed but marked, rather than
    hidden: it is the same list everywhere else in the app, and silently
    dropping an entry is how a user ends up thinking a source disappeared."""
    out = []
    for sid, meta in all_catalogues().items():
        out.append({
            "id": sid,
            "label": meta["label"],
            "kind": meta.get("kind", "series"),
            "enabled": bool(source_enabled(sid)),
            "cached": cached_catalogue(sid) is not None,
        })
    return out


def register_catalogue_routes(app):
    """Register the catalogue page and its API routes on the Flask app."""

    @app.route("/catalogue")
    def catalogue_page():
        """The full-catalogue browser. GET /catalogue.

        The language and hoster lists are handed to the template explicitly:
        they are NOT global context variables, only arguments the start page's
        own view passes (see web/app.py's index view) -- which is exactly why
        the two dropdowns rendered empty when this page assumed otherwise.
        """
        from ..runtime_state import WORKING_PROVIDERS
        from ...config import LANG_LABELS

        return render_template(
            "catalogue.html",
            lang_labels=LANG_LABELS,
            # Same three labels the start page hardcodes for SerienStream.
            sto_lang_labels={"1": "German Dub", "2": "English Dub",
                             "3": "English Dub (German Sub)"},
            supported_providers=WORKING_PROVIDERS,
        )

    @app.route("/api/catalogue/sources")
    def api_catalogue_sources():
        """Which catalogues exist and whether their source is enabled.
        GET /api/catalogue/sources. Called from static/catalogue.js."""
        return jsonify({"sources": _catalogue_sources()})

    @app.route("/api/catalogue/<source_id>")
    def api_catalogue(source_id):
        """The full list for one source. GET /api/catalogue/<source_id>.

        Answers 503 rather than 502 when the fetch fails: the list is a cache
        that will be there in a moment, not a broken endpoint, and the page
        offers a retry instead of an error state.
        """
        source_id = str(source_id or "").strip().lower()
        if source_id not in all_catalogues():
            return jsonify({"error": "unknown catalogue"}), 404
        if not source_enabled(source_id):
            # Same rule the browse routes follow for a disabled source: an
            # empty answer, not an error -- the user turned it off.
            return jsonify({"source": source_id, "entries": [], "disabled": True})

        force = request.args.get("refresh") == "1"
        entries = get_catalogue(source_id, force=force)
        if entries is None:
            return jsonify({"error": "catalogue unavailable", "source": source_id}), 503
        return jsonify({"source": source_id, "entries": entries, "count": len(entries)})

    @app.route("/api/catalogue/state")
    def api_catalogue_state():
        """Which series are already spoken for. GET /api/catalogue/state.

        Two flat url lists, resolved in ONE request rather than per row: the
        page renders up to eleven thousand of them and cannot ask per title.
        "Already in the library" is deliberately NOT here -- the client already
        holds the downloaded-folder list for its own badges and matches on it
        locally (see isDownloaded() in app.js).
        """
        queued, syncing = set(), set()
        try:
            for item in (get_queue() or []):
                url = (item.get("series_url") or "").rstrip("/")
                if url and item.get("status") in ("pending", "running", "paused"):
                    queued.add(url)
        except Exception as exc:
            logger.debug("[Catalogue] queue state failed: %s", exc)
        try:
            for job in (get_autosync_jobs() or []):
                url = (job.get("series_url") or "").rstrip("/")
                if url:
                    syncing.add(url)
        except Exception as exc:
            logger.debug("[Catalogue] autosync state failed: %s", exc)
        return jsonify({"queued": sorted(queued), "autosync": sorted(syncing)})

    @app.route("/api/catalogue/bulk", methods=["POST"])
    def api_catalogue_bulk():
        """Expand a selection into queue items or AutoSync jobs.
        POST /api/catalogue/bulk. Called from static/catalogue.js."""
        data = request.get_json(silent=True) or {}

        # Same rule as /api/download: a kids ACCOUNT may not download at all,
        # and a bulk action is not a way around that.
        from ..age_gate import is_kids_account
        if is_kids_account():
            return jsonify({"error": "not permitted", "code": "age_limited"}), 403

        mode = str(data.get("mode") or "queue").strip().lower()
        if mode not in ("queue", "autosync"):
            return jsonify({"error": "mode must be queue or autosync"}), 400

        urls = data.get("urls") or []
        if not isinstance(urls, list) or not urls:
            return jsonify({"error": "urls list is required"}), 400

        # Only urls that are actually in ONE OF the catalogues. The page shows
        # every source merged into a single list, so a selection legitimately
        # spans several of them and there is no "the" source to check against.
        # What has not changed is the reason for checking at all: a bulk
        # endpoint that scrapes whatever url it is handed is a request forgery
        # tool with a download queue attached.
        known = {}
        for sid in all_catalogues():
            entries = get_catalogue(sid)
            if entries is None:
                continue
            for entry in entries:
                known.setdefault(entry["url"].rstrip("/"), sid)
        if not known:
            return jsonify({"error": "catalogue unavailable"}), 503

        wanted, unknown, sources_used = [], 0, set()
        for raw in urls:
            url = str(raw or "").strip().rstrip("/")
            if url in known and url not in wanted:
                wanted.append(url)
                sources_used.add(known[url])
            elif url:
                unknown += 1
        if not wanted:
            return jsonify({"error": "no known urls in selection"}), 400
        if catalogue_worker.active_job_count() >= MAX_ACTIVE_JOBS:
            return jsonify({"error": "another bulk job is still running",
                            "code": "busy"}), 409

        language = str(data.get("language") or "German Dub")
        provider = str(data.get("provider") or "VOE")

        # A language group only works with per-language folders, and only as
        # long as the group still exists -- checked here for the same reason
        # /api/download checks it: a stale dropdown must not queue a hundred
        # items that are all guaranteed to fail later.
        from ..language_groups import is_group_ref, lang_separation_enabled, resolve_chain
        if is_group_ref(language):
            if not lang_separation_enabled():
                return jsonify({"error": "Sprachgruppen benötigen die Einstellung "
                                          "'Sprachen in Ordner trennen'."}), 400
            if not resolve_chain(language):
                return jsonify({"error": "Diese Sprachgruppe existiert nicht mehr."}), 400

        username = None
        from .. import runtime_state
        if runtime_state.AUTH_ENABLED:
            from ..request_context import get_current_user_info
            user = get_current_user_info() or {}
            username = user.get("username") if isinstance(user, dict) else None

        # Stage 2: that the feature was used, nothing about WHAT was selected.
        # The per-source counters and the download events already carry that,
        # each behind its own consent -- this one only answers "does anybody
        # use the Catalogue page at all".
        try:
            from ...telemetry import client as _tel_client
            from ...telemetry import events as _tel_events
            _tel_client.submit(_tel_events.build_feature_flag_event("flag.catalogue"))
        except Exception:
            pass

        job = catalogue_worker.start_job(
            "+".join(sorted(sources_used)), wanted, language, provider, mode=mode,
            missing_only=bool(data.get("missing_only", True)),
            username=username,
        )
        job["ignored"] = unknown
        return jsonify(job), 202

    @app.route("/api/catalogue/bulk")
    def api_catalogue_bulk_list():
        """Recent bulk jobs, newest last. GET /api/catalogue/bulk."""
        return jsonify({"jobs": catalogue_worker.list_jobs()})

    @app.route("/api/catalogue/bulk/<job_id>")
    def api_catalogue_bulk_job(job_id):
        """One bulk job's progress. GET /api/catalogue/bulk/<job_id>."""
        job = catalogue_worker.get_job(str(job_id or ""))
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(job)

    @app.route("/api/catalogue/bulk/<job_id>/cancel", methods=["POST"])
    def api_catalogue_bulk_cancel(job_id):
        """Stop a running bulk job after the current series.
        POST /api/catalogue/bulk/<job_id>/cancel.

        Queue items it already created stay: they are real work the user asked
        for, and the queue has its own controls for them."""
        if not catalogue_worker.cancel_job(str(job_id or "")):
            return jsonify({"error": "job is not running"}), 400
        return jsonify({"ok": True})
