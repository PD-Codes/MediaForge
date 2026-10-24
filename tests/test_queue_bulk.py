"""/api/queue/bulk: ordering moves and retries.

* "top"/"bottom" move each row in one statement (db.move_queue_item_to) and
  keep the selected rows in their relative order -- processing the ids as
  sent used to reverse them.
* "retry" follows the single restart: only failed/cancelled/completed rows,
  and only their failed episodes when any are recorded.
"""

import json
import uuid

import pytest


@pytest.fixture()
def queued(app):
    # The queue worker runs inside the test app and would pick these rows up
    # (and reorder or start them) while a test is looking at them.
    from mediaforge.web.runtime_state import is_queue_paused, set_queue_paused

    was_paused = is_queue_paused()
    set_queue_paused(True)
    made = []

    def _make(n):
        from mediaforge.web.db import add_to_queue

        ids = []
        for _ in range(n):
            base = f"https://aniworld.to/anime/stream/{uuid.uuid4().hex}"
            ids.append(add_to_queue(
                "Bulk Test", base,
                [f"{base}/staffel-1/episode-1", f"{base}/staffel-1/episode-2"],
                "German Dub", "VOE", "test-admin",
            ))
        made.extend(ids)
        return ids

    yield _make

    from mediaforge.web.db import remove_from_queue
    for queue_id in made:
        try:
            remove_from_queue(queue_id)
        except Exception:
            pass
    set_queue_paused(was_paused)


def _queued_order(ids):
    from mediaforge.web.db import get_queue

    wanted = set(ids)
    return [row["id"] for row in get_queue()
            if row["id"] in wanted and row["status"] == "queued"]


def test_top_keeps_the_selection_in_order(client, as_user, queued):
    a, b, c, d = queued(4)
    as_user("admin")
    resp = client.post("/api/queue/bulk", json={"action": "top", "ids": [d, c]})
    assert resp.status_code == 200
    assert sorted(resp.get_json()["succeeded"]) == sorted([c, d])
    assert _queued_order([a, b, c, d]) == [c, d, a, b]


def test_bottom_keeps_the_selection_in_order(client, as_user, queued):
    a, b, c, d = queued(4)
    as_user("admin")
    resp = client.post("/api/queue/bulk", json={"action": "bottom", "ids": [b, a]})
    assert resp.status_code == 200
    assert _queued_order([a, b, c, d]) == [c, d, a, b]


def test_retry_requeues_only_failed_episodes(client, as_user, queued):
    from mediaforge.web.db import get_db, get_queue_item

    (row_id,) = queued(1)
    item = get_queue_item(row_id)
    episodes = json.loads(item["episodes"])
    conn = get_db()
    try:
        conn.execute(
            "UPDATE download_queue SET status = 'failed', errors = ? WHERE id = ?",
            (json.dumps([{"url": episodes[1], "error": "boom"}]), row_id),
        )
        conn.commit()
    finally:
        conn.close()

    as_user("admin")
    resp = client.post("/api/queue/bulk", json={"action": "retry", "ids": [row_id]})
    assert resp.get_json()["succeeded"] == [row_id]
    item = get_queue_item(row_id)
    assert item["status"] == "queued"
    assert json.loads(item["episodes"]) == [episodes[1]]


def test_retry_refuses_rows_that_are_not_finished(client, as_user, queued):
    (row_id,) = queued(1)
    as_user("admin")
    resp = client.post("/api/queue/bulk", json={"action": "retry", "ids": [row_id]})
    data = resp.get_json()
    assert data["succeeded"] == []
    assert str(row_id) in data["failed"]


def test_queue_list_has_no_episodes_and_revalidates(client, as_user, queued):
    queued(1)
    as_user("admin")
    first = client.get("/api/queue")
    assert first.status_code == 200
    items = first.get_json()["items"]
    assert items and all("episodes" not in it for it in items)
    etag = first.headers.get("ETag")
    assert etag
    again = client.get("/api/queue", headers={"If-None-Match": etag})
    assert again.status_code == 304
