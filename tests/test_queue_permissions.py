"""Who may act on which download-queue row.

The queue is deliberately a SHARED view: everyone sees every job, which is what
makes it useful on a household server. Acting on someone else's job is a
different question, and the answer was missing -- cancel, remove, move, restart
and the per-episode endpoints only ever took an id, so any logged-in account
could kill a download it did not start, and `/api/queue/pause` let one user
halt the queue for everyone.

The rules these tests pin down:

* per-item endpoints: owner or admin, everyone else gets 404;
* 404 rather than 403, so the API does not confirm which ids belong to whom;
* `/api/queue/bulk` obeys the same rule per id -- it must not be the way
  around the singular endpoints;
* pause/resume are instance-wide, therefore admin-only;
* clearing finished entries is scoped to your own rows instead of refused;
* a row with no owner (auth-disabled instances, older auto-sync jobs) belongs
  to the system, so only an admin may touch it.
"""

import json
import uuid

import pytest


@pytest.fixture()
def owned_by(app):
    """Queue one item attributed to `username` and return its id."""
    def _make(username):
        from mediaforge.web.db import add_to_queue

        base = f"https://aniworld.to/anime/stream/{uuid.uuid4().hex}"
        return add_to_queue(
            "Test Series", base, [f"{base}/staffel-1/episode-1"],
            "German Dub", "VOE", username,
        )
    return _make


def _row(queue_id):
    from mediaforge.web.db import get_queue_item

    return get_queue_item(queue_id)


# ── Per-item endpoints ───────────────────────────────────────────────────────

def test_a_stranger_cannot_remove_someone_elses_download(client, as_user, owned_by):
    queue_id = owned_by("test-admin")
    as_user("user")

    resp = client.delete(f"/api/queue/{queue_id}")
    assert resp.status_code == 404
    assert _row(queue_id) is not None, "the item was deleted anyway"


def test_a_stranger_cannot_cancel_someone_elses_download(client, as_user, owned_by):
    """404, not "can only cancel running items" -- the guard runs first.

    Order matters: a state-dependent error message would still tell a stranger
    that the id exists and what state it is in.
    """
    queue_id = owned_by("test-admin")
    as_user("user")

    assert client.post(f"/api/queue/{queue_id}/cancel").status_code == 404
    assert _row(queue_id)["status"] == "queued"


def test_a_stranger_cannot_move_someone_elses_download(client, as_user, owned_by):
    queue_id = owned_by("test-admin")
    before = _row(queue_id)["position"]
    as_user("user")

    resp = client.post(f"/api/queue/{queue_id}/move", json={"direction": "up"})
    assert resp.status_code == 404
    assert _row(queue_id)["position"] == before


def test_a_stranger_cannot_restart_someone_elses_download(client, as_user, owned_by):
    from mediaforge.web.db import set_queue_status

    queue_id = owned_by("test-admin")
    set_queue_status(queue_id, "failed")
    as_user("user")

    assert client.post(f"/api/queue/{queue_id}/restart").status_code == 404
    assert _row(queue_id)["status"] == "failed"


def test_the_owner_may_act_on_their_own_download(client, as_user, owned_by):
    """The guard must not lock people out of their own jobs."""
    queue_id = owned_by("test-user")
    as_user("user")

    assert client.delete(f"/api/queue/{queue_id}").status_code == 200
    assert _row(queue_id) is None


def test_an_admin_may_act_on_anyones_download(client, as_user, owned_by):
    queue_id = owned_by("test-user")
    as_user("admin")

    assert client.delete(f"/api/queue/{queue_id}").status_code == 200


def test_an_unowned_row_is_admin_only(client, as_user, owned_by):
    """No owner = the system's. Auth-disabled instances and older sync jobs."""
    queue_id = owned_by(None)

    as_user("user")
    assert client.delete(f"/api/queue/{queue_id}").status_code == 404
    assert _row(queue_id) is not None

    as_user("admin")
    assert client.delete(f"/api/queue/{queue_id}").status_code == 200


def test_refusal_looks_like_absence(client, as_user, owned_by):
    """Same answer for "not yours" and "never existed".

    A 403 on one and a 404 on the other would let anyone map which ids belong
    to which account, one request at a time.
    """
    queue_id = owned_by("test-admin")
    as_user("user")

    mine = client.delete(f"/api/queue/{queue_id}")
    nonexistent = client.delete("/api/queue/999999")
    assert mine.status_code == nonexistent.status_code == 404
    assert mine.get_json() == nonexistent.get_json()


# ── Bulk ─────────────────────────────────────────────────────────────────────

