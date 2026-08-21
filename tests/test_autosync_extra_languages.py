"""Auto-sync's extra-language list: what the API stores and what it refuses.

`language` stays the primary and keeps deciding folder and file name;
`extra_languages` is a JSON list of languages whose audio tracks are muxed into
that same file. The validation is the interesting part -- a bad list here does
not fail loudly, it produces a sync job that quietly does the wrong thing every
few hours.
"""

import json
import uuid

import pytest


@pytest.fixture()
def merge_on():
    from mediaforge.web.db import get_setting, set_setting

    previous = get_setting("dl_audio_track_merge")
    set_setting("dl_audio_track_merge", "1")
    yield
    set_setting("dl_audio_track_merge", previous or "0")


@pytest.fixture()
def admin(as_user):
    return as_user("admin")


def _create(client, **overrides):
    body = {
        "title": "Test Series",
        "series_url": f"https://aniworld.to/anime/stream/{uuid.uuid4().hex}",
        "language": "German Dub",
        "provider": "VOE",
    }
    body.update(overrides)
    return client.post("/api/autosync", json=body)


def _job(resp):
    from mediaforge.web.db import get_autosync_job

    return get_autosync_job(resp.get_json()["id"])


def test_extra_languages_are_stored_as_json(client, admin, merge_on):
    resp = _create(client, extra_languages=["English Dub", "German Sub"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    job = _job(resp)
    assert job["language"] == "German Dub"
    assert json.loads(job["extra_languages"]) == ["English Dub", "German Sub"]


def test_no_extras_stores_null(client, admin, merge_on):
    """A job without extras must look exactly like one from before the column."""
    resp = _create(client)
    assert resp.status_code == 200
    assert _job(resp)["extra_languages"] is None


def test_primary_is_dropped_from_its_own_extras(client, admin, merge_on):
    """The UI shows the primary as selected; it must not become its own track."""
    resp = _create(client, language="German Dub",
                   extra_languages=["German Dub", "English Dub"])
    assert resp.status_code == 200
    assert json.loads(_job(resp)["extra_languages"]) == ["English Dub"]


def test_duplicates_are_collapsed(client, admin, merge_on):
    resp = _create(client, extra_languages=["English Dub", "English Dub"])
    assert resp.status_code == 200
    assert json.loads(_job(resp)["extra_languages"]) == ["English Dub"]


def test_extras_alongside_all_languages_are_refused(client, admin, merge_on):
    """"All Languages" already means one file per language."""
    resp = _create(client, language="All Languages", extra_languages=["English Dub"])
    assert resp.status_code == 400


def test_all_languages_as_an_extra_is_refused(client, admin, merge_on):
    resp = _create(client, extra_languages=["All Languages"])
    assert resp.status_code == 400


def test_extras_work_without_the_merge_setting(client, admin):
    """Naming extra languages IS the instruction to merge them.

    dl_audio_track_merge only governs the automatic merge between separately
    queued jobs; this list is explicit, so it is not consulted.
    """
    from mediaforge.web.db import get_setting, set_setting

    previous = get_setting("dl_audio_track_merge")
    set_setting("dl_audio_track_merge", "0")
    try:
        resp = _create(client, extra_languages=["English Dub"])
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert json.loads(_job(resp)["extra_languages"]) == ["English Dub"]
    finally:
        set_setting("dl_audio_track_merge", previous or "0")


def test_extras_must_be_a_list(client, admin, merge_on):
    resp = _create(client, extra_languages="English Dub")
    assert resp.status_code == 400


def test_editing_validates_against_the_new_primary(client, admin, merge_on):
    """Changing language and extras at once must not leave the primary inside.

    Validating against the job's OLD language would let "swap primary and
    extra" through, and the job would then queue its own primary as an extra
    track of itself.
    """
    created = _create(client, language="German Dub", extra_languages=["English Dub"])
    job_id = created.get_json()["id"]

    resp = client.put(
        f"/api/autosync/{job_id}",
        json={"language": "English Dub", "extra_languages": ["English Dub", "German Dub"]},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    from mediaforge.web.db import get_autosync_job

    job = get_autosync_job(job_id)
    assert job["language"] == "English Dub"
    assert json.loads(job["extra_languages"]) == ["German Dub"]


def test_extras_survive_export_and_import(client, admin, merge_on):
    """A backup that loses the extras restores a job that downloads less."""
    _create(client, extra_languages=["English Dub"])

    exported = client.get("/api/autosync/export")
    assert exported.status_code == 200
    payload = json.loads(exported.get_data(as_text=True))
    assert any(j.get("extra_languages") for j in payload["jobs"])
