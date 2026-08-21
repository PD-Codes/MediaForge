"""AutoSync routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ...search import aniwaves_search
from ...search import hanime_search
from ...search import megakino_search
from ...search import nineanime_search
from ...search import query as aniworld_query
from ...search import query_s_to
from ..autosync_worker import _normalize_episode_filter
from ..autosync_worker import _run_autosync_for_job
from ..db import add_autosync_job
from ..db import find_autosync_by_url
from ..db import get_autosync_job
from ..db import get_autosync_jobs
from ..db import get_setting
from ..db import remove_autosync_job
from ..db import update_autosync_job
from ..language_groups import is_group_ref
from ..language_groups import lang_separation_enabled
from ..language_groups import resolve_chain
from ..queue_worker import _aniwaves_enabled
from ..queue_worker import _hanime_enabled
from ..queue_worker import _nineanime_enabled
from ..queue_worker import _is_job_adaptive_paused
from ..runtime_state import _SERIES_LINK_PATTERN
from ..runtime_state import _STO_SERIES_LINK_PATTERN
from ..runtime_state import _syncing_jobs
from ..runtime_state import _syncing_jobs_lock
from flask import jsonify
from flask import render_template
from flask import request
from html import unescape as _html_unescape
import json
import os
import re
import threading
from ..request_context import get_current_user_info as _get_current_user_info
from ...logger import get_logger


logger = get_logger(__name__)


def find_site_candidates(title: str) -> list:
    """Resolve a free-text title to candidate series/movie pages on
    AniWorld / S.TO / MegaKino (+ the opt-in 9anime / Aniwaves / hanime
    sources, each only when enabled), scored against `title` by fuzzy string
    similarity, best match first (top 12).

    Series-only on purpose: Auto-Sync's whole job is "watch for episodes that
    are not there yet", which a single movie page can never produce. That is
    why MegaKino's movie hits are filtered out below and why the movie-only
    sites (FilmPalast, filmo.to) are not queried here at all -- offering them
    would only ever create a job that never has anything to do. They stay
    fully usable as search/download sources.

    Extracted out of api_autosync_site_search's body (see that route,
    still the only in-app caller reachable over HTTP, via
    static/library_video.js's libAddToAutosync()) so other code that needs the
    same "is this actually findable on a site" check can call it directly
    in-process instead of round-tripping through HTTP -- see
    web/thirdparties/mediacalendar/service.py's planned-download worker,
    which polls this once an hour per pending release to auto-create an
    AutoSync job the moment a release becomes available on one of these
    sites.
    """
    import difflib

    def _norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    target = _norm(title)

    candidates = []
    seen = set()

    def _collect(items, site, site_label, base, pattern):
        if isinstance(items, dict):
            items = [items]
        for item in (items or []):
            link = item.get("link") or item.get("url", "")
            if not pattern.match(link):
                continue
            name = _html_unescape(
                item.get("title") or item.get("name", "Unknown")
            ).replace("<em>", "").replace("</em>", "")
            url = base + link
            if url in seen:
                continue
            seen.add(url)
            score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
            candidates.append({
                "site": site, "site_label": site_label,
                "title": name, "url": url, "score": round(score, 3),
            })

    # Same gate the opt-in sources below already used, now applied to the three
    # that were queried unconditionally: a source switched off in Settings ->
    # Sources must not cost a request here either. It read as a bug from the
    # outside -- switching AniWorld off still produced AniWorld candidates, and
    # an AutoSync job created against a disabled source can never run.
    from ..source_policy import source_enabled as _src_on

    if _src_on("aniworld"):
        try:
            _collect(aniworld_query(title), "aniworld", "AniWorld",
                     "https://aniworld.to", _SERIES_LINK_PATTERN)
        except Exception as e:
            logger.debug("[AutosyncSearch] AniWorld search failed: %s", e)
    if _src_on("sto"):
        try:
            _collect(query_s_to(title), "sto", "S.TO",
                     "https://serienstream.to", _STO_SERIES_LINK_PATTERN)
        except Exception as e:
            logger.debug("[AutosyncSearch] S.TO search failed: %s", e)
    if _src_on("megakino"):
        try:
            for item in (megakino_search(title) or []):
                url = item.get("url", "")
                if not item.get("is_series"):  # Auto-Sync tracks series only
                    continue
                if url in seen:
                    continue
                seen.add(url)
                name = _html_unescape(item.get("title") or "Unknown")
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": "megakino", "site_label": "MegaKino",
                    "title": name, "url": url, "score": round(score, 3),
                })
        except Exception as e:
            logger.debug("[AutosyncSearch] MegaKino search failed: %s", e)
    # 9anime / Aniwaves: opt-in, non-adult series sources. Only queried when
    # the user enabled them in Settings -> Sources, same gate the search route
    # and the browse routes use -- a disabled source must not cost a request.
    if _nineanime_enabled():
        try:
            for item in (nineanime_search(title) or []):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                name = _html_unescape(item.get("title") or "Unknown")
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": "nineanime", "site_label": "9anime (EN)",
                    "title": name, "url": url, "score": round(score, 3),
                })
        except Exception as e:
            logger.debug("[AutosyncSearch] 9anime search failed: %s", e)
    if _aniwaves_enabled():
        try:
            for item in (aniwaves_search(title) or []):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                name = _html_unescape(item.get("title") or "Unknown")
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": "aniwaves", "site_label": "Aniwaves (EN)",
                    "title": name, "url": url, "score": round(score, 3),
                })
        except Exception as e:
            logger.debug("[AutosyncSearch] Aniwaves search failed: %s", e)
    if _hanime_enabled():
        try:
            for item in (hanime_search(title) or []):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                name = _html_unescape(item.get("title") or "Unknown")
                score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                candidates.append({
                    "site": "hanime", "site_label": "hanime 18+",
                    "title": name, "url": url, "score": round(score, 3),
                })
        except Exception as e:
            logger.debug("[AutosyncSearch] hanime search failed: %s", e)

    # Module-registered sources (register_search_source). They were missing
    # entirely, so a module could add a content source to the app and its
    # series still could not be put under Auto-Sync -- the one thing that makes
    # a series source useful long-term. thirdparty_search_sources() already
    # drops sources whose module is switched off, and get_search_source()
    # re-checks it, so a disabled module cannot be scraped from here.
    #
    # An adult module source is skipped unless the adult source is on, matching
    # how the built-in hanime branch above is gated: an 18+ hit appearing in a
    # lookup nobody asked for is not a missing feature.
    try:
        from ...search import (
            drop_unresolvable,
            get_search_source,
            thirdparty_search_sources,
        )
        for src in thirdparty_search_sources():
            site_id = src["site_id"]
            if src.get("adult") and not _hanime_enabled():
                continue
            # A module may reuse a settings key it already owns instead of the
            # source_enabled_<id> convention -- pass it through, or the check
            # reads a key nothing ever writes and every such source counts as
            # off (or on) by accident rather than by the user's choice.
            if not _src_on(site_id, key=src.get("enabled_key")):
                continue
            entry = get_search_source(site_id)
            if not entry:
                continue
            try:
                # Same filter api_search() applies: a URL no provider resolves
                # cannot ever be synced, so it must not become a candidate.
                for item in drop_unresolvable(entry["search_fn"](title), site_id):
                    url = item.get("url") or item.get("link") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    name = _html_unescape(item.get("title") or item.get("name") or "Unknown")
                    score = difflib.SequenceMatcher(None, target, _norm(name)).ratio()
                    candidates.append({
                        "site": site_id, "site_label": src.get("label") or site_id,
                        "title": name, "url": url, "score": round(score, 3),
                    })
            except Exception as e:
                logger.debug("[AutosyncSearch] %s search failed: %s", site_id, e)
    except Exception as e:
        logger.debug("[AutosyncSearch] module sources unavailable: %s", e)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:12]


def _language_group_error(language):
    """Reason a language value can't be used, or None if it's fine.

    Only group references can fail here: they need per-language folders to work
    at all (see language_groups.lang_separation_enabled) and the group itself
    has to still exist.
    """
    if not is_group_ref(language):
        return None
    if not lang_separation_enabled():
        return "Sprachgruppen benötigen die Einstellung 'Sprachen in Ordner trennen'."
    if not resolve_chain(language):
        return "Diese Sprachgruppe existiert nicht mehr."
    return None


def _normalize_extra_languages(value, primary):
    """``(json_or_None, error_or_None)`` for a job's extra-language list.

    The extras are the languages whose audio tracks are muxed into the file the
    primary language produces. Stored as a JSON list, ``None`` when there are
    none, so a job that never uses the feature is indistinguishable from one
    written before the column existed.

    The primary itself is dropped rather than rejected: it is already being
    downloaded, and a UI that shows it ticked (it is, that is what makes it the
    primary) would otherwise have to strip it before every save.
    """
    if value in (None, "", []):
        return None, None
    if not isinstance(value, list):
        return None, "extra_languages muss eine Liste sein."

    seen, ordered = set(), []
    for item in value:
        item = str(item or "").strip()
        if not item or item == primary or item in seen:
            continue
        # Same reasoning as the manual download (routes/queue.py): a fallback
        # group means "the first of these that exists" and "All Languages"
        # means one file per language. Neither can be a track in someone
        # else's file.
        if is_group_ref(item) or item == "All Languages":
            return None, (
                "Sprachgruppen und 'Alle Sprachen' können nicht als "
                "zusätzliche Tonspur gewählt werden."
            )
        seen.add(item)
        ordered.append(item)

    if not ordered:
        return None, None
    if is_group_ref(primary) or primary == "All Languages":
        return None, (
            "Zusätzliche Tonspuren brauchen eine einzelne Hauptsprache — "
            "'Alle Sprachen' und Sprachgruppen legen bereits eine Datei je "
            "Sprache an."
        )
    # Deliberately NOT gated on dl_audio_track_merge -- see the same note in
    # routes/queue.py: that setting decides whether two independently queued
    # jobs should be merged on a guess, while this list says so outright.
    return json.dumps(ordered), None


def register_autosync_routes(app):
    """Register all AutoSync job management routes (CRUD, triggering, batch
    operations, import/export) on the given Flask app."""
    @app.route("/autosync")
    def autosync_page():
        """Render the AutoSync jobs page. Route: GET /autosync."""
        return render_template("autosync.html")
    @app.route("/api/autosync")
    def api_autosync_list():
        """List AutoSync jobs (all for admins, own jobs only for regular users).

        Route: GET /api/autosync. Called from static/autosync.js's
        `loadAutosyncJobs()` and `openEditModal()`.
        """
        username, is_admin = _get_current_user_info()
        # Admins see all jobs; regular users see only their own
        jobs = get_autosync_jobs(username=None if is_admin else username)
        for job in jobs:
            job["adaptive_paused"] = _is_job_adaptive_paused(job)
        return jsonify({"jobs": jobs})
    @app.route("/api/autosync", methods=["POST"])
    def api_autosync_create():
        """Create a new AutoSync job for a series URL.

        Route: POST /api/autosync. Called from static/autosync_filter.js's
        `openCreate()` save handler.
        """
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        series_url = (data.get("series_url") or "").strip()
        language = data.get("language", "German Dub")
        provider = data.get("provider", "VOE")
        custom_path_id = data.get("custom_path_id")
        movie_custom_path_id = data.get("movie_custom_path_id")
        cover_url = data.get("cover_url")
        episode_filter = _normalize_episode_filter(data.get("episode_filter"))

        if not title or not series_url:
            return jsonify({"error": "title and series_url are required"}), 400
        _lang_err = _language_group_error(language)
        if _lang_err:
            return jsonify({"error": _lang_err}), 400

        extra_languages, _extra_err = _normalize_extra_languages(
            data.get("extra_languages"), language
        )
        if _extra_err:
            return jsonify({"error": _extra_err}), 400

        existing = find_autosync_by_url(series_url)
        if existing:
            return jsonify(
                {"error": "A sync job for this series already exists", "job": existing}
            ), 409

        username, _ = _get_current_user_info()
        # Resolve path_unavailable_action: request body > global setting > "skip"
        path_action = (
            data.get("path_unavailable_action")
            or get_setting("sync_path_unavailable_action")
            or os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip")
        ).strip().lower()
        if path_action not in ("skip", "hold"):
            path_action = "skip"
        job_id = add_autosync_job(
            title=title,
            series_url=series_url,
            language=language,
            provider=provider,
            custom_path_id=custom_path_id,
            added_by=username,
            path_unavailable_action=path_action,
            episode_filter=episode_filter,
            movie_custom_path_id=movie_custom_path_id,
            cover_url=cover_url,
            extra_languages=extra_languages,
        )
        fresh_job = get_autosync_job(job_id)
        if fresh_job:
            threading.Thread(
                target=_run_autosync_for_job,
                args=(fresh_job,),
                kwargs={"queue_downloads": False},
                daemon=True
            ).start()
        return jsonify({"ok": True, "id": job_id})
    @app.route("/api/autosync/site-search", methods=["POST"])
    def api_autosync_site_search():
        """Resolve a (library) title to candidate series on AniWorld / S.TO so
        it can be added to Auto-Sync. Performs the "is it actually findable on
        a site" check, and returns every match (with its source site) so the
        caller can let the user choose when more than one is found.

        Route: POST /api/autosync/site-search. Called from static/library_video.js's
        `libAddToAutosync()`.
        """
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title is required"}), 400
        return jsonify({"results": find_site_candidates(title)})
    @app.route("/api/autosync/<int:job_id>", methods=["PUT"])
    def api_autosync_update(job_id):
        """Update an AutoSync job's settings (owner or admin only).

        Route: PUT /api/autosync/<job_id>. Called from static/autosync.js's
        `saveEdit()` and from static/autosync_filter.js's `openCreate()` save
        handler (edit path).
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to edit this job"}), 403
        data = request.get_json(silent=True) or {}
        allowed = {"language", "provider", "enabled", "custom_path_id",
                   "path_unavailable_action", "episode_filter", "movie_custom_path_id",
                   "group_name", "extra_languages"}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if "extra_languages" in filtered:
            # Validate against the language this update leaves the job with,
            # not against the one it had: changing both in one request must not
            # be able to leave the primary sitting in its own extras list.
            _primary = filtered.get("language", job.get("language"))
            _lang_err = _language_group_error(_primary)
            if _lang_err:
                return jsonify({"error": _lang_err}), 400
            filtered["extra_languages"], _extra_err = _normalize_extra_languages(
                filtered["extra_languages"], _primary
            )
            if _extra_err:
                return jsonify({"error": _extra_err}), 400
        if "group_name" in filtered:
            gn = filtered["group_name"]
            gn = (str(gn).strip() if gn is not None else "")
            filtered["group_name"] = gn or None
        filter_changed = "episode_filter" in filtered
        if filter_changed:
            filtered["episode_filter"] = _normalize_episode_filter(filtered["episode_filter"])
            # Mark for a silent baseline recompute on the next sync so the
            # "new episodes" badge is not skewed by the changed filter scope.
            filtered["filter_dirty"] = 1
        update_autosync_job(job_id, **filtered)
        # When the filter changed, kick off a background sync immediately so the
        # card counts reflect the new scope right away (and in-scope missing
        # episodes are queued).
        if filter_changed:
            fresh = get_autosync_job(job_id)
            if fresh and fresh.get("enabled"):
                with _syncing_jobs_lock:
                    _busy = job_id in _syncing_jobs
                if not _busy:
                    threading.Thread(
                        target=_run_autosync_for_job, args=(fresh,), daemon=True
                    ).start()
        return jsonify({"ok": True})
    @app.route("/api/autosync/<int:job_id>", methods=["DELETE"])
    def api_autosync_delete(job_id):
        """Delete an AutoSync job (owner or admin only).

        Route: DELETE /api/autosync/<job_id>. Called from static/autosync.js's
        `removeJob()`.
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized to delete this job"}), 403
        ok, err = remove_autosync_job(job_id)
        if not ok:
            return jsonify({"error": err}), 404
        return jsonify({"ok": True})
    @app.route("/api/autosync/<int:job_id>/sync", methods=["POST"])
    def api_autosync_trigger(job_id):
        """Manually trigger a background sync run for a single AutoSync job.

        Route: POST /api/autosync/<job_id>/sync. Called from static/autosync.js's
        `syncNow()`.
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized"}), 403
        with _syncing_jobs_lock:
            if job_id in _syncing_jobs:
                return jsonify({"error": "Sync already running for this job"}), 409
        threading.Thread(target=_run_autosync_for_job, args=(job, True), daemon=True).start()
        return jsonify({"ok": True, "message": "Sync started"})
    @app.route("/api/autosync/<int:job_id>/dry-run", methods=["POST"])
    def api_autosync_dry_run(job_id):
        """Answer "what would this job do if it ran right now?".

        Route: POST /api/autosync/<job_id>/dry-run. Called from
        static/autosync.js's `dryRunJob()`.

        Runs synchronously rather than on a thread, unlike the real trigger
        above: the caller is a person waiting for an answer, and a preview
        whose result has to be polled for is a preview nobody uses. The
        provider fetch is the slow part, so this can take a few seconds.

        Nothing is written. Not the queue, and — the part that is easy to get
        wrong — not the job's own bookkeeping either: a preview that resets
        last_check and clears the "new episodes" badge has changed the thing
        it was asked to describe, and the next real run would find nothing new.
        """
        job = get_autosync_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"error": "Not authorized"}), 403
        with _syncing_jobs_lock:
            if job_id in _syncing_jobs:
                return jsonify({"error": "Sync already running for this job"}), 409

        report = {}
        try:
            _run_autosync_for_job(job, dry_run=True, report=report)
        except Exception as exc:
            logger.warning("[AutoSync] Dry run for job %d failed: %s", job_id, exc)
            report.setdefault("ok", False)
            report["error"] = str(exc)
        return jsonify(report)

    @app.route("/api/autosync/running")
    def api_autosync_running():
        """Return the set of currently running sync job IDs.

        Route: GET /api/autosync/running. Called from static/autosync.js's
        `pollRunningJobs()` and `loadAutosyncJobs()`.
        """
        with _syncing_jobs_lock:
            return jsonify({"running": list(_syncing_jobs)})
    @app.route("/api/autosync/sync-all", methods=["POST"])
    def api_autosync_sync_all():
        """Trigger sync for all enabled jobs the current user owns (or all if admin).

        Route: POST /api/autosync/sync-all. Called from static/autosync.js's
        `syncAll()`.
        """
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs()
        started = 0
        skipped = 0
        for job in jobs:
            if not job.get("enabled"):
                continue
            if not is_admin and job.get("added_by") != username:
                continue
            job_id = job["id"]
            with _syncing_jobs_lock:
                if job_id in _syncing_jobs:
                    skipped += 1
                    continue
            threading.Thread(target=_run_autosync_for_job, args=(job,), daemon=True).start()
            started += 1
        return jsonify({"ok": True, "started": started, "skipped": skipped})
    @app.route("/api/autosync/check", methods=["GET"])
    def api_autosync_check():
        """Check if a sync job exists for a given series URL.

        Route: GET /api/autosync/check. Called from static/app.js to reflect
        AutoSync state on the series detail modal's sync button.
        """
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"exists": False})
        job = find_autosync_by_url(url)
        if not job:
            return jsonify({"exists": False})
        # Only expose job details to the owner or admins
        username, is_admin = _get_current_user_info()
        if not is_admin and job.get("added_by") != username:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "job": job})
    @app.route("/api/autosync/export", methods=["GET"])
    def api_autosync_export():
        """Export all autosync jobs the current user can see as JSON.

        Route: GET /api/autosync/export. Called from static/autosync.js's
        `exportAutosync()`.
        """
        username, is_admin = _get_current_user_info()
        jobs = get_autosync_jobs(username=None if is_admin else username)
        # Strip runtime-only fields that make no sense on import
        export_fields = {"title", "series_url", "language", "provider", "enabled",
                         "episode_filter", "extra_languages"}
        clean = [{k: j[k] for k in export_fields if k in j} for j in jobs]
        payload = json.dumps({"version": 1, "jobs": clean}, ensure_ascii=False, indent=2)
        from flask import Response
        return Response(
            payload,
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="autosync_backup.json"'},
        )
    @app.route("/api/autosync/import", methods=["POST"])
    def api_autosync_import():
        """Import autosync jobs from a JSON backup. Skips duplicates.

        Route: POST /api/autosync/import. Called from static/autosync.js's
        `importAutosync()`.
        """
        username, is_admin = _get_current_user_info()
        if not is_admin:
            return jsonify({"error": "Nur Admins können Jobs importieren"}), 403
        try:
            data = request.get_json(silent=True)
            if data is None:
                # try raw text body
                data = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Ungültiges JSON"}), 400

        jobs_in = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs_in, list):
            return jsonify({"error": "Erwartet: {jobs: [...]}"}), 400

        imported = 0
        skipped  = 0
        errors   = []
        for entry in jobs_in:
            title      = (entry.get("title") or "").strip()
            series_url = (entry.get("series_url") or "").strip()
            language   = entry.get("language", "German Dub")
            provider   = entry.get("provider", "VOE")
            enabled    = int(entry.get("enabled", 1))
            episode_filter = _normalize_episode_filter(entry.get("episode_filter"))
            if not title or not series_url:
                errors.append(f"Übersprungen (kein title/series_url): {entry}")
                continue
            if find_autosync_by_url(series_url):
                skipped += 1
                continue
            _lang_err = _language_group_error(language)
            if _lang_err:
                # Exported from an instance where this group works; here it
                # doesn't (missing, or language separation is off). Importing it
                # would create a job that errors on every run.
                errors.append(f"{title}: {_lang_err} — übersprungen")
                continue
            # The export carries extra_languages as the stored JSON string;
            # re-validate it here rather than trusting it, because the merge
            # setting and the available languages belong to THIS instance.
            # A rejected extras list drops the extras, not the whole job --
            # a single-language sync is still what the user wanted most of.
            _extra_raw = entry.get("extra_languages")
            if isinstance(_extra_raw, str):
                try:
                    _extra_raw = json.loads(_extra_raw)
                except (TypeError, ValueError):
                    _extra_raw = None
            extra_languages, _extra_err = _normalize_extra_languages(_extra_raw, language)
            if _extra_err:
                errors.append(f"{title}: {_extra_err} — ohne Zusatzsprachen importiert")
            try:
                job_id = add_autosync_job(
                    title=title,
                    series_url=series_url,
                    language=language,
                    provider=provider,
                    added_by=username,
                    episode_filter=episode_filter,
                    extra_languages=extra_languages,
                )
                if not enabled:
                    update_autosync_job(job_id, enabled=0)
                imported += 1
            except Exception as exc:
                errors.append(f"{title}: {exc}")
        return jsonify({"ok": True, "imported": imported, "skipped": skipped, "errors": errors})
    @app.route("/api/autosync/batch", methods=["POST"])
    def api_autosync_batch():
        """Batch-update multiple autosync jobs at once.

        Route: POST /api/autosync/batch. Called from static/autosync.js's
        `batchAction()`.

        Body: { ids: [int, ...], action: "enable"|"disable"|"set_path", custom_path_id: int|null }
        """
        username, is_admin = _get_current_user_info()
        data   = request.get_json(silent=True) or {}
        ids    = data.get("ids", [])
        action = data.get("action", "")
        if not ids or action not in ("enable", "disable", "set_path", "delete",
                                     "set_group", "remove_group"):
            return jsonify({"error": "ids und action (enable|disable|set_path|delete|set_group|remove_group) erforderlich"}), 400

        updated = 0
        for job_id in ids:
            job = get_autosync_job(job_id)
            if not job:
                continue
            if not is_admin and job.get("added_by") != username:
                continue
            if action == "enable":
                update_autosync_job(job_id, enabled=1)
            elif action == "disable":
                update_autosync_job(job_id, enabled=0)
            elif action == "set_path":
                update_autosync_job(job_id, custom_path_id=data.get("custom_path_id"))
            elif action == "delete":
                remove_autosync_job(job_id)
            elif action == "set_group":
                gn = data.get("group_name")
                gn = (str(gn).strip() if gn is not None else "")
                update_autosync_job(job_id, group_name=gn or None)
            elif action == "remove_group":
                update_autosync_job(job_id, group_name=None)
            updated += 1
        return jsonify({"ok": True, "updated": updated})
