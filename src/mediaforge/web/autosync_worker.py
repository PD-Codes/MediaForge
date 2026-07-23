"""Auto-sync worker — periodic new/missing-episode detection and queueing."""

import re
import time

from ..config import INVERSE_LANG_LABELS, LANG_KEY_MAP
from ..logger import get_logger
from ..providers import resolve_provider
from .db import (
    add_to_queue,
    get_autosync_jobs,
    get_custom_path_by_id,
    get_custom_paths,
    get_setting,
    is_series_queued_or_running,
    prune_download_history,
    update_autosync_job,
)
from .language_groups import (
    is_group_ref,
    labels_from_provider_data,
    pick_language,
    resolve_chain,
)
from .queue_worker import _dl_lock
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from .runtime_state import (
    LAYOUT_BACKOFF_MINUTES,
    SYNC_ADAPTIVE_PAUSE_MAP,
    SYNC_ADAPTIVE_UNIT_MAP,
    SYNC_RETRY_MAP,
    SYNC_SCHEDULE_MAP,
    _syncing_jobs,
    _syncing_jobs_lock,
    is_layout_backoff_active,
    layout_backoff_remaining,
    trigger_layout_backoff,
)

logger = get_logger(__name__)

# Auto-sync worker state
_autosync_worker_started = False

_last_history_prune: "float" = 0.0  # throttle: download-history retention prune (~hourly)


def _normalize_episode_filter(value):
    """Normalise an episode_filter payload to a JSON string or None.

    Accepts a dict (from the API), a JSON string, or None. An empty/invalid
    value or an "all"-mode filter with no exclusions and no movies collapses to
    None, which means "no filter" (legacy behaviour).

    Used by: routes/autosync.py when creating/updating autosync jobs via the API.
    """
    import json
    if value is None:
        return None
    from .autosync_filter import parse_filter
    flt = parse_filter(value)
    if flt is None:
        return None
    # Collapse a no-op filter to NULL so legacy behaviour is preserved exactly.
    if (flt.get("mode") == "all" and not flt.get("seasons")
            and not flt.get("include_movies")):
        return None
    return json.dumps(flt, ensure_ascii=False)


def _parse_sync_days(raw, default="0,1,2,3,4,5,6"):
    """Parse a CSV of weekday indices (0=Mon..6=Sun) into a sorted set of ints."""
    if raw is None or str(raw).strip() == "":
        raw = default
    out = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part == "":
            continue
        try:
            v = int(part)
        except ValueError:
            continue
        if 0 <= v <= 6:
            out.add(v)
    return out


def _parse_sync_times(raw, default="06:00"):
    """Parse a CSV of HH:MM into a sorted list of (hour, minute) tuples."""
    if raw is None or str(raw).strip() == "":
        raw = default
    seen = set()
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part == "":
            continue
        if ":" not in part:
            continue
        hh, _, mm = part.partition(":")
        try:
            h, m = int(hh), int(mm)
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59 and (h, m) not in seen:
            seen.add((h, m))
            out.append((h, m))
    out.sort()
    return out


def _normalize_sync_times(raw):
    """Return a normalized CSV "HH:MM,HH:MM" string, or "" if none valid."""
    return ",".join(f"{h:02d}:{m:02d}" for (h, m) in _parse_sync_times(raw, default=""))


def _norm_title(s):
    """Lowercase, strip punctuation/diacritics-ish to bare alnum tokens."""
    import re, unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_is_confident(source, candidates, threshold=0.86):
    """Whether *source* clearly refers to the same title as one of *candidates*.

    Used to decide if a TMDB-localized title may safely replace the original
    site title. Errs toward False (keep the original) when unsure, so a wrong
    TMDB match (e.g. a spin-off) never overrides the real title.

    Used by: tmdb_cache.py when deciding whether to substitute a localized
    TMDB title for the original site title.
    """
    import difflib
    src = _norm_title(source)
    if not src:
        return False
    best = 0.0
    for c in candidates:
        cn = _norm_title(c)
        if not cn:
            continue
        if cn == src:
            return True
        r = difflib.SequenceMatcher(None, src, cn).ratio()
        if r > best:
            best = r
    return best >= threshold


def _job_notif_lang(job):
    """Best-effort UI language ('en' or 'de') for the user who created *job*.

    Used to localize the handful of user-facing strings this background
    worker builds itself (custom-path hold/resume, unsupported-provider, the
    generic failure wrapper). Flask's request-bound session -- and therefore
    app.py's get_locale() -- isn't available from a daemon thread, but the
    per-user language preference is persisted in the DB and can be read
    directly from here. Falls back to "en" (the same default used everywhere
    else -- see get_locale() / db.get_user_language()) if the job has no
    creator, the lookup fails, or the users table doesn't exist (no-auth
    mode, see db.get_user_id_by_username()'s docstring).
    """
    try:
        from .db import get_user_id_by_username, get_user_language
        uid = get_user_id_by_username(job.get("added_by"))
        if uid is None:
            return "en"
        return get_user_language(uid)
    except Exception:
        return "en"


def _tr(lang, de, en):
    """Tiny DE/EN string picker -- the backend equivalent of the frontend's
    t(de, en) helper (static/app.js) -- for the few notification/error
    strings this file builds itself. Deliberately NOT used for raw scraper
    exception text (str(e)): that originates deep in the site models and
    isn't ours to translate here."""
    return de if lang == "de" else en


