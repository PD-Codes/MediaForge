"""Auto-Sync with many jobs: what one run costs, and who gets to run when.

The bug these guard against looked like this from the outside: with a lot of
jobs, every card said "next check: soon" and nothing seemed to happen. Three
things added up to it:

* every run fetched provider_data -- one request per episode -- for EVERY
  episode of the series, including all the ones already on disk, so one
  round of the single background worker took longer than the sync interval;
* the poll loop ran the job list it had read at the start of a long round,
  so disabled, edited or just-synced jobs ran anyway;
* "Sync all" started one thread per job, all at once.
"""

import os
import queue
import threading
import time
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# A provider that never touches the network
# ---------------------------------------------------------------------------

class _Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.urls = []

    def hit(self, url):
        with self.lock:
            self.urls.append(url)


class _FakeEpisode:
    def __init__(self, season, number, url, offers, counter):
        self.season = season
        self.episode_number = number
        self.url = url
        self._offers = offers
        self._counter = counter

    @property
    def provider_data(self):
        # One HTTP request per access in the real models.
        self._counter.hit(self.url)
        from mediaforge.config import INVERSE_LANG_LABELS, LANG_KEY_MAP
        return {LANG_KEY_MAP[INVERSE_LANG_LABELS[label]]: {"VOE": "x"}
                for label in self._offers}


class _FakeSeason:
    def __init__(self, number, episodes):
        self.season_number = number
        self.url = f"season-{number}"
        self._episode_specs = episodes
        self.episodes = []


def _fake_provider(title, base_url, offers_by_episode, counter, title_ok=True):
    """resolve_provider() stand-in: one season, episodes 1..N, each offering
    the languages listed in offers_by_episode[n]."""

    season = _FakeSeason(1, offers_by_episode)
    season.episodes = [
        _FakeEpisode(season, n, f"{base_url}/staffel-1/episode-{n}", offers, counter)
        for n, offers in sorted(offers_by_episode.items())
    ]

    class _Series:
        def __init__(self, url):
            self.url = url
            self.title = title if title_ok else None
            self.title_cleaned = title if title_ok else ""
            self.page_problem = None if title_ok else "the site served a bot check"
            self.seasons = [season]

    class _Season:
        def __init__(self, url, series):
            self.episodes = season.episodes

    class _Provider:
        name = "FakeProvider"
        series_cls = _Series
        season_cls = _Season

    return _Provider()


def _touch_episode(folder: Path, title, episode):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{title} S01E{episode:03d}.mkv").write_bytes(b"")


@pytest.fixture()
def make_job(app):
    from mediaforge.web.db import add_autosync_job, get_autosync_job, remove_autosync_job

    created = []

    def _make(language="German Dub", **fields):
        title = f"Pytest Throughput {uuid.uuid4().hex[:8]}"
        with app.app_context():
            job_id = add_autosync_job(
                title=title,
                series_url=f"https://example.invalid/anime/{uuid.uuid4().hex}",
                language=language,
                provider="VOE",
                added_by="test-admin",
            )
            if fields:
                from mediaforge.web.db import update_autosync_job
                update_autosync_job(job_id, **fields)
            created.append(job_id)
            return get_autosync_job(job_id)

    yield _make
    with app.app_context():
        for job_id in created:
            remove_autosync_job(job_id)


def _downloads() -> Path:
    return Path(os.environ["MEDIAFORGE_DOWNLOAD_PATH"])


# ---------------------------------------------------------------------------
# provider_data only where it can change the outcome
# ---------------------------------------------------------------------------

def test_episodes_already_on_disk_cost_no_provider_request(app, make_job, monkeypatch):
    from mediaforge.web import autosync_worker as aw

    job = make_job()
    counter = _Counter()
    offers = {n: ["German Dub"] for n in range(1, 6)}
    monkeypatch.setattr(aw, "resolve_provider",
                        lambda url: _fake_provider(job["title"], job["series_url"], offers, counter))
    folder = _downloads() / job["title"]
    for n in (1, 2, 3):
        _touch_episode(folder, job["title"], n)

    report = {}
    with app.app_context():
        aw._run_autosync_for_job(job, dry_run=True, report=report)

    assert report.get("ok") is True, report
    # Episodes 1-3 are on disk: nothing the site says about them changes
    # anything, so they must not cost a request each.
    assert sorted(counter.urls) == sorted(
        [f"{job['series_url']}/staffel-1/episode-{n}" for n in (4, 5)]
    )
    assert report["would_queue"] == 2
    assert report["episodes_local"] == 3


