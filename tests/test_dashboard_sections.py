"""The Dashboard's two card layouts: sections (default) and the free-position
grid (Beta, opt-in via home_dash_view="grid").

Both share one container that always renders (#homeDashGrid, so
home_panels.js's ResizeObserver/pointer handlers always have something to
bind to -- see its own comment); only ONE of #homeDashGrid and
#homeDashSections ever gets actual cards for a given request, decided by
app.py's _dash_sections. The interesting failure mode is rendering both (a
module widget would then exist twice with the same id) or neither.
"""

import pytest


@pytest.fixture()
def prefs(app, users):
    from mediaforge.web import db

    def _set(role, **values):
        uid = users["admin" if role == "admin" else "user"]
        with app.app_context():
            ok, err = db.set_user_ui_prefs(uid, dict(values))
            assert ok, err

    return _set


@pytest.fixture(autouse=True)
def classic_default(app):
    """Force the new (two-tab) layout on for every test in this file."""
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("new_home_enabled", "1")
    yield
    with app.app_context():
        db.set_setting("new_home_enabled", "0")


def _home(as_user, role="user"):
    resp = as_user(role).get("/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_sections_is_the_default(as_user, prefs):
    prefs("user", new_home="1", home_dash_view="")
    body = _home(as_user)
    assert 'id="homeDashSections"' in body
    for section in ("mediaforge", "system", "stats", "modules"):
        assert 'data-section="%s"' % section in body


def test_grid_is_still_reachable_as_beta(as_user, prefs):
    prefs("user", new_home="1", home_dash_view="grid")
    body = _home(as_user)
    assert 'id="homeDashSections"' not in body
    assert 'id="homeDashGrid"' in body


def test_the_grid_container_always_renders_even_in_sections_mode(as_user, prefs):
    """static/home_panels.js binds its ResizeObserver/pointer/lock handlers
    to #homeDashGrid unconditionally -- see its own SECTIONS_MODE comment.
    Removing the element in sections mode would break that script outright."""
    prefs("user", new_home="1", home_dash_view="")
    body = _home(as_user)
    assert 'id="homeDashGrid"' in body


def test_all_in_one_page_overrules_both(as_user, prefs):
    """"All in one page" is a third, older layout (home_panel_bar.js) that
    predates the grid/sections split entirely -- neither should render."""
    prefs("user", new_home="1", home_dash_enabled="all", home_dash_view="")
    body = _home(as_user)
    assert 'id="homePanelBar"' in body
    assert 'id="homeDashSections"' not in body


def test_a_junk_dash_view_value_is_refused(as_user):
    resp = as_user("user").post("/api/user/preferences", json={"home_dash_view": "spreadsheet"})
    assert resp.status_code >= 400


def test_section_order_accepts_a_permutation_and_rejects_junk(as_user):
    ok = as_user("user").post("/api/user/preferences",
                              json={"home_dash_section_order": "system,stats,mediaforge,modules"})
    assert ok.status_code == 200
    bad = as_user("user").post("/api/user/preferences",
                               json={"home_dash_section_order": "not-a-section"})
    assert bad.status_code >= 400


def test_section_layout_accepts_card_section_pairs_and_rejects_junk(as_user):
    """Per-card drag override (see static/home_panels.js's saveCardLayout()):
    "<card id>:<section>" pairs, order = render order within the section."""
    ok = as_user("user").post(
        "/api/user/preferences",
        json={"home_dash_section_layout": "queue:system,storage:mediaforge"})
    assert ok.status_code == 200
    bad = as_user("user").post(
        "/api/user/preferences",
        json={"home_dash_section_layout": "queue:not-a-section"})
    assert bad.status_code >= 400
    bad2 = as_user("user").post(
        "/api/user/preferences",
        json={"home_dash_section_layout": "no-colon-here"})
    assert bad2.status_code >= 400
