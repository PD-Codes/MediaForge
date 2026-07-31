"""The per-account home page layout and the banner that advertises it.

Three promises, and all three are about NOT nagging people:

  * the layout is a per-ACCOUNT choice, so one person trying the new page does
    not switch it for everyone on the instance (it used to: new_home_enabled
    is a single instance-wide setting),
  * the banner is decided server-side, so a user who answered it never ships
    its markup again -- "hide it with CSS afterwards" still flashes on a slow
    paint and still tells the browser about a pitch the user rejected,
  * both answers ("try it" and "don't show again") end it for good.

The classic layout is also the only place a non-admin can reach these
settings at all: /settings redirects them, so the modal on the home page is
it. That is asserted here too, because losing it would leave someone who
dismissed the banner permanently stuck on the layout they happen to have.
"""

import pytest


@pytest.fixture()
def prefs(app, users):
    """Read/write helpers for one account's UI preferences."""
    from mediaforge.web import db

    def _set(role, **values):
        uid = users["admin" if role == "admin" else "user"]
        with app.app_context():
            ok, err = db.set_user_ui_prefs(uid, dict(values))
            assert ok, err

    def _get(role):
        uid = users["admin" if role == "admin" else "user"]
        with app.app_context():
            return db.get_user_ui_prefs(uid) or {}

    return _set, _get


@pytest.fixture(autouse=True)
def classic_default(app):
    """The instance default is the classic layout unless a test says otherwise."""
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("new_home_enabled", "0")
    yield
    with app.app_context():
        db.set_setting("new_home_enabled", "0")


def _home(as_user, role="user"):
    resp = as_user(role).get("/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# ── the banner ───────────────────────────────────────────────────────────

def test_banner_is_offered_on_the_classic_layout(as_user, prefs):
    setp, _ = prefs
    setp("user", new_home="", new_home_promo_done="")
    assert 'id="newHomePromo"' in _home(as_user)


def test_a_dismissed_banner_is_not_rendered_at_all(as_user, prefs):
    """Not "hidden" -- absent. A banner the client removes afterwards was
    still sent, and still flashes before the script runs."""
    setp, _ = prefs
    setp("user", new_home="", new_home_promo_done="1")
    body = _home(as_user)
    assert 'id="newHomePromo"' not in body
    assert "Try the new home page" not in body


def test_the_new_layout_never_advertises_itself(as_user, prefs):
    setp, _ = prefs
    setp("user", new_home="1", new_home_promo_done="")
    assert 'id="newHomePromo"' not in _home(as_user)


def test_switching_back_does_not_reopen_the_pitch(as_user, prefs):
    """Someone who tried the new page and returned has answered. Asking again
    is what turns a banner into nagging."""
    setp, _ = prefs
    setp("user", new_home="0", new_home_promo_done="1")
    assert 'id="newHomePromo"' not in _home(as_user)


# ── the layout choice ────────────────────────────────────────────────────

def test_the_account_overrules_the_instance_default(app, as_user, prefs):
    from mediaforge.web import db
    setp, _ = prefs
    with app.app_context():
        db.set_setting("new_home_enabled", "1")
    setp("user", new_home="0", new_home_promo_done="1")
    # homeFeed is the new layout's container; homeSourceChips is the classic
    # one's. Exactly one of them may be on the page.
    body = _home(as_user)
    assert 'id="homeSourceChips"' in body and 'id="homeFeed"' not in body


def test_an_empty_override_follows_the_instance_default(app, as_user, prefs):
    """"" is a real value, not a missing one: it means "whatever the admin
    set". Reading it as "0" would pin every account to the classic page the
    first time they touched the form."""
    from mediaforge.web import db
    setp, _ = prefs
    setp("user", new_home="", new_home_promo_done="1")
    with app.app_context():
        db.set_setting("new_home_enabled", "1")
    assert 'id="homeFeed"' in _home(as_user)


def test_one_account_switching_does_not_move_anybody_else(app, as_user, prefs):
    """The point of the whole change: new_home_enabled is instance-wide, so
    before this an admin looking at the new layout moved every account onto
    it."""
    setp, _ = prefs
    setp("admin", new_home="1", new_home_promo_done="1")
    setp("user", new_home="", new_home_promo_done="1")
    assert 'id="homeFeed"' in _home(as_user, "admin")
    assert 'id="homeSourceChips"' in _home(as_user, "user")


def test_the_preference_is_writable_through_the_normal_endpoint(as_user, prefs):
    _, getp = prefs
    resp = as_user("user").post("/api/user/preferences",
                                json={"new_home": "1", "new_home_promo_done": "1"})
    assert resp.status_code == 200
    stored = getp("user")
    assert stored.get("new_home") == "1"
    assert stored.get("new_home_promo_done") == "1"


def test_a_junk_layout_value_is_refused(as_user):
    """The value is echoed back into the page through window._USER_PREFS."""
    resp = as_user("user").post("/api/user/preferences", json={"new_home": "yes"})
    assert resp.status_code >= 400


# ── the way back ─────────────────────────────────────────────────────────

def test_a_non_admin_cannot_reach_the_settings_page(as_user):
    """The premise of everything below: if this ever stops being true, the
    home page modal is no longer the only route to these settings."""
    assert as_user("user").get("/settings").status_code in (301, 302)


def test_the_classic_layout_carries_the_settings_modal_too(as_user, prefs):
    """Without this, dismissing the banner is a one-way door: no settings
    page, no modal, no way back to either layout."""
    setp, _ = prefs
    setp("user", new_home="0", new_home_promo_done="1")
    body = _home(as_user)
    assert 'id="startPageOverlay"' in body
    assert 'id="spLayout-user"' in body
    assert 'id="homeCustomize"' in body
    assert "start_page.js" in body


def test_the_modal_opener_ships_with_the_classic_layout(as_user, prefs):
    """openStartPageModal() used to live in home_feed.js, which the classic
    layout does not load -- the button would have called nothing."""
    from pathlib import Path
    setp, _ = prefs
    setp("user", new_home="0", new_home_promo_done="1")
    assert "home_feed.js" not in _home(as_user)
    static = Path(__file__).resolve().parents[1] / "src/mediaforge/web/static"
    assert "openStartPageModal" in (static / "start_page.js").read_text(encoding="utf-8")
    assert "window.openStartPageModal" not in (static / "home_feed.js").read_text(encoding="utf-8")
