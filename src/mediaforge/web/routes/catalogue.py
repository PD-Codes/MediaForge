"""Catalogue page and its API.

The full A-Z list of a source site, for bulk selection. See
``mediaforge/catalogue.py`` for where the lists come from and why they hold
titles only, and ``web/catalogue_worker.py`` for what happens after the user
confirms a selection.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from flask import jsonify, render_template, request

from ...catalogue import all_catalogues
from ...logger import get_logger
from .. import catalogue_store, catalogue_worker
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

# Not a selection limit -- see the note above. This bounds the REQUEST: the
# whole catalogue is around 13k urls, so anything past this cannot be a real
# selection, and validating an arbitrarily long list is a denial of service
# that costs the sender one JSON body.
MAX_SUBMITTED_URLS = 60_000


def _catalogue_sources():
    """Every catalogue that exists, with the enabled state the rest of the app
    uses. A source switched off in Settings is listed but marked, rather than
    hidden: it is the same list everywhere else in the app, and silently
    dropping an entry is how a user ends up thinking a source disappeared."""
    from ..db import catalogue_meta
    stored = catalogue_meta()
    out = []
    for sid, meta in all_catalogues().items():
        out.append({
            "id": sid,
            "label": meta["label"],
            "kind": meta.get("kind", "series"),
            # The merged list marks every row with its source's colour, so a
            # third-party catalogue has to be able to supply one -- same as
            # register_home_feed_source(). Already validated as a literal hex
            # by catalogue._safe_color(); "" means "use the page's fallback".
            "color": meta.get("color") or "",
            "enabled": bool(source_enabled(sid)),
            # "cached" now means "there are rows in the DB", which survives a
            # restart -- the old in-memory answer was False after every one.
            "cached": bool(stored.get(sid, {}).get("count")),
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
        entries, meta = catalogue_store.get_entries(source_id, force=force)
        if entries is None:
            return jsonify({"error": "catalogue unavailable", "source": source_id}), 503
        # A stale list is a normal answer, not an error: it is served straight
        # from the DB while the refresh runs behind it, and the page says how
        # old it is rather than showing a spinner over perfectly good data.
        return jsonify({
            "source": source_id,
            "entries": entries,
            "count": len(entries),
            "fetched_at": meta.get("fetched_at") or 0,
            "stale": bool(meta.get("status") == "failed"),
            "refreshing": source_id in catalogue_store.status()["refreshing"],
        })

    @app.route("/api/catalogue/status")
    def api_catalogue_status():
        """Freshness and background progress. GET /api/catalogue/status.

        Polled by the page while anything is running. Deliberately separate
        from /api/catalogue/state (which is about the QUEUE): this one is
        about the catalogue's own housekeeping -- which list is being
        refetched, how old each one is, and how far the id resolution has
        got. A background job the user cannot see is a background job the
        user assumes is broken.
        """
        return jsonify(catalogue_store.status())

    @app.route("/api/catalogue/refresh", methods=["POST"])
    def api_catalogue_refresh():
        """Ask for a refetch of one source or all of them.
        POST /api/catalogue/refresh {"source": "aniworld"}.

        Answers 202 and returns immediately -- the work happens in the
        background and the page follows it through /api/catalogue/status.
        """
        data = request.get_json(silent=True) or {}
        source_id = str(data.get("source") or "").strip().lower()
        if source_id:
            if source_id not in all_catalogues():
                return jsonify({"error": "unknown catalogue"}), 404
            started = 1 if catalogue_store.start_refresh(source_id) else 0
        else:
            started = catalogue_store.refresh_stale(force=True)

        # Refreshing the LISTS is only half of what a user means by "update".
        # The ids are what merges the two sites' rows and what decides
        # "already in my library", and the backfill otherwise sits in an idle
        # wait for up to fifteen minutes. A store that has just saved new
        # entries wakes it too (see catalogue_store._do_refresh); this covers
        # the case where nothing needed refetching but ids are still pending.
        try:
            from .. import catalogue_ids
            catalogue_ids.start()      # no-op when running, and wakes it either way
        except Exception as exc:
            logger.debug("[Catalogue] could not wake the id worker: %s", exc)

        return jsonify({"started": started, "status": catalogue_store.status()}), 202

    @app.route("/api/catalogue/resolve", methods=["POST"])
    def api_catalogue_resolve():
        """Resolve ONE entry's TMDB/IMDb id now. POST /api/catalogue/resolve.

        The lazy half of the id resolution: the backfill crawls the whole
        catalogue over hours, but a title the user just opened is one they
        care about right now, so it jumps the queue. One TMDB lookup, cached
        for 24h and rate-limited process-wide like every other one.

        Already-resolved entries answer from the database without touching
        TMDB at all.
        """
        data = request.get_json(silent=True) or {}
        url = str(data.get("url") or "").strip().rstrip("/")
        if not url:
            return jsonify({"error": "url is required"}), 400

        # Must be a url we actually hold -- otherwise this is a "look up
        # anything on TMDB for me" endpoint with the app's API key attached.
        #
        # ONE indexed row read. This used to load every source's complete list
        # into Python and compare ~13,000 strings to find one entry, on every
        # single details open -- which is most of what made opening a title
        # feel slow.
        from ..db import find_catalogue_entry
        entry = find_catalogue_entry(url)
        if not entry:
            return jsonify({"error": "unknown url"}), 404
        if entry.get("tmdb_id") or entry.get("imdb_id"):
            return jsonify({"tmdb_id": entry.get("tmdb_id") or "",
                            "imdb_id": entry.get("imdb_id") or "",
                            "cached": True})
        from .. import catalogue_ids
        result = catalogue_ids.resolve_entry(
            entry["source_id"], entry["url"], entry["title"])
        return jsonify({"tmdb_id": result.get("tmdb_id", ""),
                        "imdb_id": result.get("imdb_id", ""),
                        "cached": False})

    @app.route("/api/catalogue/state")
    def api_catalogue_state():
        """Which series are already spoken for. GET /api/catalogue/state.

        Two flat url lists, resolved in ONE request rather than per row: the
        page renders up to eleven thousand of them and cannot ask per title.
        "Already in the library" is deliberately NOT here -- the client already
        holds the downloaded-folder list for its own badges and matches on it
        locally (see isDownloaded() in app.js).
        """
        from ..request_context import get_current_user_info
        username, is_admin = get_current_user_info()

        queued, syncing = set(), set()
        try:
            for item in (get_queue() or []):
                url = (item.get("series_url") or "").rstrip("/")
                # The download_queue's own vocabulary (see db/queue.py's CHECK
                # constraint): queued / running / completed / partial / failed
                # / cancelled. "pending" and "paused" are not statuses at all
                # -- a pause is global, not per item -- so this matched nothing
                # and the "Queued" badge never appeared for a series that had
                # just been added, which reads exactly like "nothing happened".
                if url and item.get("status") in ("queued", "running"):
                    queued.add(url)
        except Exception as exc:
            logger.debug("[Catalogue] queue state failed: %s", exc)
        try:
            # Scoped exactly like GET /api/autosync itself (routes/autosync.py):
            # admins see everything, everybody else sees their own. Asking
            # unscoped here handed any logged-in account the full list of
            # series every other account is syncing -- through a badge.
            for job in (get_autosync_jobs(username=None if is_admin else username) or []):
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

        from ..request_context import get_current_user_info
        username, is_admin = get_current_user_info()

        # Creating AutoSync jobs is admin-only everywhere else in the app:
        # POST /api/autosync sits in app.py's _admin_only set. This endpoint
        # reaches add_autosync_job() directly, so without this check it was a
        # way for any logged-in account to create unlimited recurring jobs --
        # the exact action that gate exists to prevent, and in bulk. The queue
        # mode is deliberately NOT gated: a one-off download is what every
        # account may do (see /api/download).
        if mode == "autosync" and not is_admin:
            return jsonify({"error": "not permitted", "code": "admin_only"}), 403

        urls = data.get("urls") or []
        if not isinstance(urls, list) or not urls:
            return jsonify({"error": "urls list is required"}), 400
        # No ceiling on the SELECTION (see the note at the top of this file),
        # but a hard one on the request BODY: past the total number of stored
        # entries every further element is guaranteed junk, and validating a
        # million of them is a denial of service with a JSON body.
        if len(urls) > MAX_SUBMITTED_URLS:
            return jsonify({"error": "too many urls in one request",
                            "code": "too_many"}), 413

        # Only urls that are actually in ONE OF the catalogues. The page shows
        # every source merged into a single list, so a selection legitimately
        # spans several of them and there is no "the" source to check against.
        # What has not changed is the reason for checking at all: a bulk
        # endpoint that scrapes whatever url it is handed is a request forgery
        # tool with a download queue attached.
        # Reads the stored lists only. A POST must not be able to trigger two
        # multi-megabyte downloads just to answer "is this url in a catalogue" --
        # and after the move to the DB there is always something stored unless
        # the app has genuinely never fetched anything.
        # One indexed query per 400 urls, instead of loading every source's
        # full list into Python and building a 13k-entry dict per request.
        from ..db import catalogue_entry_count, catalogue_sources_for_urls
        known = catalogue_sources_for_urls(urls)
        if not known and not catalogue_entry_count():
            return jsonify({"error": "catalogue unavailable"}), 503

        wanted, unknown, sources_used = [], 0, set()
        # A set beside the list: `url not in wanted` was a linear scan of a
        # list the docstring above invites to hold thirteen thousand entries,
        # which is ~85 million comparisons for one press of "select all".
        seen = set()
        for raw in urls:
            url = str(raw or "").strip().rstrip("/")
            if url in known and url not in seen:
                seen.add(url)
                wanted.append(url)
                sources_used.add(known[url])
            elif url and url not in seen:
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
        """Recent bulk jobs, newest last. GET /api/catalogue/bulk.

        Own jobs only; admins see every account's. The page resumes its
        progress card from this, and a card reporting somebody else's job is
        both a leak and a lie about what the user did."""
        from ..request_context import get_current_user_info
        username, is_admin = get_current_user_info()
        return jsonify({"jobs": catalogue_worker.list_jobs(username, is_admin)})

    @app.route("/api/catalogue/bulk/<job_id>")
    def api_catalogue_bulk_job(job_id):
        """One bulk job's progress. GET /api/catalogue/bulk/<job_id>."""
        from ..request_context import get_current_user_info
        username, is_admin = get_current_user_info()
        job = catalogue_worker.get_job(str(job_id or ""), username, is_admin)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(job)

    @app.route("/api/catalogue/bulk/<job_id>/cancel", methods=["POST"])
    def api_catalogue_bulk_cancel(job_id):
        """Stop a running bulk job after the current series.
        POST /api/catalogue/bulk/<job_id>/cancel.

        Queue items it already created stay: they are real work the user asked
        for, and the queue has its own controls for them.

        Somebody else's job is "not running" as far as this account is
        concerned -- a job id was all it took to stop another user's work."""
        from ..request_context import get_current_user_info
        username, is_admin = get_current_user_info()
        if not catalogue_worker.cancel_job(str(job_id or ""), username, is_admin):
            return jsonify({"error": "job is not running"}), 400
        return jsonify({"ok": True})