def _run_autosync_for_job(job, force_notify=False, queue_downloads: bool = True):
    """Check a single autosync job for new/missing episodes and queue them.

    Guarded by `_syncing_jobs`/`_syncing_jobs_lock` so the same job never runs
    concurrently (a slow provider fetch could otherwise overlap with the next
    10s poll tick). Roughly:

      1. If the job has a custom_path_id, verify the target directory is
         reachable; otherwise skip/hold the job depending on configuration.
      2. Phase 1 — count episodes available online (language-independent) to
         cheaply detect whether anything new has appeared since last check.
      3. Phase 2 — for each configured language, scan disk for what's already
         downloaded and queue whatever online episodes are missing (checking
         per-episode language availability first to avoid queuing episodes
         that would fail in the queue worker).
      4. Persist last_check/episodes_found/etc. and notify on newly queued
         episodes.

    On failure, transient network errors are logged without incrementing the
    job's retry_count (they're expected to resolve on their own); other
    errors increment retry_count, which `_autosync_worker` uses to schedule a
    short-interval retry.

    Used by: `_autosync_worker`'s poll loop, once per enabled job whose
    schedule/retry window is due.
    """
    import os
    from datetime import datetime
    from pathlib import Path
    from .autosync_filter import parse_filter, episode_included, movie_included

    job_id = job["id"]

    # A layout-change backoff is currently open (see the exception handler
    # below and runtime_state.trigger_layout_backoff()) — skip this job
    # without even attempting a fetch. This is what keeps a burst of due
    # jobs from each hitting the same broken parse and each firing their own
    # notification: only the job that first detects the problem does that;
    # everyone else due during the window just waits it out.
    if is_layout_backoff_active():
        logger.info(
            "Auto-sync skipped '%s' — AniWorld layout-change backoff active for another %.0fs",
            job.get("title", "?"), layout_backoff_remaining(),
        )
        return

    with _syncing_jobs_lock:
        if job_id in _syncing_jobs:
            logger.info("Auto-sync skipped job %d — already running", job_id)
            return
        _syncing_jobs.add(job_id)

    try:
        # Telemetry: stage-2 usage counter — fires once per actual sync
        # attempt (not for skipped/held jobs above), no title/URL involved.
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.autosync"))

        # ------------------------------------------------------------------ #
        # Custom Path availability check                                       #
        # If the job has a custom_path_id, verify the directory is accessible #
        # before doing any online fetching. Depending on the job's             #
        # path_unavailable_action setting we either skip or hold.              #
        # ------------------------------------------------------------------ #
        from pathlib import Path as _Path
        _cp_ids = [cid for cid in (job.get("custom_path_id"), job.get("movie_custom_path_id")) if cid]
        _cp_record = None
        _cp_available = True
        for _cp_id in _cp_ids:
            _rec = get_custom_path_by_id(_cp_id)
            if not _rec:
                _cp_available = False
                _cp_record = {"path": f"ID #{_cp_id} (gelöscht/nicht gefunden)"}
                break
            try:
                if not _Path(_rec["path"]).expanduser().is_dir():
                    _cp_available = False
                    _cp_record = _rec
                    break
            except Exception:
                _cp_available = False
                _cp_record = _rec
                break

        if _cp_ids and not _cp_available:
            _global_action = os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip").lower()
            _action = (job.get("path_unavailable_action") or _global_action or "skip").lower()
            _was_on_hold = bool(job.get("on_hold"))

            if _action == "hold":
                if not _was_on_hold:
                    # First time going on hold — persist state + notify
                    _lang = _job_notif_lang(job)
                    update_autosync_job(
                        job["id"],
                        on_hold=1,
                        last_error=_tr(_lang,
                            "Custom Path nicht erreichbar — Sync pausiert (Hold)",
                            "Custom path unreachable — sync paused (hold)"),
                    )
                    logger.warning(
                        "Auto-sync HOLD for '%s' — custom path '%s' not accessible",
                        job.get("title", "?"),
                        _cp_record["path"] if _cp_record else _cp_ids,
                    )
                    try:
                        from .notifications import notify_all
                        notify_all(
                            title=job.get("title", "Auto-Sync"),
                            body=_tr(_lang,
                                "⚠️ Sync pausiert: Custom Path nicht erreichbar — ",
                                "⚠️ Sync paused: custom path unreachable — ")
                                + str(_cp_record['path'] if _cp_record else _tr(_lang, 'Unbekannt', 'Unknown')),
                            event="on_sync_hold",
                            username=job.get("added_by"),
                        )
                    except Exception as e:
                        logger.warning("[AutoSync] Hold notification failed: %s", e)
                else:
                    logger.info(
                        "Auto-sync still on HOLD for '%s' — custom path still unavailable",
                        job.get("title", "?"),
                    )
                return  # wait for next cycle
            else:
                # action == "skip" (default)
                logger.info(
                    "Auto-sync SKIP for '%s' — custom path not accessible (action=skip)",
                    job.get("title", "?"),
                )
                update_autosync_job(
                    job["id"],
                    last_check=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    last_error=_tr(_job_notif_lang(job),
                        "Custom Path nicht erreichbar — Sync übersprungen",
                        "Custom path unreachable — sync skipped"),
                )
                return
        elif job.get("on_hold"):
            # Path is accessible — if we were on hold, clear it and notify resume
            update_autosync_job(job["id"], on_hold=0, last_error=None)
            logger.info(
                "Auto-sync RESUME for '%s' — custom path is accessible again",
                job.get("title", "?"),
            )
            try:
                from .notifications import notify_all
                _lang = _job_notif_lang(job)
                notify_all(
                    title=job.get("title", "Auto-Sync"),
                    body=_tr(_lang,
                        "▶️ Sync wird fortgesetzt: Custom Path ist wieder erreichbar — ",
                        "▶️ Sync resuming: custom path is reachable again — ")
                        + str(_cp_record['path'] if _cp_record else ''),
                    event="on_sync_resume",
                    username=job.get("added_by"),
                )
            except Exception as e:
                logger.warning("[AutoSync] Resume notification failed: %s", e)

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"

        # Language fallback group (see web/language_groups.py): an ordered chain
        # instead of one language, resolved here and never passed on — every
        # queue entry this run creates carries a concrete language label.
        # Checked before the series page is fetched: a job that cannot run must
        # say why instead of failing later with whatever the network said.
        lang_chain = []
        if is_group_ref(job.get("language")):
            if not lang_sep:
                # Turned off after the job was created. Without per-language
                # folders a group cannot tell which language an existing file is
                # in, so it could neither skip nor upgrade correctly.
                raise RuntimeError(
                    "language groups require the 'Separate languages into folders' "
                    "setting — enable it again or pick a single language for this job"
                )
            lang_chain = resolve_chain(job["language"])
            if not lang_chain:
                # The group was deleted (or emptied) behind this job's back.
                # Failing loudly beats syncing nothing and reporting success.
                raise RuntimeError(
                    f"language group '{job['language']}' no longer exists — "
                    "pick a language or another group for this job"
                )

        prov = resolve_provider(job["series_url"])
        if prov.series_cls is None or prov.season_cls is None:
            # Some providers have no series/season concept for Auto-Sync to
            # drive at all -- e.g. a movie-only site, or MegaKino specifically,
            # where a series URL and a movie URL share the exact same shape
            # (MEGAKINO_SERIES_PATTERN == MEGAKINO_MOVIE_PATTERN in config.py)
            # and can only be told apart by hitting the JSON API, which
            # resolve_provider() never does -- see providers.py's PROVIDERS
            # comment. This is a permanent mismatch, not a transient failure:
            # retrying will never succeed, and leaving it to reach
            # `prov.series_cls(...)` below throws an uncaught
            # `TypeError: 'NoneType' object is not callable` every single
            # cycle forever. Disable the job once, with a clear reason, and
            # notify a single time instead.
            _lang = _job_notif_lang(job)
            _msg = _tr(_lang,
                f"Auto-Sync wird für „{prov.name}“ nicht unterstützt "
                "(diese Seite hat kein Serien-/Staffel-Konzept, das sich "
                "aus der URL erkennen lässt).",
                f"Auto-Sync is not supported for \"{prov.name}\" "
                "(this site has no series/season concept that can be "
                "detected from the URL).",
            )
            logger.warning(
                "Auto-sync job '%s' targets provider '%s' which has no "
                "series_cls/season_cls -- disabling the job instead of "
                "retrying forever.",
                job.get("title", "?"), prov.name,
            )
            update_autosync_job(
                job["id"],
                enabled=0,
                last_error=_msg,
                last_check=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            )
            telemetry_client.submit(telemetry_events.build_feature_detail_event(
                "detail.autosync", action="run", status="unsupported_provider",
                metadata={"provider": prov.name},
            ))
            try:
                from .notifications import notify_all
                notify_all(
                    title=job.get("title", "Auto-Sync"),
                    body="⚠️ " + _msg + " " + _tr(_lang,
                        "Auto-Sync wurde für diesen Job deaktiviert.",
                        "Auto-Sync has been disabled for this job."),
                    event="on_sync_error",
                    username=job.get("added_by"),
                )
            except Exception as notif_exc:
                logger.warning("[AutoSync] Unsupported-provider notification failed: %s", notif_exc)
            return
        series = prov.series_cls(url=job["series_url"])

        if not job.get("cover_url"):
            _poster = getattr(series, "poster_url", None)
            if callable(_poster):
                try:
                    _poster = _poster()
                except Exception:
                    _poster = None
            if _poster:
                update_autosync_job(job["id"], cover_url=_poster)
                job["cover_url"] = _poster


        # Only use lang_sep for "All Languages" when the global setting is enabled;
        # otherwise scan root directory to avoid phantom missing-episode detection.
        if job.get("language") == "All Languages" and not lang_sep:
            logger.warning(
                "Auto-sync job '%s' uses 'All Languages' but lang_separation is off — scanning root.",
                job.get("title", "?"),
            )

        from .lang_folders import LANG_FOLDER_MAP
        from .lang_folders import SYNC_ALL_LANGUAGES

        lang_folder_map = LANG_FOLDER_MAP

        target_languages = []
        if job.get("language") == "All Languages":
            disable_eng_sub = os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
            for lang in SYNC_ALL_LANGUAGES:
                if disable_eng_sub and lang == "English Sub":
                    continue
                target_languages.append(lang)
        elif lang_chain:
            # Handled as one chain further down, not language by language.
            target_languages = []
        else:
            target_languages.append(job["language"])

        # Phase 1: Count all episodes available online (language-independent).
        # This is done before any disk scan so we can decide early whether
        # new episodes have appeared since the last check.
        # We keep the episode object so Phase 2 can lazily check language
        # availability via provider_data before actually queuing.
        previous_episodes_found = job.get("episodes_found", 0)
        _flt = parse_filter(job.get("episode_filter"))
        _movies_on = movie_included(_flt)
        _filter_dirty = bool(job.get("filter_dirty"))
        # Resolve the path for movies/specials: dedicated path falls back to the
        # series path when unset.
        _movie_path_id = job.get("movie_custom_path_id") or job.get("custom_path_id")
        # list of (season_num, ep_num, url, ep_obj, is_movie)
        online_episodes = []
        for season in series.seasons:
            season_obj = prov.season_cls(url=season.url, series=series)
            if getattr(season_obj, "are_movies", False):
                # Movies / specials collection (aniworld "/filme"). Controlled
                # solely by the filter's include_movies flag — no per-episode
                # filtering. Legacy (no filter) keeps skipping movies.
                if not _movies_on:
                    logger.debug(
                        "Auto-sync: skipping movie season for '%s'",
                        job.get("title", "?"),
                    )
                    continue
                for ep in season_obj.episodes:
                    online_episodes.append(
                        (ep.season.season_number, ep.episode_number, ep.url, ep, True)
                    )
                continue
            for ep in season_obj.episodes:
                _sn = ep.season.season_number
                _en = ep.episode_number
                if not episode_included(_flt, _sn, _en):
                    continue
                online_episodes.append((_sn, _en, ep.url, ep, False))

        total_online_count = len(online_episodes)
        # (season, episode) pairs that are in scope of the filter — used to keep
        # the local/downloaded count consistent with the configured episodes.
        scope_pairs = {(s, e) for (s, e, _u, _o, _m) in online_episodes}
        is_first_run = previous_episodes_found == 0
        has_new_episodes_online = total_online_count > previous_episodes_found

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        if not is_first_run and not has_new_episodes_online:
            logger.info(
                "Auto-sync: no new episodes online for '%s' (%d found, unchanged) — will still check local files",
                job["title"],
                total_online_count,
            )

        if has_new_episodes_online:
            new_count = total_online_count - previous_episodes_found
            logger.info(
                "Auto-sync: %d new episode(s) detected for '%s' (was %d, now %d)",
                new_count,
                job["title"],
                previous_episodes_found,
                total_online_count,
            )
            pass  # notification fires in Phase 2 when episodes are actually queued

        # Phase 2: Per language — scan disk and queue missing episodes.
        total_new_queued = 0
        max_local_found = 0

        # Pre-fetch provider_data for all online episodes once, shared across all
        # languages — avoids one HTTP request per episode per language (N+1).
        _pd_cache: dict = {}  # url -> pd_data dict (or None on error)

        def _fetch_pd(ep_url, ep_obj):
            if ep_url in _pd_cache:
                return
            try:
                pd = ep_obj.provider_data
                _pd_cache[ep_url] = pd._data if hasattr(pd, "_data") else pd
            except Exception as exc:
                logger.debug("Auto-sync: provider_data prefetch failed for %s: %s", ep_url, exc)
                _pd_cache[ep_url] = None

        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=5) as _pool:
            for (_s, _e, _url, _ep, _is_movie) in online_episodes:
                _pool.submit(_fetch_pd, _url, _ep)

        # Compute scan_roots once — same for all languages
        raw = os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        if raw:
            dl_base = Path(raw).expanduser()
            if not dl_base.is_absolute():
                dl_base = Path.home() / dl_base
        else:
            dl_base = Path.home() / "Downloads"

        scan_roots = [dl_base]
        for cp in get_custom_paths():
            cp_path = Path(cp["path"]).expanduser()
            if not cp_path.is_absolute():
                cp_path = Path.home() / cp_path
            scan_roots.append(cp_path)

        # If the series page didn't parse, stop here — do not sync "nothing".
        #
        # A title of None means the markup had no series-title block: the site served a
        # captcha, a block page, an error page, or changed its layout. The episode list
        # scraped from that same page is then empty too — and an empty episode list is
        # indistinguishable, further down, from "this series genuinely has no new
        # episodes". The run would end in the success path, clear last_error, reset the
        # new-episode counter and record a clean last_check. A blocked fetch would look
        # exactly like being up to date, which is the worst thing a sync can do quietly.
        #
        # So: say what happened, in a sentence that names the likely cause, and let the
        # normal error handling retry it next cycle.
        series_title = getattr(series, "title", None)
        if not series_title:
            # The provider already worked out *why* while it still had the evidence (status
            # code, body, the page itself) — see AniworldSeries.page_problem. Repeating a
            # vaguer version of that here would mean the user reads "could not read the
            # series page" in the job card while the log knows it was an HTTP 503.
            problem = (getattr(series, "page_problem", None)
                       or "could not read the series page (no title in the response)")
            raise RuntimeError(f"{problem} — sync skipped, not treated as 'no new episodes'")

        title_clean = (getattr(series, "title_cleaned", "") or series_title).lower()
        ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)

        # mtime-based scan cache: {base_path_str -> (mtime, {(s, e): [paths]})}
        # Avoids re-scanning the same folder multiple times within one sync run.
        _scan_cache: dict = {}

        def _scan_base(base: Path) -> dict:
            """Map (season, episode) -> file paths found under base (mtime-cached).

            The paths are what a language upgrade needs: to replace an episode
            with a better-language copy, the old file has to be nameable, not
            just countable.
            """
            key = str(base)
            try:
                mtime = base.stat().st_mtime if base.is_dir() else 0
            except OSError:
                mtime = 0
            cached = _scan_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
            eps: dict = {}
            if base.is_dir() and title_clean:
                for folder in base.iterdir():
                    if not folder.is_dir() or not folder.name.lower().startswith(title_clean):
                        continue
                    for f in folder.rglob("*"):
                        if f.is_file():
                            m = ep_re.search(f.name)
                            if m:
                                eps.setdefault(
                                    (int(m.group(1)), int(m.group(2))), []
                                ).append(f)
            _scan_cache[key] = (mtime, eps)
            return eps

        def _scan_languages(languages) -> dict:
            """(season, episode) -> paths already on disk for any of `languages`."""
            found: dict = {}
            for lang in languages:
                folder = lang_folder_map.get(lang, lang.lower().replace(" ", "-"))
                for root in scan_roots:
                    for pair, paths in _scan_base((root / folder) if lang_sep else root).items():
                        found.setdefault(pair, []).extend(paths)
            return found

        # What this run will queue: (language, series_urls, movie_urls,
        # {episode_url: [files to delete afterwards]}). Both modes below fill
        # the same plan so the queueing code stays single.
        queue_plan = []

        if lang_chain:
            # Fallback group. One pass over the episode list instead of one per
            # language, with a per-language view of what is already on disk —
            # which is exactly why groups require lang_separation: the language
            # of an existing file is only knowable from its folder.
            #
            # Per episode, three outcomes:
            #   * already there in the best available language  -> nothing
            #   * not there at all                              -> download
            #   * there, but only in a worse language than the  -> upgrade:
            #     best one now available                           re-download,
            #     then delete the old copies
            present_by_lang = {lang: _scan_languages([lang]) for lang in lang_chain}
            local_pairs = set()
            for _lang_present in present_by_lang.values():
                local_pairs |= _lang_present.keys()
            max_local_found = len(local_pairs & scope_pairs)

            # language -> ([series urls], [movie urls], {url: [old paths]})
            chain_buckets: dict = {}
            for (s_num, e_num, url, ep_obj, is_movie) in online_episodes:
                available = labels_from_provider_data(_pd_cache.get(url))
                chosen = pick_language(lang_chain, available)
                if not chosen:
                    # Either the episode has none of the chain's languages yet,
                    # or the prefetch failed — same conservative skip as the
                    # single-language path: try again next cycle.
                    logger.debug(
                        "Auto-sync: S%02dE%02d of '%s' offers none of %s — skipping",
                        s_num, e_num, job["title"], ", ".join(lang_chain),
                    )
                    continue
                chosen_rank = lang_chain.index(chosen)
                # Languages this episode is already on disk in, worst-ranked ones
                # first — everything ranked below `chosen` is what an upgrade
                # would replace.
                have_ranks = [
                    rank for rank, lang in enumerate(lang_chain)
                    if (s_num, e_num) in present_by_lang[lang]
                ]
                if have_ranks and min(have_ranks) <= chosen_rank:
                    # Already there in the best language available right now
                    # (or in an even better one) — leave it alone.
                    continue

                replaced = []
                if have_ranks:
                    for rank in have_ranks:
                        replaced.extend(
                            str(p) for p in present_by_lang[lang_chain[rank]][(s_num, e_num)]
                        )
                    logger.info(
                        "Auto-sync: upgrading S%02dE%02d of '%s' from '%s' to '%s' "
                        "(%d old file(s) will be replaced)",
                        s_num, e_num, job["title"], lang_chain[min(have_ranks)],
                        chosen, len(replaced),
                    )
                elif chosen != lang_chain[0]:
                    logger.info(
                        "Auto-sync: S%02dE%02d of '%s' not available in '%s' — "
                        "falling back to '%s'",
                        s_num, e_num, job["title"], lang_chain[0], chosen,
                    )
                bucket = chain_buckets.setdefault(chosen, ([], [], {}))
                bucket[1 if is_movie else 0].append(url)
                if replaced:
                    bucket[2][url] = replaced

            for chosen_lang in lang_chain:  # keep the chain's order in the queue
                if chosen_lang in chain_buckets:
                    series_urls, movie_urls, replace_map = chain_buckets[chosen_lang]
                    queue_plan.append((chosen_lang, series_urls, movie_urls, replace_map))

        for target_lang in target_languages:
            # Build set of downloaded (season, episode) on disk using cached scans
            downloaded_eps = set(_scan_languages([target_lang]))

            in_scope_local = downloaded_eps & scope_pairs
            if len(in_scope_local) > max_local_found:
                max_local_found = len(in_scope_local)

            # Build the lang-enum target for language-availability checks.
            # We compare by string value so it works for both aniworld and s.to
            # enums (which are separate classes with identical values).
            _lang_key = INVERSE_LANG_LABELS.get(target_lang)
            _target_lang_str = None
            if _lang_key:
                _target_enum = LANG_KEY_MAP.get(_lang_key)
                if _target_enum:
                    _target_lang_str = (_target_enum[0].value, _target_enum[1].value)

            # Collect episode URLs that are not yet present on disk AND whose
            # target language is actually available online. Movies/specials are
            # collected separately so they can be queued to their own path.
            missing_series = []
            missing_movies = []
            for (s_num, e_num, url, ep_obj, is_movie) in online_episodes:
                if (s_num, e_num) in downloaded_eps:
                    continue
                # Check language availability before queuing to avoid
                # "No provider data found for language" errors in the queue worker.
                if _target_lang_str is not None:
                    # Use pre-fetched provider_data from cache (avoids repeated HTTP requests)
                    pd_data = _pd_cache.get(url)
                    if pd_data is None:
                        # Prefetch failed — skip conservatively to avoid queueing
                        # episodes that will fail with "No provider data for language"
                        logger.debug(
                            "Auto-sync: provider data unavailable for S%02dE%02d of '%s' — skipping",
                            s_num, e_num, job["title"],
                        )
                        lang_available = False
                    else:
                        try:
                            lang_available = any(
                                (k[0].value, k[1].value) == _target_lang_str
                                for k in pd_data
                            )
                        except Exception as exc:
                            logger.warning(
                                "Auto-sync: could not check language availability for S%02dE%02d of '%s': %s — skipping conservatively",
                                s_num, e_num, job["title"], exc,
                            )
                            lang_available = False  # can't verify → skip to avoid failed queue entries
                    if not lang_available:
                        logger.debug(
                            "Auto-sync: S%02dE%02d not yet available in '%s' for '%s' — skipping",
                            s_num, e_num, target_lang, job["title"],
                        )
                        continue
                if is_movie:
                    missing_movies.append(url)
                else:
                    missing_series.append(url)

            queue_plan.append((target_lang, missing_series, missing_movies, {}))

        _src = ("sync:all_langs"
                if job.get("language") == "All Languages" else "sync")
        for (target_lang, missing_series, missing_movies, replace_map) in queue_plan:
            # Queue series episodes and movie episodes as separate entries so they
            # can land in different download paths.
            for (_group, _path_id, _kind) in (
                (missing_series, job.get("custom_path_id"), "series"),
                (missing_movies, _movie_path_id, "movies"),
            ):
                if not _group:
                    continue
                with _dl_lock:
                    # Skip only if THESE episodes already overlap a queued/running
                    # item for this language (lets series + movies queue together).
                    if is_series_queued_or_running(
                        job["series_url"], language=target_lang,
                        requested_episodes=_group,
                    ):
                        logger.info(
                            "Auto-sync skipped '%s' (%s, %s) — already queued/running",
                            job["title"], target_lang, _kind,
                        )
                        continue

                    total_new_queued += len(_group)
                    if queue_downloads:
                        add_to_queue(
                            title=job["title"],
                            series_url=job["series_url"],
                            episodes=_group,
                            language=target_lang,
                            provider=job["provider"],
                            username=job.get("added_by"),
                            custom_path_id=_path_id,
                            source=_src,
                            replace_paths={
                                u: replace_map[u] for u in _group if u in replace_map
                            },
                        )
                if queue_downloads:
                    logger.info(
                        "Auto-sync queued %d %s episode(s) for '%s' (%s)",
                        len(_group), _kind, job["title"], target_lang,
                    )

        update_fields = {
            "last_check": now_str,
            "episodes_found": total_online_count,
            "local_episodes_found": max_local_found,
            "retry_count": 0,
        }

        # Only update last_new_found / last_new_count when episodes genuinely appeared online.
        # After a filter change (filter_dirty) the previous baseline was measured
        # against a different scope, so the delta is meaningless — recompute the
        # baseline silently and clear the flag without firing a "new" badge.
        if has_new_episodes_online and not _filter_dirty:
            update_fields["last_new_found"] = now_str
            update_fields["last_new_count"] = total_online_count - previous_episodes_found
        else:
            # Reset badge counter so UI shows "up to date" after a clean check
            update_fields["last_new_count"] = 0
        if _filter_dirty:
            update_fields["filter_dirty"] = 0

        update_fields["last_error"] = None  # clear any previous error on success
        update_autosync_job(job["id"], **update_fields)

        # Telemetry: stage-3 run statistic — no series title/URL, just the
        # outcome of this one sync attempt.
        telemetry_client.submit(telemetry_events.build_feature_detail_event(
            "detail.autosync", action="run", status="success",
            metadata={"episodes_found": total_online_count, "newly_queued": total_new_queued},
        ))

        # Notify when episodes were actually queued for download
        if total_new_queued > 0 and queue_downloads:
            from .notifications import notify_all
            _lang = _job_notif_lang(job)
            notify_all(
                title=job["title"],
                body=_tr(_lang,
                    f"⬇️ {total_new_queued} neue Folge(n) werden heruntergeladen",
                    f"⬇️ {total_new_queued} new episode(s) are being downloaded"),
                event="on_autosync",
                username=job.get("added_by"),
                episode_count=total_new_queued,
            )
    except Exception as e:
        from datetime import datetime

        # Transient network errors (timeout, connection refused, DNS) are
        # expected occasionally and should not count as retryable failures.
        # Log as WARNING without traceback and skip retry-count increment.
        #
        # "could not read the series page" belongs in that same category, and this is why:
        # it fires when the site hands back something that isn't the series page — an
        # outage, a maintenance page, a rate limit after a burst of jobs, a bot check. All
        # of those pass on their own. Counting them as real failures would burn through the
        # retry budget of every job in one bad sync cycle and start firing error
        # notifications about a site that was simply busy for a minute. The job still gets
        # its error text and still gets retried; it just doesn't get punished for weather.
        _net_keywords = ("ReadTimeout", "ConnectTimeout", "ConnectionError",
                         "TimeoutError", "timed out", "timeout", "ConnectionRefused",
                         "RemoteDisconnected", "NameResolutionError",
                         # …and the site telling us, in its own words, that it is not
                         # available right now. See AniworldSeries.__diagnose(): these are
                         # the phrases it produces for an outage, a rate limit or a bot
                         # wall. A layout change is deliberately NOT in this list — that
                         # one is our bug and should be shouted about, not shrugged off.
                         "is down or in maintenance", "rate-limiting us", "bot check",
                         "empty response", "refused the request",
                         "could not read the series page")
        _is_transient = any(kw.lower() in type(e).__name__.lower() or kw.lower() in str(e).lower()
                            for kw in _net_keywords)

        # What telemetry gets to know. "error_type: RuntimeError" was true and told us
        # nothing: it could not distinguish "aniworld was down for a minute" from "their
        # layout changed and our parser is broken for everybody" — and those two want very
        # different reactions from us. So we send the *class* of failure and the HTTP status
        # instead, both derived from the message, never the message itself.
        #
        # Never the message itself, because it can contain a series URL, and never the job
        # title, because that is what the user is watching. A failure class and a status
        # code say everything we need and nothing about the person.
        _err_text = str(e).lower()
        if "layout" in _err_text:
            _failure = "layout_changed"        # our bug — the one worth paging about
        elif "isn't valid text" in _err_text:
            # See AniworldSeries.__diagnose(): a 200 with a real body that isn't
            # decodable text (usually a Content-Encoding we can't decompress, e.g.
            # Brotli without the niquests[brotli] extra) looks identical to a layout
            # change from here — title extraction fails either way — but the fix is
            # completely different (a dependency, not a parser update). Still "our
            # bug" and still every job's next attempt fails the same way, so it gets
            # the same shared-backoff treatment below, just with an accurate message.
            _failure = "decode_error"
        elif "is down or in maintenance" in _err_text or "empty response" in _err_text:
            _failure = "site_down"
        elif "rate-limiting" in _err_text:
            _failure = "rate_limited"
        elif "bot check" in _err_text:
            _failure = "bot_wall"
        elif "does not exist" in _err_text:
            _failure = "series_gone"
        elif _is_transient:
            _failure = "network"
        else:
            _failure = "other"

        _status_match = re.search(r"HTTP (\d{3})", str(e))
        _telemetry_meta = {
            "error_type": type(e).__name__,
            "failure": _failure,
            "provider": job.get("provider") or "aniworld",
        }
        if _status_match:
            _telemetry_meta["http_status"] = int(_status_match.group(1))

        # A layout change (or a body we can't decode — see the decode_error branch
        # above) is "our bug" (see the _net_keywords comment above), but it is also, by
        # nature, everybody's bug at once: whatever the cause, every job's next attempt
        # fails the exact same way. Left alone, that means a burst of jobs due around
        # the same time (e.g. right after startup with 50 jobs configured) each hit the
        # same failure within seconds of each other and each fire their own
        # "Sync-Fehler" Pushover notification — a wall of duplicate alerts for one event.
        #
        # So both failure classes get handled before the transient/non-transient split
        # below: log it once as a warning (not an error — nothing here needs a stack
        # trace, the message already says exactly what's wrong), open a shared backoff
        # window that holds back every job due in the next few minutes by a few more
        # (see runtime_state.trigger_layout_backoff() and the check at the top of this
        # function), and notify only once per window instead of once per job.
        if _failure in ("layout_changed", "decode_error"):
            _is_new_backoff = trigger_layout_backoff()
            logger.warning(
                "Auto-sync: %s while checking '%s' — holding back sync jobs due in the "
                "next %d min by another %d min: %s",
                ("AniWorld's layout appears to have changed (parser needs updating)"
                 if _failure == "layout_changed" else
                 "AniWorld sent a response this build could not decode (see message)"),
                job.get("title", "?"), LAYOUT_BACKOFF_MINUTES, LAYOUT_BACKOFF_MINUTES, e,
            )
            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                last_error=str(e),
            )
            telemetry_client.submit(telemetry_events.build_feature_detail_event(
                "detail.autosync", action="run", status="layout_change_backoff",
                metadata=_telemetry_meta,
            ))
            if _is_new_backoff:
                _lang = _job_notif_lang(job)
                if _failure == "layout_changed":
                    _notif_body = _tr(_lang,
                        f"⚠️ AniWorld-Layout hat sich vermutlich geändert — Parser muss "
                        f"aktualisiert werden. Sync-Jobs pausieren für {LAYOUT_BACKOFF_MINUTES} Minuten.",
                        f"⚠️ AniWorld's layout appears to have changed — the parser needs "
                        f"updating. Sync jobs are pausing for {LAYOUT_BACKOFF_MINUTES} minutes.",
                    )
                else:
                    _notif_body = _tr(_lang,
                        f"⚠️ AniWorld hat eine Antwort geschickt, die nicht dekodiert werden "
                        f"konnte (fehlt evtl. Brotli-Unterstützung) — Sync-Jobs pausieren für "
                        f"{LAYOUT_BACKOFF_MINUTES} Minuten.",
                        f"⚠️ AniWorld sent a response that could not be decoded "
                        f"(possibly missing Brotli support) — sync jobs are pausing for "
                        f"{LAYOUT_BACKOFF_MINUTES} minutes.",
                    )
                try:
                    from .notifications import notify_all
                    notify_all(
                        title="Auto-Sync",
                        body=_notif_body,
                        event="on_sync_error",
                        username=job.get("added_by"),
                    )
                except Exception as notif_exc:
                    logger.warning("[AutoSync] Layout-change notification failed: %s", notif_exc)
            return

        if _is_transient:
            logger.warning(
                "Auto-sync error for '%s' (%s, transient — will retry next cycle): %s",
                job.get("title", "?"), _failure, e,
            )
            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                # The user gets the plain sentence, not a category and not a stack trace:
                # "aniworld.to is down or in maintenance (HTTP 503)" is something you can
                # act on (wait), which "[Netzwerkfehler] RuntimeError" is not.
                last_error=str(e),
            )
            telemetry_client.submit(telemetry_events.build_feature_detail_event(
                "detail.autosync", action="run", status="transient_error",
                metadata=_telemetry_meta,
            ))
        else:
            logger.error("Auto-sync failed for '%s': %s", job.get("title", "?"), e, exc_info=True)

            current_retry = job.get("retry_count", 0)
            max_retries = int(get_setting("sync_error_retries") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
            new_retry = current_retry + 1

            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                last_error=str(e),
                retry_count=new_retry,
            )
            telemetry_client.submit(telemetry_events.build_feature_detail_event(
                "detail.autosync", action="run", status="failed",
                metadata=dict(_telemetry_meta, retry_count=new_retry),
            ))

        # Only send error notifications for non-transient failures
        if not _is_transient:
            only_notify_on_all_failed = get_setting("notif_sync_error_only_failed_all", "0") == "1"
            should_notify = True
            if only_notify_on_all_failed:
                if new_retry <= max_retries:
                    should_notify = False

            if should_notify:
                try:
                    from .notifications import notify_all
                    _lang = _job_notif_lang(job)
                    notify_all(
                        title=job.get("title", "Auto-Sync"),
                        body=_tr(_lang, "❌ Sync-Fehler: ", "❌ Sync error: ") + str(e)[:200],
                        event="on_sync_error",
                        username=job.get("added_by"),
                    )
                except Exception as notif_exc:
                    logger.warning("[AutoSync] Error notification failed: %s", notif_exc)
    finally:
        with _syncing_jobs_lock:
            _syncing_jobs.discard(job_id)


