"""Library scoping — the half of groups that is easy to get wrong quietly.

A permission that does not apply is visible: the button is still there and it
works. A *scope* that does not apply is invisible: the library simply shows
everything, and nobody notices until it matters. So these tests come at it
from the enforcement side rather than the model side (which
tests/test_ops.py covers).
"""

import pytest


@pytest.fixture()
def scoped_group(app, users):
    """A group restricted to a custom path, with the plain user in it."""
    from mediaforge.web import groups
    from mediaforge.web.db import add_custom_path, get_custom_paths

    with app.app_context():
        add_custom_path("Pytest scoped", "/tmp/mediaforge-pytest-scoped")
        location = [p for p in get_custom_paths() if p["name"] == "Pytest scoped"][0]
        gid, err = groups.create_group(
            "pytest_scoped", "Scoped", ["library.read"], [str(location["id"])])
        assert err is None, err
        groups.set_user_groups(users["user"], [gid])
    yield {"group_id": gid, "location_id": str(location["id"])}
    with app.app_context():
        groups.set_user_groups(users["user"], [])
        groups.delete_group(gid)


def test_unrestricted_by_default(app, users):
    """Every user is in a built-in group whose scope is "*". If that simply
    won, scoping would be permanently dead — so check the default explicitly."""
    from mediaforge.web.routes.library import lib_current_scope
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert lib_current_scope() == ["*"]


def test_scope_applies_to_the_session(app, users, scoped_group):
    from mediaforge.web.routes.library import lib_current_scope, lib_scope_allows
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert lib_current_scope() == [scoped_group["location_id"]]
        assert lib_scope_allows(int(scoped_group["location_id"])) is True
        assert lib_scope_allows(None) is False        # "default" is out of scope


def test_admins_are_never_scoped(app, users, scoped_group):
    """An admin who cannot see a library cannot fix it either."""
    from mediaforge.web import groups
    from mediaforge.web.routes.library import lib_current_scope
    with app.app_context():
        groups.set_user_groups(users["admin"], [scoped_group["group_id"]])
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["admin"]
        session["user_role"] = "admin"
        assert lib_current_scope() == ["*"]
    with app.app_context():
        groups.set_user_groups(users["admin"], [])


def test_no_session_means_no_restriction(app):
    """No-auth mode has no session at all. Failing closed there would empty the
    library for every install that has never configured a group."""
    from mediaforge.web.routes.library import lib_current_scope
    with app.app_context():
        assert lib_current_scope() == ["*"]


def test_scope_survives_a_broken_group_table(app, users, monkeypatch):
    """A database mid-migration must not lock people out of their library."""
    from mediaforge.web.routes import library

    def _boom(*_a, **_kw):
        raise RuntimeError("no such table: user_groups")

    monkeypatch.setattr("mediaforge.web.groups.effective_scope", _boom)
    with app.test_request_context("/api/library"):
        from flask import session
        session["user_id"] = users["user"]
        session["user_role"] = "user"
        assert library.lib_current_scope() == ["*"]


def test_library_listing_hides_out_of_scope_locations(as_user, app, scoped_group):
    """The endpoint, not just the helper: this is what the page actually calls."""
    client = as_user("user")
    resp = client.get("/api/library?kind=video")
    assert resp.status_code == 200
    locations = resp.get_json().get("locations") or []
    keys = {str(loc.get("custom_path_id") or "default") for loc in locations}
    assert "default" not in keys, "the default location leaked into a scoped listing"


def test_overview_counters_do_not_leak_out_of_scope_sizes(as_user, scoped_group):
    """Counting everything and showing some of it is how a "restricted" view
    leaks exactly what it was meant to hide."""
    client = as_user("user")
    resp = client.get("/api/library/overview")
    assert resp.status_code == 200
    assert "counts" in resp.get_json()


def test_file_guard_refuses_out_of_scope_paths(app, users, scoped_group, tmp_path):
    """lib_resolve_library_file() is the single "may the caller touch this
    file?" answer, so the scope has to be enforced there and not per route."""
    from mediaforge.web.routes import library

    media = tmp_path / "episode.mkv"
    media.write_bytes(b"x")

    # Pretend the file lives in the default download root, which the scoped
    # group excludes.
    monkey_targets = [("Default", None, tmp_path)]
    original = library._lib_build_scan_targets
    library._lib_build_scan_targets = lambda: monkey_targets
    try:
        with app.test_request_context("/api/library"):
            from flask import session
            session["user_id"] = users["user"]
            session["user_role"] = "user"
            assert library.lib_resolve_library_file(str(media)) is None
            # A worker has no session and must not be blocked by somebody
            # else's scope.
            assert library.lib_resolve_library_file(str(media), scoped=False) is not None
    finally:
        library._lib_build_scan_targets = original


def test_library_locations_endpoint_is_admin_only(as_user):
    assert as_user("user").get("/api/ops/library-locations").status_code == 403
    resp = as_user("admin").get("/api/ops/library-locations")
    assert resp.status_code == 200
    ids = {loc["id"] for loc in resp.get_json()["locations"]}
    assert "default" in ids
