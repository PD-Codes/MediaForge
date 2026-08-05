"""Auto-Sync dry run.

The property that matters is not "it returns a plan" — it is that running the
preview does not change the thing it was asked to describe. A dry run that
updates ``last_check`` and clears the "new episodes" badge means the next real
run finds nothing new, which is a data-loss-shaped bug wearing a preview's
clothes.
"""

import pytest


@pytest.fixture()
def job(app):
    from mediaforge.web.db import (add_autosync_job, remove_autosync_job,
                                   get_autosync_job, update_autosync_job)
    with app.app_context():
        job_id = add_autosync_job(
            title="Pytest Dry Run",
            series_url="https://example.invalid/anime/pytest-dry-run",
            language="German Dub",
            provider="VOE",
            added_by="test-admin",
        )
        # Give it a baseline the dry run must not touch.
        update_autosync_job(job_id, last_check="2026-01-01 00:00:00",
                            episodes_found=12, last_new_count=3)
        yield get_autosync_job(job_id)
        remove_autosync_job(job_id)


def _run(app, job, **kwargs):
    from mediaforge.web.autosync_worker import _run_autosync_for_job
    with app.app_context():
        return _run_autosync_for_job(job, **kwargs)


def test_dry_run_returns_a_report_even_when_it_cannot_reach_the_provider(app, job):
    """example.invalid never resolves, which is the realistic offline case.

    The report still has to come back and say it failed, rather than raising
    or returning None -- the UI has nothing else to show.
    """
    report = _run(app, job, dry_run=True)
    assert isinstance(report, dict)
    assert report["job_id"] == job["id"]
    assert report["dry_run"] is True
    assert "would_queue" in report


def test_dry_run_does_not_touch_the_job(app, job):
    """The whole point. Checked field by field, because "mostly unchanged" is
    what this bug looks like when it comes back."""
    from mediaforge.web.db import get_autosync_job

    before = dict(get_autosync_job(job["id"]))
    _run(app, job, dry_run=True)
    after = dict(get_autosync_job(job["id"]))

    for field in ("last_check", "episodes_found", "local_episodes_found",
                  "last_new_found", "last_new_count", "retry_count",
                  "filter_dirty"):
        assert before.get(field) == after.get(field), field


def test_dry_run_queues_nothing(app, job):
    from mediaforge.web.db import get_queue

    with app.app_context():
        before = len(get_queue())
    _run(app, job, dry_run=True)
    with app.app_context():
        assert len(get_queue()) == before


def test_dry_run_implies_no_queueing_even_if_asked(app, job):
    """dry_run must win over queue_downloads, not the other way round."""
    from mediaforge.web.db import get_queue
    with app.app_context():
        before = len(get_queue())
    _run(app, job, dry_run=True, queue_downloads=True)
    with app.app_context():
        assert len(get_queue()) == before


def test_report_is_filled_in_place(app, job):
    """The caller passes the dict in so an early exit still reports something."""
    from mediaforge.web.autosync_worker import _run_autosync_for_job
    report = {}
    with app.app_context():
        returned = _run_autosync_for_job(job, dry_run=True, report=report)
    assert returned is report
    assert report["title"] == "Pytest Dry Run"


def test_blocked_run_says_why(app, job, monkeypatch):
    monkeypatch.setattr("mediaforge.web.autosync_worker.is_layout_backoff_active",
                        lambda: True)
    monkeypatch.setattr("mediaforge.web.autosync_worker.layout_backoff_remaining",
                        lambda: 300.0)
    report = _run(app, job, dry_run=True)
    assert report["blocked"] == "layout_backoff"


def test_endpoint_requires_ownership_or_admin(as_user, job):
    """A job added by somebody else must not be previewable -- the report
    names the series and how many episodes are missing."""
    resp = as_user("user").post("/api/autosync/%d/dry-run" % job["id"])
    assert resp.status_code == 403


def test_endpoint_returns_the_report(as_user, job):
    resp = as_user("admin").post("/api/autosync/%d/dry-run" % job["id"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["job_id"] == job["id"]
    assert body["dry_run"] is True


def test_endpoint_404s_for_an_unknown_job(as_user):
    assert as_user("admin").post("/api/autosync/999999/dry-run").status_code == 404
