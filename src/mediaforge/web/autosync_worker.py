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
from .queue_worker import _dl_lock
from ..telemetry import client as telemetry_client
from ..telemetry import events as telemetry_events
from .runtime_state import (
    SYNC_ADAPTIVE_PAUSE_MAP,
    SYNC_ADAPTIVE_UNIT_MAP,
    SYNC_RETRY_MAP,
    SYNC_SCHEDULE_MAP,
    _syncing_jobs,
    _syncing_jobs_lock,
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


def _run_autosync_for_job(job, force_notify=False):
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
        _cp_id = job.get("custom_path_id")
        if _cp_id:
            _cp_record = get_custom_path_by_id(_cp_id)
            _cp_available = False
            if _cp_record:
                try:
                    _cp_available = _Path(_cp_record["path"]).expanduser().is_dir()
                except Exception:
                    _cp_available = False

            if not _cp_available:
                _global_action = os.environ.get("MEDIAFORGE_SYNC_PATH_UNAVAILABLE_ACTION", "skip").lower()
                _action = (job.get("path_unavailable_action") or _global_action or "skip").lower()
                _was_on_hold = bool(job.get("on_hold"))

                if _action == "hold":
                    if not _was_on_hold:
                        # First time going on hold — persist state + notify
                        update_autosync_job(
                            job["id"],
                            on_hold=1,
                            last_error="Custom Path nicht erreichbar — Sync pausiert (Hold)",
                        )
                        logger.warning(
                            "Auto-sync HOLD for '%s' — custom path '%s' not accessible",
                            job.get("title", "?"),
                            _cp_record["path"] if _cp_record else _cp_id,
                        )
                        try:
                            from .notifications import notify_all
                            notify_all(
                                title=job.get("title", "Auto-Sync"),
                                body="⏸ Sync pausiert: Custom Path nicht erreichbar — "
                                     + str(_cp_record['path'] if _cp_record else 'Unbekannt'),
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
                        last_error="Custom Path nicht erreichbar — Sync übersprungen",
                    )
                    return
            else:
                # Path is accessible — if we were on hold, clear it and notify resume
                if job.get("on_hold"):
                    update_autosync_job(job["id"], on_hold=0, last_error=None)
                    logger.info(
                        "Auto-sync RESUME for '%s' — custom path is accessible again",
                        job.get("title", "?"),
                    )
                    try:
                        from .notifications import notify_all
                        notify_all(
                            title=job.get("title", "Auto-Sync"),
                            body="▶️ Sync wird fortgesetzt: Custom Path ist wieder erreichbar — "
                                 + str(_cp_record['path'] if _cp_record else ''),
                            event="on_sync_resume",
                            username=job.get("added_by"),
                        )
                    except Exception as e:
                        logger.warning("[AutoSync] Resume notification failed: %s", e)

        prov = resolve_provider(job["series_url"])
        series = prov.series_cls(url=job["series_url"])

        lang_sep = os.environ.get("MEDIAFORGE_LANG_SEPARATION", "0") == "1"
        # Only use lang_sep for "All Languages" when the global setting is enabled;
        # otherwise scan root directory to avoid phantom missing-episode detection.
        if job.get("language") == "All Languages" and not lang_sep:
            logger.warning(
                "Auto-sync job '%s' uses 'All Languages' but lang_separation is off — scanning root.",
                job.get("title", "?"),
            )

        lang_folder_map = {
            "German Dub": "german-dub",
            "English Sub": "english-sub",
            "German Sub": "german-sub",
            "English Dub": "english-dub",
            "English Dub (German Sub)": "english-dub-german-sub",
        }

        target_languages = []
        if job.get("language") == "All Languages":
            disable_eng_sub = os.environ.get("MEDIAFORGE_DISABLE_ENGLISH_SUB", "0") == "1"
            for lang in lang_folder_map.keys():
                if disable_eng_sub and lang == "English Sub":
                    continue
                target_languages.append(lang)
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

        title_clean = (
            getattr(series, "title_cleaned", None) or getattr(series, "title", "")
        ).lower()
        ep_re = re.compile(r"S(\d{2})E(\d{2,3})", re.IGNORECASE)

        # mtime-based scan cache: {base_path_str -> (mtime, downloaded_eps_set)}
        # Avoids re-scanning the same folder multiple times within one sync run.
        _scan_cache: dict = {}

        def _scan_base(base: Path) -> set:
            """Return (season, episode) pairs found under base, using mtime cache."""
            key = str(base)
            try:
                mtime = base.stat().st_mtime if base.is_dir() else 0
            except OSError:
                mtime = 0
            cached = _scan_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
            eps: set = set()
            if base.is_dir() and title_clean:
                for folder in base.iterdir():
                    if not folder.is_dir() or not folder.name.lower().startswith(title_clean):
                        continue
                    for f in folder.rglob("*"):
                        if f.is_file():
                            m = ep_re.search(f.name)
                            if m:
                                eps.add((int(m.group(1)), int(m.group(2))))
            _scan_cache[key] = (mtime, eps)
            return eps

        for target_lang in target_languages:
            job_lang_folder = lang_folder_map.get(
                target_lang, target_lang.lower().replace(" ", "-")
            )

            # Build set of downloaded (season, episode) on disk using cached scans
            downloaded_eps: set = set()
            for root in scan_roots:
                base = (root / job_lang_folder) if lang_sep else root
                downloaded_eps |= _scan_base(base)

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

            _src = ("sync:all_langs"
                    if job.get("language") == "All Languages" else "sync")
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
                    add_to_queue(
                        title=job["title"],
                        series_url=job["series_url"],
                        episodes=_group,
                        language=target_lang,
                        provider=job["provider"],
                        username=job.get("added_by"),
                        custom_path_id=_path_id,
                        source=_src,
                    )
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
        if total_new_queued > 0:
            from .notifications import notify_all
            notify_all(
                title=job["title"],
                body=f"⬇️ {total_new_queued} neue Folge(n) werden heruntergeladen",
                event="on_autosync",
                username=job.get("added_by"),
                episode_count=total_new_queued,
            )
    except Exception as e:
        from datetime import datetime

        # Transient network errors (timeout, connection refused, DNS) are
        # expected occasionally and should not count as retryable failures.
        # Log as WARNING without traceback and skip retry-count increment.
        _net_keywords = ("ReadTimeout", "ConnectTimeout", "ConnectionError",
                         "TimeoutError", "timed out", "timeout", "ConnectionRefused",
                         "RemoteDisconnected", "NameResolutionError")
        _is_transient = any(kw.lower() in type(e).__name__.lower() or kw.lower() in str(e).lower()
                            for kw in _net_keywords)
        if _is_transient:
            logger.warning(
                "Auto-sync network error for '%s' (transient, will retry next cycle): %s",
                job.get("title", "?"), e,
            )
            update_autosync_job(
                job["id"],
                last_check=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                last_error=f"[Netzwerkfehler] {e}",
            )
            telemetry_client.submit(telemetry_events.build_feature_detail_event(
                "detail.autosync", action="run", status="transient_error",
                metadata={"error_type": type(e).__name__},
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
                metadata={"error_type": type(e).__name__},
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
                    notify_all(
                        title=job.get("title", "Auto-Sync"),
                        body=f"❌ Sync-Fehler: {str(e)[:200]}",
                        event="on_sync_error",
                        username=job.get("added_by"),
                    )
                except Exception as e:
                    logger.warning("[AutoSync] Error notification failed: %s", e)
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
