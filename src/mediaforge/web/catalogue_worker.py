"""Bulk expansion of catalogue selections into queue items or AutoSync jobs.

The Catalogue page hands over a list of SERIES urls. ``/api/download`` wants a
list of EPISODE urls, and getting from one to the other means scraping each
series page plus every one of its seasons -- for fifty marked series that is
several hundred requests. Doing that in the browser would block the page for
minutes and lose everything on a reload, so it happens here, in one background
job the page can watch and cancel.

Two shapes of result, chosen by the user (see routes/catalogue.py):

* ``mode="queue"``    -- one queue item per series, holding every episode that
  is not already on disk. What exists today, downloaded now.
* ``mode="autosync"`` -- one AutoSync job per series. Nothing is downloaded by
  this call at all; the AutoSync worker picks it up and keeps picking up new
  episodes later. For a running series this is almost always what people
  actually mean by "get this show".

Deliberately gentle with the source sites: series are expanded ONE at a time
with a small pause between them. A bulk action is not a reason to open fifty
parallel connections to a site that is already behind DDoS-Guard -- and the
user is not waiting on the result anyway, the queue starts working after the
first series is expanded.
"""

import threading
import time
import uuid

from ..logger import get_logger

logger = get_logger(__name__)

# Pause between two series expansions. Small enough to stay responsive, large
# enough that a hundred-series selection does not read as an attack.
_SERIES_DELAY = 0.6

# Jobs are kept after they finish so the page can still show the result; this
# is how many completed ones are remembered.
_KEEP_FINISHED = 20

_lock = threading.Lock()
_jobs: dict = {}       # job_id -> job dict
_order: list = []      # job_ids, oldest first


def _new_job(source, urls, language, provider, mode, missing_only, username):
    return {
        "id": uuid.uuid4().hex[:12],
        "source": source,
        "mode": mode,
        "language": language,
        "provider": provider,
        "missing_only": bool(missing_only),
        "username": username,
        "urls": list(urls),
        "total": len(urls),
        "done": 0,
        "queued": 0,          # series that produced a queue item / autosync job
        "episodes": 0,        # episodes queued in total (queue mode only)
        "skipped": 0,         # already queued, already complete, nothing to do
        "failed": 0,
        "status": "running",  # running | finished | cancelled | error
        "current": "",
        "errors": [],         # [{title/url, error}], capped
        "started_at": time.time(),
        "finished_at": None,
        "cancel": threading.Event(),
    }


def _public(job):
    """The job as the API returns it -- without the internals."""
    return {
        "id": job["id"], "source": job["source"], "mode": job["mode"],
        "language": job["language"], "provider": job["provider"],
        "total": job["total"], "done": job["done"], "queued": job["queued"],
        "episodes": job["episodes"], "skipped": job["skipped"],
        "failed": job["failed"], "status": job["status"],
        "current": job["current"], "errors": job["errors"][:20],
        "started_at": job["started_at"], "finished_at": job["finished_at"],
    }


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def list_jobs():
    with _lock:
        return [_public(_jobs[j]) for j in _order if j in _jobs]


def cancel_job(job_id) -> bool:
    """Stop after the series currently being expanded. Already-created queue
    items are NOT removed -- they are real work the user asked for, and the
    queue has its own controls for them."""
    with _lock:
        job = _jobs.get(job_id)
    if not job or job["status"] != "running":
        return False
    job["cancel"].set()
    return True


def active_job_count() -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j["status"] == "running")


def _remember(job):
    with _lock:
        _jobs[job["id"]] = job
        _order.append(job["id"])
        # Trim finished jobs, oldest first; a running one is never dropped.
        finished = [jid for jid in _order
                    if jid in _jobs and _jobs[jid]["status"] != "running"]
        while len(finished) > _KEEP_FINISHED:
            drop = finished.pop(0)
            _jobs.pop(drop, None)
            if drop in _order:
                _order.remove(drop)


def _note_error(job, label, exc):
    job["failed"] += 1
    if len(job["errors"]) < 50:
        job["errors"].append({"title": label, "error": f"{type(exc).__name__}: {exc}"[:200]})


