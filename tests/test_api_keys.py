"""Scoped API keys and the v1 API's authorisation.

The property worth guarding is that a scope actually restricts. A scoped key
that silently behaves like the old all-or-nothing one is the failure mode
here, and it is invisible: everything keeps working, which is exactly what it
looks like when nothing is enforced.
"""

import json

import pytest

from mediaforge.web import api_keys


@pytest.fixture()
def scoped_key(app):
    with app.app_context():
        plaintext, err = api_keys.create_key("pytest", ["status:read"])
        assert err is None, err
        key_id = [k["id"] for k in api_keys.list_keys() if k["name"] == "pytest"][0]
    yield {"key": plaintext, "id": key_id}
    with app.app_context():
        api_keys.delete_key(key_id)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_plaintext_is_never_stored(app, scoped_key):
    """A stolen database must not hand over working credentials."""
    from mediaforge.web.db import get_db

    with app.app_context():
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM api_keys").fetchall()
        finally:
            conn.close()

    blob = json.dumps([dict(r) for r in rows])
    assert scoped_key["key"] not in blob
    # The prefix is stored so a key is recognisable in a log, but it is far
    # too short to be useful on its own.
    assert len(scoped_key["key"]) > 30


def test_verify_round_trip(app, scoped_key):
    with app.app_context():
        resolved = api_keys.verify(scoped_key["key"])
    assert resolved and resolved["scopes"] == ["status:read"]


def test_verify_rejects_garbage(app, scoped_key):
    with app.app_context():
        assert api_keys.verify("") is None
        assert api_keys.verify("mf_not-a-real-key") is None
        assert api_keys.verify(scoped_key["key"] + "x") is None


def test_disabled_and_deleted_keys_stop_working(app, scoped_key):
    with app.app_context():
        api_keys.set_enabled(scoped_key["id"], False)
        assert api_keys.verify(scoped_key["key"]) is None
        api_keys.set_enabled(scoped_key["id"], True)
        assert api_keys.verify(scoped_key["key"]) is not None
        api_keys.delete_key(scoped_key["id"])
        assert api_keys.verify(scoped_key["key"]) is None


def test_expiry_is_honoured(app):
    with app.app_context():
        past, err = api_keys.create_key("pytest-expired", ["status:read"],
                                        expires_at="2000-01-01T00:00:00")
        assert err is None
        assert api_keys.verify(past) is None

        # An unparseable expiry counts as expired: ignoring it would let a
        # malformed date make a key eternal.
        broken, _ = api_keys.create_key("pytest-broken-expiry", ["status:read"],
                                        expires_at="not-a-date")
        assert api_keys.verify(broken) is None

        for entry in api_keys.list_keys():
            if entry["name"].startswith("pytest-"):
                api_keys.delete_key(entry["id"])


def test_unknown_scopes_are_dropped(app):
    """A scope nothing checks looks granted and does nothing."""
    with app.app_context():
        plaintext, err = api_keys.create_key(
            "pytest-scopes", ["status:read", "not.a.scope", "*"])
        assert err is None
        assert api_keys.verify(plaintext)["scopes"] == ["status:read"]
        for entry in api_keys.list_keys():
            if entry["name"] == "pytest-scopes":
                api_keys.delete_key(entry["id"])


def test_a_key_with_no_valid_scope_is_refused(app):
    with app.app_context():
        plaintext, err = api_keys.create_key("pytest-empty", ["nonsense"])
    assert plaintext is None and err == "scopes_required"


def test_list_never_exposes_the_hash(app, scoped_key):
    with app.app_context():
        for entry in api_keys.list_keys():
            assert "key_hash" not in entry


# ---------------------------------------------------------------------------
# Enforcement on the v1 API
# ---------------------------------------------------------------------------

def test_scoped_key_opens_only_its_scope(client, users, scoped_key):
    headers = {"X-Api-Key": scoped_key["key"]}
    assert client.get("/api/v1/status", headers=headers).status_code == 200
    # Same key, different resource: 403, not 401. The credential was fine.
    denied = client.get("/api/v1/library", headers=headers)
    assert denied.status_code == 403
    body = json.loads(denied.data)
    assert body["required_scope"] == "library:read"


def test_missing_key_is_401(client, users):
    assert client.get("/api/v1/status").status_code == 401


