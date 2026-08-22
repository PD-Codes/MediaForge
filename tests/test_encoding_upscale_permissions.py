"""The encoding and upscale queues follow the download queue's rule.

They sit in the same queue hub, next to the downloads, and used to answer with
every job in full: title and file path of whatever the other accounts were
converting. Same rule as the download queue now -- own jobs whole, everyone
else's as an anonymous placeholder, and the per-item buttons only act on your
own.

Neither table stores an owner. It does not need one: both already record the
`queue_item_id` of the download the job came from, and that row knows who
asked. A LEFT JOIN answers it for every job, including the ones created before
any of this mattered. A job with no download behind it came from the library
route, which is admin-only, so "no owner" means the instance's own.
"""

import uuid

import pytest


@pytest.fixture()
def download_of(app):
    """A download row attributed to `username`, to hang jobs off."""
    def _make(username):
        from mediaforge.web.db import add_to_queue

        base = f"https://aniworld.to/anime/stream/{uuid.uuid4().hex}"
        return add_to_queue("Test Series", base, [f"{base}/staffel-1/episode-1"],
                            "German Dub", "VOE", username)
    return _make


@pytest.fixture()
def encoding_job(download_of, tmp_path):
    def _make(username):
        from mediaforge.web.db import add_to_encoding_queue

        return add_to_encoding_queue(
            title="Test Series – S01E01.mkv",
            file_path=str(tmp_path / f"{uuid.uuid4().hex}.mkv"),
            source="download",
            queue_item_id=download_of(username),
        )
    return _make


@pytest.fixture()
def upscale_job(download_of, tmp_path):
    def _make(username):
        from mediaforge.web.db import add_to_upscale_queue

        return add_to_upscale_queue(
            title="Test Series – S01E01.mkv",
            file_path=str(tmp_path / f"{uuid.uuid4().hex}.mkv"),
            source="download",
            queue_item_id=download_of(username),
        )
    return _make


def _row(items, job_id):
    return next(i for i in items if i["id"] == job_id)


# ── Disclosure ───────────────────────────────────────────────────────────────

def test_a_foreign_encoding_job_is_anonymous(client, as_user, encoding_job):
    theirs = encoding_job("test-admin")
    as_user("user")

    row = _row(client.get("/api/encoding/queue").get_json()["items"], theirs)

    assert row["foreign"] is True
    assert row["status"]
    for leaked in ("title", "file_path", "output_path", "files", "error",
                   "username"):
        assert leaked not in row, f"a foreign encoding row still carries {leaked}"


def test_a_foreign_upscale_job_is_anonymous(client, as_user, upscale_job):
    theirs = upscale_job("test-admin")
    as_user("user")

    row = _row(client.get("/api/upscale/queue").get_json()["items"], theirs)

    assert row["foreign"] is True
    for leaked in ("title", "file_path", "output_path", "files", "error"):
        assert leaked not in row


def test_own_jobs_are_untouched(client, as_user, encoding_job, upscale_job):
    enc = encoding_job("test-user")
    ups = upscale_job("test-user")
    as_user("user")

    enc_row = _row(client.get("/api/encoding/queue").get_json()["items"], enc)
    ups_row = _row(client.get("/api/upscale/queue").get_json()["items"], ups)

    for row in (enc_row, ups_row):
        assert not row.get("foreign")
        assert row["title"].startswith("Test Series")
        assert row["file_path"]


def test_an_admin_sees_both_queues_in_full(client, as_user, encoding_job, upscale_job):
    enc = encoding_job("test-user")
    ups = upscale_job("test-user")
    as_user("admin")

    assert not _row(client.get("/api/encoding/queue").get_json()["items"], enc).get("foreign")
    assert not _row(client.get("/api/upscale/queue").get_json()["items"], ups).get("foreign")


def test_a_job_with_no_download_behind_it_is_the_instances(client, as_user, tmp_path):
    """The library route creates those, and it is admin-only."""
    from mediaforge.web.db import add_to_upscale_queue

    job = add_to_upscale_queue(title="Orphan", file_path=str(tmp_path / "x.mkv"),
                               source="library")
    as_user("user")

    assert _row(client.get("/api/upscale/queue").get_json()["items"], job)["foreign"] is True


# ── Acting on a job ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("queue,path", [
    ("encoding", "/api/encoding/queue/%d"),
    ("upscale", "/api/upscale/queue/%d"),
])
def test_a_stranger_cannot_delete_someone_elses_job(
        client, as_user, encoding_job, upscale_job, queue, path):
    job = (encoding_job if queue == "encoding" else upscale_job)("test-admin")
    as_user("user")

    assert client.delete(path % job).status_code == 404


@pytest.mark.parametrize("queue,path", [
    ("encoding", "/api/encoding/queue/%d/move"),
    ("upscale", "/api/upscale/queue/%d/move"),
])
def test_a_stranger_cannot_reorder_someone_elses_job(
        client, as_user, encoding_job, upscale_job, queue, path):
    job = (encoding_job if queue == "encoding" else upscale_job)("test-admin")
    as_user("user")

    assert client.post(path % job, json={"direction": "up"}).status_code == 404


def test_the_owner_may_delete_their_own_job(client, as_user, encoding_job):
    job = encoding_job("test-user")
    as_user("user")

    assert client.delete("/api/encoding/queue/%d" % job).status_code == 200


def test_an_admin_may_delete_anyones_job(client, as_user, upscale_job):
    job = upscale_job("test-user")
    as_user("admin")

    assert client.delete("/api/upscale/queue/%d" % job).status_code == 200


def test_clearing_finished_jobs_leaves_other_peoples_alone(
        client, as_user, encoding_job):
    from mediaforge.web.db import get_encoding_item, set_encoding_status

    theirs = encoding_job("test-admin")
    mine = encoding_job("test-user")
    for job in (theirs, mine):
        set_encoding_status(job, "completed")

    as_user("user")
    assert client.post("/api/encoding/queue/clear").status_code == 200

    assert get_encoding_item(mine) is None
    assert get_encoding_item(theirs) is not None, "cleared another account's job"