def _autosync_worker():
    """Background thread that periodically syncs all enabled autosync jobs.

    Uses short-polling (every 10 s) and checks each job's last_check
    against the configured interval so that schedule changes take effect
    immediately instead of blocking in a long sleep.

    Also throttles a ~hourly download-history retention prune on the same
    loop iteration, and computes the interval/weekly-slot/adaptive-pause
    scheduling logic that decides whether each job's `_run_autosync_for_job`
    should run this tick.

    Used by: started as a daemon thread by `_ensure_autosync_worker()`.
    """
    import os
    from datetime import datetime, timedelta, time as dtime

    while True:
        try:
            # Throttled download-history retention prune (~hourly, also on first cycle)
            global _last_history_prune
            _now_mono = time.monotonic()
            if _now_mono - _last_history_prune > 3600:
                _last_history_prune = _now_mono
                try:
                    _hrd = int(get_setting("history_retention_days")
                               or os.environ.get("MEDIAFORGE_HISTORY_RETENTION_DAYS", "30"))
                    _pruned = prune_download_history(_hrd)
                    if _pruned:
                        logger.info("[History] pruned %d entries older than %d days", _pruned, _hrd)
                except Exception as _pe:
                    logger.debug("[History] prune failed: %s", _pe)

            mode = (os.environ.get("MEDIAFORGE_SYNC_MODE", "interval") or "interval").lower()
            schedule_key = os.environ.get("MEDIAFORGE_SYNC_SCHEDULE", "0")
            interval = SYNC_SCHEDULE_MAP.get(schedule_key, 0)

            # Nothing to do when interval mode is disabled and we're not on a weekly plan.
            if mode != "weekly" and not interval:
                time.sleep(10)
                continue

            now = datetime.utcnow()
            jobs = get_autosync_jobs()
            max_retries = int(get_setting("sync_error_retries") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRIES", "0"))
            retry_time_key = get_setting("sync_error_retry_time") or os.environ.get("MEDIAFORGE_SYNC_ERROR_RETRY_TIME", "5min")
            job_retry_interval = SYNC_RETRY_MAP.get(retry_time_key, 300)

            # Adaptive Auto-Sync: jobs that have not found a new episode for a long
            # time are slowed down to a wider re-check interval ("pause mode") until
            # something new appears again, after which they return to the normal cycle.
            adaptive_enabled = (get_setting("sync_adaptive_enabled")
                                or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_ENABLED", "0")) == "1"
            adaptive_pause_key = (get_setting("sync_adaptive_pause_after")
                                  or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_PAUSE_AFTER", "4w"))
            adaptive_pause_seconds = SYNC_ADAPTIVE_PAUSE_MAP.get(adaptive_pause_key, 4 * 7 * 86400)
            try:
                adaptive_retry_value = int(get_setting("sync_adaptive_retry_value")
                                           or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_VALUE", "2"))
            except (ValueError, TypeError):
                adaptive_retry_value = 2
            adaptive_retry_unit = (get_setting("sync_adaptive_retry_unit")
                                   or os.environ.get("MEDIAFORGE_SYNC_ADAPTIVE_RETRY_UNIT", "days"))
            adaptive_retry_interval = adaptive_retry_value * SYNC_ADAPTIVE_UNIT_MAP.get(adaptive_retry_unit, 86400)

            # Weekly plan: determine the most recent scheduled slot that has
            # already passed today (in local time), expressed as naive UTC so it
            # can be compared against the UTC-stored last_check.
            weekly_slot_utc = None
            if mode == "weekly":
                local_now = datetime.now()
                utc_off = datetime.now().astimezone().utcoffset() or timedelta(0)  # local - utc
                days = _parse_sync_days(os.environ.get("MEDIAFORGE_SYNC_DAYS", "0,1,2,3,4,5,6"))
                times = _parse_sync_times(os.environ.get("MEDIAFORGE_SYNC_TIMES", "06:00"))
                if local_now.weekday() in days and times:
                    passed = [datetime.combine(local_now.date(), dtime(h, m))
                              for (h, m) in times
                              if datetime.combine(local_now.date(), dtime(h, m)) <= local_now]
                    if passed:
                        weekly_slot_utc = max(passed) - utc_off  # local -> naive UTC

            for job in jobs:
                if not job.get("enabled"):
                    continue
                last_check = job.get("last_check")
                try:
                    last_dt = (datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S")
                               if last_check else datetime.min)
                except (ValueError, TypeError):
                    last_dt = datetime.min

                retry_count = job.get("retry_count", 0)
                in_retry = 0 < retry_count <= max_retries

                should_run = False
                if in_retry:
                    # A failed job retries on the short retry interval regardless of mode.
                    if now >= last_dt + timedelta(seconds=job_retry_interval):
                        should_run = True
                elif mode == "weekly":
                    # Run once per slot: the slot passed and the job hasn't run since.
                    if weekly_slot_utc is not None and last_dt < weekly_slot_utc <= now:
                        should_run = True
                else:
                    # Effective interval: normally the configured one, but widened
                    # to the adaptive "retry after" interval while a job is in pause
                    # mode (no new episode found for longer than the threshold).
                    eff_interval = interval
                    if interval and adaptive_enabled:
                        last_new = job.get("last_new_found")
                        ref_dt = None
                        if last_new:
                            try:
                                ref_dt = datetime.strptime(last_new, "%Y-%m-%d %H:%M:%S")
                            except (ValueError, TypeError):
                                ref_dt = None
                        if ref_dt is not None and now >= ref_dt + timedelta(seconds=adaptive_pause_seconds):
                            eff_interval = adaptive_retry_interval
                    if eff_interval and now >= last_dt + timedelta(seconds=eff_interval):
                        should_run = True

                if should_run:
                    _run_autosync_for_job(job)

            time.sleep(10)
        except Exception as e:
            logger.error("Auto-sync worker error: %s", e, exc_info=True)
            time.sleep(30)


def _ensure_autosync_worker():
    """Start the auto-sync worker thread once.

    The `_autosync_worker_started` flag makes this idempotent — needed
    because Flask's debug-mode reloader can otherwise call it twice
    (parent + child process).

    Used by: app.py's create_app(), once per running server process.
    """
    global _autosync_worker_started
    if _autosync_worker_started:
        return
    _autosync_worker_started = True
    import threading
    thread = threading.Thread(target=_autosync_worker, daemon=True)
    thread.start()
