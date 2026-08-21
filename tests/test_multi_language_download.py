"""Multi-language download: one file, one audio track per selected language.

The fan-out lives in routes/queue.py's /api/download: the FIRST entry of
`languages` is the primary and keeps the ordinary single-language behaviour,
every further entry becomes its own queue row carrying `path_language` so the
worker resolves the target folder against the primary instead of its own
language. What must hold is the part that is easy to break silently:

* the primary row looks exactly like a plain single-language download
  (path_language NULL), so nothing about existing jobs changes;
* the secondary rows carry the primary in path_language, which is what stops
  them writing a second file next to the first one;
* the secondary rows are queued AFTER the primary -- the worker is a single
  serial loop claiming by position, so this ordering is the only thing keeping
  two ffmpeg processes off the same output file;
* the combinations that have no meaning (fallback group, "All Languages") are
  refused rather than half-applied.
"""

import json
import uuid

import pytest


@pytest.fixture()
def merge_on():
    """Enable dl_audio_track_merge for the duration of one test."""
    from mediaforge.web.db import get_setting, set_setting

    previous = get_setting("dl_audio_track_merge")
    set_setting("dl_audio_track_merge", "1")
    yield
    set_setting("dl_audio_track_merge", previous or "0")


@pytest.fixture()
def admin(as_user):
    return as_user("admin")


def _rows(queue_id, extra_ids):
    from mediaforge.web.db import get_queue_item

    return [get_queue_item(i) for i in [queue_id] + list(extra_ids)]


def _post(client, **overrides):
    # A fresh series per call: is_series_queued_or_running() rejects an
    # overlapping episode set in the same language, so reusing one URL would
    # make each test depend on the ones before it.
    slug = uuid.uuid4().hex
    base = f"https://aniworld.to/anime/stream/{slug}"
    body = {
        "title": "Test Series",
        "series_url": base,
        "episodes": [f"{base}/staffel-1/episode-1"],
        "provider": "VOE",
    }
    body.update(overrides)
    return client.post("/api/download", json=body)


def test_single_language_is_unchanged(client, admin, merge_on):
    """A plain `language` payload must not grow a path_language."""
    resp = _post(client, language="German Dub")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["extra_queue_ids"] == []

    from mediaforge.web.db import get_queue_item

    row = get_queue_item(data["queue_id"])
    assert row["language"] == "German Dub"
    assert row["path_language"] is None


def test_fan_out_orders_primary_first_and_marks_the_rest(client, admin, merge_on):
    resp = _post(client, languages=["German Dub", "English Dub", "German Sub"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert len(data["extra_queue_ids"]) == 2

    primary, *extras = _rows(data["queue_id"], data["extra_queue_ids"])

    # The primary is an ordinary job: it produces the file, so it must resolve
    # its own folder, not somebody else's.
    assert primary["language"] == "German Dub"
    assert primary["path_language"] is None

    assert [r["language"] for r in extras] == ["English Dub", "German Sub"]
    # This is the whole point: both extras name the primary, so all three rows
    # resolve to one folder and the merge finds the file already there.
    assert all(r["path_language"] == "German Dub" for r in extras)

    # Serial worker + claim-by-position: the primary has to be claimed first or
    # an extra would create the file under its own name.
    assert primary["position"] < min(r["position"] for r in extras)

    # Same episodes everywhere, otherwise a track would land in a file that
    # never gets the rest of the season.
    eps = json.loads(primary["episodes"])
    assert all(json.loads(r["episodes"]) == eps for r in extras)


def test_duplicates_within_the_selection_are_collapsed(client, admin, merge_on):
    """Ticking the same language twice must not queue it twice."""
    resp = _post(client, languages=["German Dub", "German Dub", "English Dub"])
    assert resp.status_code == 200
    assert len(resp.get_json()["extra_queue_ids"]) == 1


def test_upscale_runs_once_not_per_language(client, admin, merge_on):
    """Upscaling every row would re-encode the finished file N times."""
    resp = _post(client, languages=["German Dub", "English Dub"], upscale=True)
    assert resp.status_code == 200
    data = resp.get_json()
    primary, extra = _rows(data["queue_id"], data["extra_queue_ids"])
    assert primary["upscale"] == 1
    assert extra["upscale"] == 0


def test_works_without_the_merge_setting(client, admin):
    """Picking several languages IS the instruction to merge them.

    dl_audio_track_merge decides whether two *independently* queued jobs should
    be merged on a guess. This request said so outright, so the worker forces
    the merge for the secondary rows and the setting is not consulted.
    """
    from mediaforge.web.db import get_setting, set_setting

    previous = get_setting("dl_audio_track_merge")
    set_setting("dl_audio_track_merge", "0")
    try:
        resp = _post(client, languages=["German Dub", "English Dub"])
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert len(resp.get_json()["extra_queue_ids"]) == 1
    finally:
        set_setting("dl_audio_track_merge", previous or "0")


def test_refused_together_with_a_language_group(client, admin, merge_on):
    # "First of these that exists" and "all of these" cannot both be meant.
    resp = _post(client, languages=["group:1", "English Dub"])
    assert resp.status_code == 400


def test_refused_together_with_all_languages(client, admin, merge_on):
    resp = _post(client, languages=["All Languages", "English Dub"])
    assert resp.status_code == 400
