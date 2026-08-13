"""What a request with no session may reach.

Not every request carries a cookie: the external REST API authenticates with
a key, the calendar feed with a query-string token, and a module can register
a route with either. Two things followed from that and are asserted here.

The adult source used to be *allowed* for such a request, because the rule was
"no ceiling means no limit" and a request with no account has no ceiling to
read. Opting in to that source is a per-account decision, and an account is
exactly what these requests do not have.

The image proxy used to be *refused* for them, because it sat behind the
blanket login check -- so a key-authenticated client could fetch a listing
from /api/v1/ but not the posters the listing pointed at. That is what drove
modules to add image endpoints of their own, each with its own copy of the
host allowlist and the SSRF check.
"""

import pytest

from mediaforge.web import api_keys


@pytest.fixture()
def library_key(app):
    with app.app_context():
        plaintext, err = api_keys.create_key("pytest-img", ["library:read"])
        assert err is None, err
        key_id = [k["id"] for k in api_keys.list_keys() if k["name"] == "pytest-img"][0]
    yield plaintext
    with app.app_context():
        api_keys.delete_key(key_id)


@pytest.fixture()
def status_key(app):
    with app.app_context():
        plaintext, err = api_keys.create_key("pytest-img-wrong", ["status:read"])
        assert err is None, err
        key_id = [k["id"] for k in api_keys.list_keys()
                  if k["name"] == "pytest-img-wrong"][0]
    yield plaintext
    with app.app_context():
        api_keys.delete_key(key_id)


# ---------------------------------------------------------------------------
# The adult source
# ---------------------------------------------------------------------------

def test_adult_is_refused_without_a_session(app):
    from mediaforge.web.age_gate import allows_adult, has_session

    with app.test_request_context("/api/v1/status"):
        assert has_session() is False
        assert allows_adult() is False


def test_adult_is_allowed_for_an_ordinary_logged_in_account(app):
    from mediaforge.web.age_gate import allows_adult, has_session
    from flask import session

    with app.test_request_context("/"):
        session["user_id"] = 1
        session["user_role"] = "user"
        assert has_session() is True
        assert allows_adult() is True


def test_a_kids_account_is_still_refused(app):
    """The role has always won; changing the session-less answer must not
    have quietly reordered that."""
    from mediaforge.web.age_gate import allows_adult
    from flask import session

    with app.test_request_context("/"):
        session["user_id"] = 2
        session["user_role"] = "kids"
        assert allows_adult() is False


# ---------------------------------------------------------------------------
# The image proxy
# ---------------------------------------------------------------------------

def test_image_proxy_refuses_an_anonymous_caller(client, users):
    resp = client.get("/api/img?url=https://image.tmdb.org/t/p/w200/x.jpg")
    assert resp.status_code == 401
    # The API's own 401, naming the header -- not the session layer's bare
    # "authentication required", which sends people to log in for nothing.
    assert b"X-Api-Key" in resp.data


def test_image_proxy_accepts_an_api_key(client, users, library_key):
    """A bad host is fine here: 403 proves the request got past the auth gate
    and into the allowlist, which is the thing under test. Fetching a real
    image would make this a network test."""
    resp = client.get("/api/img?url=https://not-allowed.example/x.jpg",
                      headers={"X-Api-Key": library_key})
    assert resp.status_code == 403
    assert b"X-Api-Key" not in resp.data


def test_image_proxy_enforces_the_scope(client, users, status_key):
    resp = client.get("/api/img?url=https://image.tmdb.org/t/p/w200/x.jpg",
                      headers={"X-Api-Key": status_key})
    assert resp.status_code == 403
    assert b"library:read" in resp.data


def test_image_proxy_still_works_for_a_browser_session(as_user):
    resp = as_user("user").get("/api/img?url=https://not-allowed.example/x.jpg")
    # Past the auth gate, refused by the allowlist -- i.e. exactly what a
    # logged-in browser saw before the endpoint was opened to keys.
    assert resp.status_code == 403
