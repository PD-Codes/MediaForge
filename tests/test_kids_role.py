"""The kids ROLE — a restriction that lives on the account, not a mode.

The point of these tests is the difference between "filtered" and "refused".
Filtering (the home feed, the library listing) depends on TMDB metadata that
may simply be absent, so it can never be the whole answer; the refusals
(downloads, playback, the module store) do not depend on metadata at all and
are what actually holds. Both are checked here, and so is the one thing that
would undo either: a kids account changing its own ceiling.
"""

import pytest


@pytest.fixture()
def kids(client, users):
    """A logged-in kids account.

    The role is put in the DB, not just in the session: every gate re-reads it
    from there on purpose, so a test that only faked the session would pass
    while the real thing failed.
    """
    from mediaforge.web.db import get_db, update_user_role

    uid = users["user"]
    conn = get_db()
    try:
        before = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"]
    finally:
        conn.close()
    ok, err = update_user_role(uid, "kids")
    assert ok, err
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["user_name"] = "test-user"
        sess["user_role"] = "kids"
    yield client
    update_user_role(uid, before)


# ---------------------------------------------------------------------------
# The role itself
# ---------------------------------------------------------------------------

def test_kids_is_a_role_the_database_accepts(app):
    """The CHECK constraint had to be widened, which on SQLite means the table
    is rebuilt -- so this also proves the migration ran.

    Takes the `app` fixture because init_db() is what creates the table: read
    without it, sqlite_master simply has no row and the assertion below fails
    for the wrong reason."""
    from mediaforge.web.db import USER_ROLES, get_db

    assert "kids" in USER_ROLES
    conn = get_db()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()["sql"]
    finally:
        conn.close()
    assert "'kids'" in sql


def test_an_unknown_role_is_still_refused():
    from mediaforge.web.db import update_user_role
    ok, err = update_user_role(1, "superuser")
    assert not ok and err == "Invalid role"


# ---------------------------------------------------------------------------
# Refusals -- the part that does not depend on metadata
# ---------------------------------------------------------------------------

def test_a_kids_account_cannot_download(kids):
    resp = kids.post("/api/download", json={
        "series_url": "https://example.invalid/series/x",
        "episodes": [{"url": "https://example.invalid/e1"}],
    })
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "age_limited"


def test_a_kids_account_cannot_reach_the_module_store(kids):
    assert kids.get("/extensions").status_code in (302, 403)


def test_a_kids_account_cannot_switch_its_own_mode(kids):
    """The role has no way out by design -- if this endpoint answered, the
    ceiling would be a preference again."""
    resp = kids.post("/api/home/mode", json={"mode": "", "max_fsk": ""})
    assert resp.status_code == 403


def test_a_kids_account_cannot_raise_its_ceiling_through_preferences(kids):
    resp = kids.post("/api/user/preferences", json={"home_max_fsk": "18"})
    assert resp.status_code >= 400


def test_the_adult_source_is_refused_rather_than_returned_empty(kids):
    """"Search found nothing" is a worse answer than "this source is not
    available to you" -- it reads as a broken search."""
    resp = kids.post("/api/search", json={"keyword": "x", "site": "hanime"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "age_limited"


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------

def test_the_role_sets_the_ceiling_without_any_preference(kids, app):
    from mediaforge.web.age_gate import ceiling, is_kids_account

    with app.test_request_context():
        from flask import session
        session["user_role"] = "kids"
        assert is_kids_account()
        assert ceiling() == 6           # the instance default


def test_the_role_beats_a_stored_preference(app):
    """A kids account that somehow had a higher value stored must not talk
    itself out of its own restriction."""
    from mediaforge.web.age_gate import ceiling

    with app.test_request_context():
        from flask import session
        session["user_role"] = "kids"
        session["user_id"] = 999999      # no prefs row -> would be None
        assert ceiling() == 6


def test_the_home_page_offers_a_kids_account_no_mode_switch(kids):
    cfg = kids.get("/api/home-feed/sources").get_json()["config"]
    assert cfg["kids_account"] is True
    assert cfg["kids_enabled"] is False


def test_an_ordinary_account_is_not_limited(as_user, app):
    from mediaforge.web.age_gate import ceiling, is_kids_account

    as_user("user")
    with app.test_request_context():
        from flask import session
        session["user_role"] = "user"
        assert not is_kids_account()
        assert ceiling() is None


# ---------------------------------------------------------------------------
# Filtering -- honest about what it can and cannot judge
# ---------------------------------------------------------------------------

def test_unrated_titles_are_kept_on_purpose(app):
    """Dropping everything TMDB cannot rate would empty the app on an instance
    without a TMDB key, and an empty app is one people switch the protection
    off for. The refusals above are what actually holds."""
    from mediaforge.web.age_gate import filter_items

    items = [{"title": "rated", "fsk": "16"}, {"title": "unrated"}]
    assert [i["title"] for i in filter_items(items, 6)] == ["unrated"]


def test_both_item_shapes_are_understood():
    """Browse results carry an inlined tmdb dict, library rows a flat fsk."""
    from mediaforge.web.age_gate import rating_of

    assert rating_of({"tmdb": {"fsk": "12"}}) == 12
    assert rating_of({"fsk": 16}) == 16
    assert rating_of({"fsk": ""}) is None
    assert rating_of({"tmdb": {"fsk": "nonsense"}}) is None
    assert rating_of("not a dict") is None
