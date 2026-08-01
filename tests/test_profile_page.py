"""/profile — the account's own settings.

The reason this page exists is the thing worth pinning: /settings is
admin-only, so before it, a normal account could not reach its own theme,
language or media-server profile at all. Every test here is about "a NORMAL
account can do this", not about admins.
"""


def test_a_normal_account_can_open_its_own_profile(as_user):
    resp = as_user("user").get("/profile")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The three controls that had no reachable home before.
    assert 'id="accentPresetsSettings"' in body
    assert 'id="profileThemePack"' in body
    assert 'id="profilePlayerUser"' in body


def test_the_page_is_built_like_the_other_settings_pages(as_user):
    """Without the shared container the page ran the full width of the
    viewport and looked nothing like Settings, Integrations or Notifications
    -- which is exactly what shipped the first time. The floating menu and
    the panels are the rest of that same pattern."""
    body = as_user("user").get("/profile").get_data(as_text=True)
    assert "settings-container has-floating-menu" in body
    assert 'id="profileMenu"' in body
    for panel in ("account", "appearance", "language", "mediaplayer", "home"):
        assert 'id="panel-%s"' % panel in body


def test_settings_is_still_admin_only(as_user):
    """The profile page is an addition, not a hole in the admin gate."""
    assert as_user("user").get("/settings").status_code in (302, 401, 403)


def test_the_profile_page_shows_the_account_it_belongs_to(as_user):
    body = as_user("user").get("/profile").get_data(as_text=True)
    assert "test-user" in body


# ---------------------------------------------------------------------------
# Changing your own password
# ---------------------------------------------------------------------------

def test_the_current_password_is_required(as_user):
    """An authenticated session is not enough: a session left open on a shared
    machine is exactly where someone else would change it."""
    resp = as_user("user").post("/api/user/password",
                                json={"current": "not-my-password", "new": "brandnew123"})
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "wrong-password"


def test_a_short_password_is_refused(client, users):
    from mediaforge.web.db import set_user_password
    ok, err = set_user_password(users["user"], "short")
    assert not ok and "8" in err


def test_a_password_can_actually_be_changed_and_used(client, users):
    """End to end through the DB helper, so the hash really is replaced --
    a test that only checks the endpoint's 200 would pass on a no-op."""
    from mediaforge.web.db import get_db, set_user_password, verify_user

    conn = get_db()
    try:
        before = conn.execute("SELECT username, password_hash FROM users WHERE id = ?",
                              (users["user"],)).fetchone()
        username, old_hash = before["username"], before["password_hash"]
    finally:
        conn.close()

    ok, err = set_user_password(users["user"], "a-fresh-password")
    assert ok, err
    try:
        assert verify_user(username, "a-fresh-password")
    finally:
        conn = get_db()
        try:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (old_hash, users["user"]))
            conn.commit()
        finally:
            conn.close()


def test_get_user_by_id_never_returns_the_hash():
    """It feeds a template. A password hash reaching the page is a password
    hash reaching the browser's view-source."""
    from mediaforge.web.db import get_user_by_id
    row = get_user_by_id(1) or {}
    assert "password_hash" not in row
    assert get_user_by_id(None) is None


# ---------------------------------------------------------------------------
# "Mark as unwatched" (/api/progress/clear)
# ---------------------------------------------------------------------------

def test_marking_unwatched_forgets_the_position(as_user, users):
    """Deleting the row rather than writing position 0: "Continue watching"
    lists *unfinished* positions, so a zeroed row would still be offered."""
    from mediaforge.web.db import get_watch_progress, save_watch_progress

    path = "/tmp/mf-test/ep1.mkv"
    save_watch_progress(path, 300.0, 1200.0, username="test-user")
    assert get_watch_progress(path, username="test-user")["percent"] > 0

    resp = as_user("user").post("/api/progress/clear", json={"paths": [path]})
    assert resp.status_code == 200
    assert resp.get_json()["cleared"] == 1
    assert get_watch_progress(path, username="test-user")["percent"] == 0


def test_marking_unwatched_only_touches_your_own_positions(as_user):
    """What you have watched is yours -- clearing it must not reach into
    another account's row for the same file."""
    from mediaforge.web.db import get_watch_progress, save_watch_progress

    path = "/tmp/mf-test/shared.mkv"
    save_watch_progress(path, 100.0, 1000.0, username="test-user")
    save_watch_progress(path, 900.0, 1000.0, username="somebody-else")

    as_user("user").post("/api/progress/clear", json={"paths": [path]})

    assert get_watch_progress(path, username="test-user")["percent"] == 0
    assert get_watch_progress(path, username="somebody-else")["percent"] > 0


def test_an_empty_or_oversized_list_is_refused(as_user):
    client = as_user("user")
    assert client.post("/api/progress/clear", json={"paths": []}).status_code == 400
    assert client.post("/api/progress/clear",
                       json={"paths": ["x"] * 501}).status_code == 400