def test_a_page_that_did_not_parse_sends_no_episode_requests(app, make_job, monkeypatch):
    from mediaforge.web import autosync_worker as aw

    job = make_job()
    counter = _Counter()
    offers = {n: ["German Dub"] for n in range(1, 4)}
    monkeypatch.setattr(aw, "resolve_provider",
                        lambda url: _fake_provider(job["title"], job["series_url"], offers,
                                                   counter, title_ok=False))
    report = {}
    with app.app_context():
        aw._run_autosync_for_job(job, dry_run=True, report=report)

    assert report.get("ok") is False
    assert "bot check" in report.get("error", "")
    assert counter.urls == []


def test_fallback_group_fetches_only_what_is_missing_in_the_best_language(
        app, make_job, monkeypatch):
    from mediaforge.web import autosync_worker as aw

    monkeypatch.setenv("MEDIAFORGE_LANG_SEPARATION", "1")
    monkeypatch.setattr(aw, "is_group_ref", lambda value: value == "group:pytest")
    monkeypatch.setattr(aw, "resolve_chain", lambda value: ["German Dub", "English Sub"])
    monkeypatch.setattr(aw, "deletes_replaced", lambda value: False)

    job = make_job(language="group:pytest")
    counter = _Counter()
    offers = {
        1: ["German Dub", "English Sub"],   # on disk in German Dub: leave alone
        2: ["German Dub", "English Sub"],   # only English Sub on disk: upgrade
        3: ["English Sub"],                 # nowhere yet: fall back to English Sub
    }
    monkeypatch.setattr(aw, "resolve_provider",
                        lambda url: _fake_provider(job["title"], job["series_url"], offers, counter))
    _touch_episode(_downloads() / "german-dub" / job["title"], job["title"], 1)
    _touch_episode(_downloads() / "english-sub" / job["title"], job["title"], 2)

    report = {}
    with app.app_context():
        aw._run_autosync_for_job(job, dry_run=True, report=report)

    assert report.get("ok") is True, report
    base = job["series_url"]
    assert sorted(counter.urls) == [f"{base}/staffel-1/episode-2", f"{base}/staffel-1/episode-3"]
    planned = {(entry["language"], url)
               for entry in report["languages"] for url in entry["sample"]}
    assert planned == {
        ("German Dub", f"{base}/staffel-1/episode-2"),
        ("English Sub", f"{base}/staffel-1/episode-3"),
    }


# ---------------------------------------------------------------------------
# The poll loop runs what is due NOW, not what was due when the round began
# ---------------------------------------------------------------------------

@pytest.fixture()
def ran(monkeypatch):
    """Replace the real sync with a recorder; returns the list of runs."""
    from mediaforge.web import autosync_worker as aw

    runs = []
    monkeypatch.setattr(aw, "_run_autosync_for_job",
                        lambda job, **kwargs: runs.append((job, kwargs)) or {})
    return runs


def test_due_job_runs_with_its_current_settings(app, make_job, ran):
    from mediaforge.web import autosync_worker as aw
    from mediaforge.web.db import update_autosync_job

    snapshot = make_job()
    with app.app_context():
        update_autosync_job(snapshot["id"], language="English Sub")
        assert aw._run_due_job(snapshot) is True
    assert ran[0][0]["language"] == "English Sub"


def test_due_job_disabled_meanwhile_does_not_run(app, make_job, ran):
    from mediaforge.web import autosync_worker as aw
    from mediaforge.web.db import update_autosync_job

    snapshot = make_job()
    with app.app_context():
        update_autosync_job(snapshot["id"], enabled=0)
        assert aw._run_due_job(snapshot) is False
    assert ran == []


def test_due_job_synced_meanwhile_does_not_run_twice(app, make_job, ran):
    from mediaforge.web import autosync_worker as aw
    from mediaforge.web.db import update_autosync_job

    snapshot = make_job()
    with app.app_context():
        update_autosync_job(snapshot["id"], last_check="2099-01-01 00:00:00")
        assert aw._run_due_job(snapshot) is False
    assert ran == []


