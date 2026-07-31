"""The Comics block on the Library settings tab.

Three switches and two cache buttons, and the interesting one is the third
switch: comic_replace_original replaces the user's CBR/CBA with the converted
CBZ, which is the only setting in this block that destroys something. So the
defaults are asserted here rather than assumed -- a default that flips from 0
to 1 by accident is a data-loss bug, not a cosmetic one -- and so is the fact
that the value is normalised on the SERVER: the confirmation dialog in
settings.js is a courtesy, the validation is not.

The cache endpoints live in routes/comics.py and delete files, so they are
admin-only. test_admin_gating.py only sweeps endpoints named api_settings*,
which these are not, hence the explicit check below.
"""
import pytest

COMIC_DEFAULTS = {
    "comic_auto_prepare_all": "0",  # costs a second copy of every CBR/CBA
    "comic_replace_original": "0",  # deletes the original file
}


@pytest.fixture()
def clean_comic_settings(app):
    """Remove the three keys before and after, so a test reads the default."""
    from mediaforge.web import db

    def _clear():
        # The row has to go, not merely be emptied: get_setting() returns a
        # stored "" as "", and only a missing row falls through to the default.
        with app.app_context():
            for key in COMIC_DEFAULTS:
                db.delete_setting(key)

    _clear()
    yield
    _clear()


def test_the_defaults_are_what_the_api_serves(as_user, clean_comic_settings):
    resp = as_user("admin").get("/api/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    for key, expected in COMIC_DEFAULTS.items():
        assert data[key] == expected, f"{key} default changed"


def test_saving_and_reading_back(as_user, clean_comic_settings):
    client = as_user("admin")
    resp = client.put("/api/settings", json={
        "comic_auto_prepare_all": "1",
        "comic_replace_original": "1",
    })
    assert resp.status_code == 200
    data = client.get("/api/settings").get_json()
    assert data["comic_auto_prepare_all"] == "1"
    assert data["comic_replace_original"] == "1"


def test_a_value_that_is_not_a_boolean_is_stored_as_off(as_user, clean_comic_settings):
    """Validation is server-side. Anything that is not plainly true must end
    up as "0" -- for the destructive switch especially, "not understood" has
    to mean "off"."""
    client = as_user("admin")
    client.put("/api/settings", json={"comic_replace_original": "maybe"})
    assert client.get("/api/settings").get_json()["comic_replace_original"] == "0"
    client.put("/api/settings", json={"comic_replace_original": True})
    assert client.get("/api/settings").get_json()["comic_replace_original"] == "1"


def test_untouched_keys_are_left_alone(as_user, clean_comic_settings):
    """A PUT that carries one key must not reset the other two."""
    client = as_user("admin")
    client.put("/api/settings", json={"comic_auto_prepare_all": "1"})
    client.put("/api/settings", json={"comic_auto_prepare_all": "1"})
    data = client.get("/api/settings").get_json()
    assert data["comic_auto_prepare_all"] == "1"


# ---------------------------------------------------------------------------
# The cache endpoints
# ---------------------------------------------------------------------------

def test_the_cache_endpoints_are_admin_only(app):
    for endpoint in ("api_comic_cache", "api_comic_cache_clear"):
        assert endpoint in app.config["ADMIN_ONLY_ENDPOINTS"], endpoint


def test_a_normal_account_cannot_read_or_clear_the_caches(as_user):
    client = as_user("user")
    assert client.get("/api/library/comic/cache").status_code in (401, 403)
    resp = client.post("/api/library/comic/cache/clear", json={"cache": "covers"})
    assert resp.status_code in (401, 403)


def test_an_admin_sees_the_sizes_and_the_extractors(as_user):
    data = as_user("admin").get("/api/library/comic/cache").get_json()
    assert data["ok"] is True
    for half in ("covers", "converted"):
        assert set(data[half]) == {"files", "bytes"}
        assert data[half]["files"] >= 0
    # Whatever this machine has (usually nothing) -- the keys must be there.
    assert set(data["extractors"]) == {"rar", "ace"}


def test_clearing_needs_a_known_cache_name(as_user):
    """The name is a whitelist of two, never a path: nothing the client sends
    may decide what gets deleted."""
    client = as_user("admin")
    assert client.post("/api/library/comic/cache/clear", json={}).status_code == 400
    resp = client.post("/api/library/comic/cache/clear", json={"cache": "../../etc"})
    assert resp.status_code == 400


def test_clearing_answers_with_the_new_size(as_user):
    client = as_user("admin")
    for which in ("covers", "converted"):
        data = client.post("/api/library/comic/cache/clear", json={"cache": which}).get_json()
        assert data["ok"] is True
        assert data["cache"] == which
        assert data["stats"]["files"] == 0