def test_bulk_cannot_be_used_to_bypass_the_per_item_rule(client, as_user, owned_by):
    theirs = owned_by("test-admin")
    mine = owned_by("test-user")
    as_user("user")

    resp = client.post("/api/queue/bulk", json={"action": "remove",
                                                "ids": [theirs, mine]})
    assert resp.status_code == 200
    data = resp.get_json()

    # The caller's own row goes; the other one is reported, not acted on.
    assert data["succeeded"] == [mine]
    assert str(theirs) in data["failed"]
    assert _row(theirs) is not None


# ── Instance-wide switches ───────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ["/api/queue/pause", "/api/queue/resume"])
def test_pausing_the_whole_queue_is_admin_only(client, as_user, endpoint):
    """One account must not be able to stop downloads for everyone."""
    as_user("user")
    assert client.post(endpoint).status_code == 403

    as_user("admin")
    assert client.post(endpoint).status_code == 200


def test_clearing_finished_entries_only_hides_your_own(client, as_user, owned_by):
    """Tidying up after yourself is fine; clearing everyone's history is not."""
    from mediaforge.web.db import set_queue_status

    theirs = owned_by("test-admin")
    mine = owned_by("test-user")
    for queue_id in (theirs, mine):
        set_queue_status(queue_id, "completed")

    as_user("user")
    assert client.delete("/api/queue/completed").status_code == 200

    assert _row(mine)["hidden"] == 1
    assert _row(theirs)["hidden"] == 0, "cleared another account's entry"


def test_an_admin_clears_the_whole_queue(client, as_user, owned_by):
    from mediaforge.web.db import set_queue_status

    theirs = owned_by("test-user")
    set_queue_status(theirs, "completed")

    as_user("admin")
    assert client.delete("/api/queue/completed").status_code == 200
    assert _row(theirs)["hidden"] == 1


# ── Unchanged on purpose ─────────────────────────────────────────────────────

def test_a_foreign_row_is_visible_but_anonymous(client, as_user, owned_by):
    """The row stays; what identifies the other person's viewing does not.

    Both halves matter. Hiding it outright would make an ordinary wait behind
    a stranger's 40-episode season look like a frozen queue -- there is one
    worker and it takes one job at a time, so "nothing ahead of me and nothing
    happening" is exactly the wrong picture. Showing it in full would tell
    every account what everyone else watches.
    """
    theirs = owned_by("test-admin")
    as_user("user")

    items = client.get("/api/queue").get_json()["items"]
    row = next(i for i in items if i["id"] == theirs)

    assert row["foreign"] is True
    assert row["status"]          # enough to explain the wait
    assert "position" in row
    for leaked in ("title", "series_url", "current_url", "errors", "username",
                   "provider", "language", "poster"):
        assert leaked not in row, f"a foreign row still carries {leaked}"


def test_own_rows_keep_everything(client, as_user, owned_by):
    """The reduction must not touch the caller's own jobs."""
    mine = owned_by("test-user")
    as_user("user")

    items = client.get("/api/queue").get_json()["items"]
    row = next(i for i in items if i["id"] == mine)

    assert not row.get("foreign")
    assert row["title"] == "Test Series"
    assert row["series_url"]


def test_an_admin_sees_every_row_in_full(client, as_user, owned_by):
    theirs = owned_by("test-user")
    as_user("admin")

    items = client.get("/api/queue").get_json()["items"]
    row = next(i for i in items if i["id"] == theirs)

    assert not row.get("foreign")
    assert row["title"] == "Test Series"


def test_the_badge_does_not_leak_other_peoples_series(client, as_user, owned_by):
    """`urls` marks browse cards as "downloading" -- on the browse page.

    Handed out whole it would say which series the other accounts are
    fetching, without anyone opening the queue at all.
    """
    from mediaforge.web.db import get_queue_item

    theirs = owned_by("test-admin")
    mine = owned_by("test-user")
    theirs_url = get_queue_item(theirs)["series_url"]
    mine_url = get_queue_item(mine)["series_url"]

    as_user("user")
    urls = client.get("/api/queue/badge").get_json()["urls"]

    # Counting is no good here -- the session DB carries rows from earlier
    # tests. What matters is which of these two shows up.
    assert mine_url in urls
    assert theirs_url not in urls, "the badge named a series the caller does not own"

    as_user("admin")
    assert theirs_url in client.get("/api/queue/badge").get_json()["urls"]


def test_queueing_a_download_is_still_open_to_everyone(client, as_user):
    """The fix must not make the app admin-only by accident."""
    as_user("user")
    base = f"https://aniworld.to/anime/stream/{uuid.uuid4().hex}"
    resp = client.post("/api/download", json={
        "title": "Test Series", "series_url": base,
        "episodes": [f"{base}/staffel-1/episode-1"],
        "language": "German Dub", "provider": "VOE",
    })
    assert resp.status_code == 200
    assert json.loads(resp.get_data())["queue_id"]