def test_due_job_waiting_for_a_manual_sync_is_left_to_it(app, make_job, ran):
    from mediaforge.web import autosync_worker as aw

    snapshot = make_job()
    with aw._syncing_jobs_lock:
        aw._queued_sync_jobs.add(snapshot["id"])
    try:
        with app.app_context():
            assert aw._run_due_job(snapshot) is False
    finally:
        with aw._syncing_jobs_lock:
            aw._queued_sync_jobs.discard(snapshot["id"])
    assert ran == []


def test_job_on_hold_is_retried_once_a_minute_not_every_tick(app, make_job, ran):
    from mediaforge.web import autosync_worker as aw

    snapshot = make_job(on_hold=1)
    with app.app_context():
        assert aw._run_due_job(snapshot) is True
        assert aw._run_due_job(snapshot) is False   # the next 10 s tick
    assert len(ran) == 1


# ---------------------------------------------------------------------------
# Manual syncs (Sync now / Sync all) run through a bounded queue
# ---------------------------------------------------------------------------

@pytest.fixture()
def manual_runner(monkeypatch):
    """A fresh manual-sync queue whose runs block until released.

    Yields a dict with the release event and what ran; the real sync is
    replaced, and the runner gets its own queue and threads so threads left
    over from other tests cannot pick these jobs up.
    """
    from mediaforge.web import autosync_worker as aw

    monkeypatch.setattr(aw, "_manual_sync_queue", queue.Queue())
    monkeypatch.setattr(aw, "_manual_sync_threads", [])
    state = {"release": threading.Event(), "lock": threading.Lock(),
             "now": 0, "max": 0, "seen": []}

    def _fake_run(job, **kwargs):
        with aw._syncing_jobs_lock:
            aw._syncing_jobs.add(job["id"])
        with state["lock"]:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["seen"].append((job["id"], job["language"], kwargs))
        try:
            state["release"].wait(15)
        finally:
            with state["lock"]:
                state["now"] -= 1
            with aw._syncing_jobs_lock:
                aw._syncing_jobs.discard(job["id"])
        return {}

    monkeypatch.setattr(aw, "_run_autosync_for_job", _fake_run)
    yield state
    state["release"].set()


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_manual_syncs_run_two_at_a_time_and_read_the_job_late(
        app, make_job, manual_runner, as_user):
    from mediaforge.web import autosync_worker as aw
    from mediaforge.web.db import update_autosync_job

    ids = [make_job()["id"] for _ in range(4)]
    with app.app_context():
        assert [aw.request_manual_sync(i, force_notify=True) for i in ids] == ["queued"] * 4
        # Still waiting: asking again adds nothing.
        assert aw.request_manual_sync(ids[-1]) == "pending"

        assert _wait_for(lambda: manual_runner["now"] == 2)
        time.sleep(0.3)
        assert manual_runner["max"] == 2

        running = as_user("admin").get("/api/autosync/running").get_json()
        assert set(ids[:2]) <= set(running["running"])
        assert set(ids[2:]) <= set(running["queued"])

        # Edited while waiting: the run uses the new settings.
        update_autosync_job(ids[3], language="English Sub")
        manual_runner["release"].set()
        assert _wait_for(lambda: len(manual_runner["seen"]) == 4)

    languages = {job_id: lang for job_id, lang, _kw in manual_runner["seen"]}
    assert languages[ids[3]] == "English Sub"
    assert all(kw == {"force_notify": True} for _id, _lang, kw in manual_runner["seen"])
    assert manual_runner["max"] == 2
    assert _wait_for(lambda: not (set(ids) & aw._queued_sync_jobs))


def test_sync_all_queues_instead_of_starting_everything(app, make_job, manual_runner, as_user):
    ids = [make_job()["id"] for _ in range(3)]
    client = as_user("admin")

    body = client.post("/api/autosync/sync-all").get_json()
    assert body["ok"] is True
    assert body["started"] >= 3

    assert _wait_for(lambda: manual_runner["now"] == 2)
    time.sleep(0.3)
    assert manual_runner["max"] == 2

    running = client.get("/api/autosync/running").get_json()
    assert set(ids) <= set(running["running"]) | set(running["queued"])

    # Pressing it again while they wait queues nothing twice.
    again = client.post("/api/autosync/sync-all").get_json()
    assert again["started"] == 0
    manual_runner["release"].set()