def test_unknown_key_is_401(client, users):
    assert client.get("/api/v1/status",
                      headers={"X-Api-Key": "mf_nope"}).status_code == 401


def test_legacy_key_still_grants_everything(app, client, users):
    """Breaking every existing Home Assistant integration to introduce scopes
    would be a poor trade, so the single key keeps working."""
    from mediaforge.web.db import get_setting, set_setting

    with app.app_context():
        legacy = get_setting("external_api_key", "") or "pytest-legacy-key"
        set_setting("external_api_key", legacy)

    headers = {"X-Api-Key": legacy}
    for path in ("/api/v1/status", "/api/v1/library", "/api/v1/history"):
        assert client.get(path, headers=headers).status_code == 200, path


def test_query_parameter_still_works(client, users, scoped_key):
    resp = client.get("/api/v1/status?apikey=" + scoped_key["key"])
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

def test_every_v1_endpoint_declares_a_scope(app):
    """The OpenAPI document states the scope per endpoint, so a route that is
    missing from the map would be documented as needing nothing."""
    from mediaforge.web.routes.v1_api import _V1_ENDPOINT_SCOPES

    registered = {
        rule.endpoint for rule in app.url_map.iter_rules()
        if str(rule).startswith("/api/v1/") and rule.endpoint != "api_v1_openapi"
    }
    missing = sorted(registered - set(_V1_ENDPOINT_SCOPES))
    assert not missing, "v1 endpoints with no declared scope: %s" % missing

    stale = sorted(set(_V1_ENDPOINT_SCOPES) - registered)
    assert not stale, "declared but not registered: %s" % stale


def test_every_v1_endpoint_is_login_exempt(app):
    """Regression: the exempt set used to name seven of thirteen by hand.

    The other six were wrapped in login_required, which answers /api/ paths
    with a plain 401 — so a caller with a valid API key was told its key was
    wrong, and regenerating it (the obvious next step) changed nothing.
    """
    from mediaforge.web.routes.v1_api import _V1_ENDPOINT_SCOPES

    client = app.test_client()
    for endpoint in _V1_ENDPOINT_SCOPES:
        rule = next(r for r in app.url_map.iter_rules() if r.endpoint == endpoint)
        path = str(rule).replace("<int:queue_id>", "1")
        resp = client.get(path)
        # Called with no key at all, every one of them has to answer with the
        # API's OWN 401 -- the one that names X-Api-Key -- rather than the
        # session layer's bare "authentication required".
        assert resp.status_code == 401, (endpoint, resp.status_code)
        assert b"X-Api-Key" in resp.data, (
            "%s answered the session layer's 401, so it is not login-exempt"
            % endpoint)


def test_declared_scopes_are_real(app):
    from mediaforge.web.routes.v1_api import _V1_ENDPOINT_SCOPES
    unknown = {s for s in _V1_ENDPOINT_SCOPES.values() if s not in api_keys.SCOPES}
    assert not unknown, unknown


def test_openapi_is_readable_without_a_key(client, users):
    """A client cannot know which scopes to ask for until it can read this.

    ``users`` is requested only so an admin account exists: without one the app
    redirects everything to /setup, which would make this pass for the wrong
    reason on a fresh database.
    """
    resp = client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    spec = json.loads(resp.data)
    assert spec["openapi"].startswith("3.")
    assert "/api/v1/status" in spec["paths"]
    assert spec["paths"]["/api/v1/status"]["get"]["x-required-scope"] == "status:read"


def test_openapi_converts_flask_path_parameters(client, users):
    """Flask's <int:queue_id> is not OpenAPI's {queue_id}."""
    spec = json.loads(client.get("/api/v1/openapi.json").data)
    assert "/api/v1/queue/{queue_id}" in spec["paths"]
    params = spec["paths"]["/api/v1/queue/{queue_id}"]["get"]["parameters"]
    assert params and params[0]["name"] == "queue_id"
    assert not any("<" in path for path in spec["paths"])


def test_openapi_lists_the_scope_catalogue(client, users):
    spec = json.loads(client.get("/api/v1/openapi.json").data)
    assert set(spec["x-scopes"]) == set(api_keys.SCOPES)


def test_api_key_endpoints_are_admin_only(as_user):
    assert as_user("user").get("/api/ops/api-keys").status_code == 403
    assert as_user("admin").get("/api/ops/api-keys").status_code == 200