def _episodes_for(series_url, missing_only):
    """(title, [episode_url]) for one series, or (title, []) when there is
    nothing to download.

    ``missing_only`` drops episodes already on disk. The queue worker skips
    those anyway ("Bereits vorhanden"), but a queue item holding 300 episodes
    of which 299 are done is a queue item nobody can read.
    """
    from ..providers import resolve_provider

    provider = resolve_provider(series_url)
    if provider.series_cls is None:
        raise ValueError("this source has no series concept")
    series = provider.series_cls(url=series_url)
    title = getattr(series, "title", "") or series_url

    episode_urls = []
    for season in series.seasons:
        for episode in season.episodes:
            if missing_only:
                try:
                    if getattr(episode, "is_downloaded", False):
                        continue
                except Exception:
                    pass
            episode_urls.append(episode.url)
    return title, episode_urls


def _beat(state, detail="", error=None, handled=None, last_run=None):
    """Report to the Operations view. Never lets a telemetry-ish concern break
    the actual work -- worker_registry.beat() already swallows its own errors,
    this guards the import as well."""
    try:
        from .worker_registry import beat
        extra = {"entries": handled} if handled is not None else None
        beat("catalogue", state=state, detail=detail, error=error,
             last_run=last_run, extra=extra)
    except Exception:
        pass


def _run(job):
    from .db import add_autosync_job, add_to_queue, find_autosync_by_url
    from .db import is_series_queued_or_running
    from .worker_registry import STATE_ERROR, STATE_IDLE, STATE_WORKING

    _beat(STATE_WORKING, detail=f"{job['mode']}: 0/{job['total']}", error="", handled=0)

    for url in job["urls"]:
        if job["cancel"].is_set():
            job["status"] = "cancelled"
            break
        job["current"] = url
        try:
            if job["mode"] == "autosync":
                # One job per series; an existing one is left exactly as it is
                # rather than reset -- it may carry a filter, a custom path or
                # a language the user chose deliberately.
                if find_autosync_by_url(url):
                    job["skipped"] += 1
                else:
                    from ..providers import resolve_provider
                    prov = resolve_provider(url)
                    series = prov.series_cls(url=url) if prov.series_cls else None
                    title = getattr(series, "title", "") or url
                    cover = getattr(series, "poster_url", None)
                    add_autosync_job(title, url, job["language"], job["provider"],
                                      added_by=job["username"], cover_url=cover)
                    job["queued"] += 1
            else:
                title, episodes = _episodes_for(url, job["missing_only"])
                if not episodes:
                    job["skipped"] += 1
                elif is_series_queued_or_running(url, job["language"],
                                                  requested_episodes=episodes):
                    job["skipped"] += 1
                else:
                    add_to_queue(title, url, episodes, job["language"], job["provider"],
                                  job["username"], source="catalogue")
                    job["queued"] += 1
                    job["episodes"] += len(episodes)
        except Exception as exc:
            # One unreachable series must not end a hundred-series job.
            logger.warning("[Catalogue] bulk: %s failed: %s", url, exc)
            _note_error(job, url, exc)
        finally:
            job["done"] += 1
            _beat(STATE_WORKING,
                  detail=f"{job['mode']}: {job['done']}/{job['total']}",
                  handled=job["done"])
        time.sleep(_SERIES_DELAY)

    job["current"] = ""
    if job["status"] == "running":
        job["status"] = "finished"
    job["finished_at"] = time.time()

    # A cancelled job is not an error -- the user asked for it, and the
    # project's rule is that a user aborting their own work never surfaces as
    # a failure. Only genuine expansion failures set the error state.
    import datetime as _dt
    _last_run = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if job["failed"] and job["status"] != "cancelled":
        _beat(STATE_ERROR,
              detail=f"{job['queued']} created, {job['failed']} failed",
              error=f"{job['failed']} of {job['total']} series could not be expanded",
              handled=job["done"], last_run=_last_run)
    else:
        _beat(STATE_IDLE,
              detail=f"{job['queued']} created, {job['skipped']} skipped",
              error="", handled=job["done"], last_run=_last_run)
    logger.info("[Catalogue] bulk %s (%s): %d/%d handled, %d created, %d episodes, "
                "%d skipped, %d failed",
                job["id"], job["mode"], job["done"], job["total"], job["queued"],
                job["episodes"], job["skipped"], job["failed"])


def start_job(source, urls, language, provider, mode="queue", missing_only=True,
              username=None):
    """Kick off a bulk expansion. Returns the public job dict."""
    job = _new_job(source, urls, language, provider, mode, missing_only, username)
    _remember(job)
    thread = threading.Thread(target=_run, args=(job,), daemon=True,
                              name=f"catalogue-bulk-{job['id']}")
    thread.start()
    return _public(job)
