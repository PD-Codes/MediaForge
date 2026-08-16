"""Who may reach what: admin gating, sessionless access, the kids role,
scoped API keys, the users-table migration and /profile.

Merged from: test_admin_gating.py, test_session_less_access.py, test_kids_role.py, test_api_keys.py, test_users_migration.py, test_v1_scope_registry.py, test_profile_page.py.
"""

import pytest
import json
import sqlite3

from mediaforge.web import api_keys
from mediaforge.web.routes import v1_api


# ==========================================================================
# test_admin_gating.py
#
# Authorisation is a hand-maintained list, so guard it with a test.
# 
# MediaForge does not put @admin_required on its routes: web/app.py's
# secure_endpoints() wraps every view with login_required and consults one
# hand-written set (_admin_only, published as app.config["ADMIN_ONLY_ENDPOINTS"])
# to decide which ones additionally need admin. A few endpoints instead check
# _get_current_user_info() inline.
# 
# Either is fine -- what must never happen is neither, which is the default for a
# newly added route. Exactly that gap left the whole integrations settings API
# (Jellyfin/Plex/TMDB keys in clear text) and PUT /api/settings/dns open to any
# logged-in account. So the test asks the question functionally: call it as a
# normal user and see what comes back.
# ==========================================================================
# Endpoints that match the settings pattern but are deliberately open to every
# logged-in user. Keep this list short and justified.
DELIBERATELY_OPEN = set()


def _settings_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("api_settings") and rule.endpoint not in DELIBERATELY_OPEN:
            yield rule


def _call(client, rule):
    method = "GET" if "GET" in rule.methods else sorted(rule.methods - {"HEAD", "OPTIONS"})[0]
    kwargs = {} if method == "GET" else {"json": {}}
    return client.open(str(rule), method=method, **kwargs)


def test_there_are_settings_routes(app):
    """Guards the test itself against an empty rule list."""
    assert len(list(_settings_rules(app))) > 20


def test_no_settings_endpoint_answers_a_normal_user(app, as_user):
    """The real property: a plain account must not reach instance settings."""
    client = as_user("user")
    leaked = []
    for rule in _settings_rules(app):
        resp = _call(client, rule)
        if resp.status_code not in (401, 403):
            leaked.append(f"{rule.endpoint} ({rule}) -> {resp.status_code}")
    assert not leaked, (
        "reachable by a non-admin account; add the endpoint to _admin_only in "
        "web/app.py (or check the role inline):\n  " + "\n  ".join(leaked)
    )


def test_admin_can_reach_them(app, as_user):
    """Counter-test: the gate must not lock the admin out as well."""
    client = as_user("admin")
    denied = [
        f"{rule.endpoint} -> {resp.status_code}"
        for rule in _settings_rules(app)
        if "GET" in rule.methods
        for resp in [_call(client, rule)]
        if resp.status_code in (401, 403)
    ]
    assert not denied, "admin was refused:\n  " + "\n  ".join(denied)


def test_admin_only_entries_all_exist(app):
    """No stale names in the set -- a typo silently protects nothing."""
    unknown = sorted(e for e in app.config["ADMIN_ONLY_ENDPOINTS"]
                     if e not in app.view_functions)
    assert not unknown, f"_admin_only names endpoints that do not exist: {unknown}"


def test_upscale_add_library_is_admin_only(app):
    """It replaces files in place, so it belongs in the same tier as
    library delete/rename/move."""
    assert "api_upscale_add_library" in app.config["ADMIN_ONLY_ENDPOINTS"]


def test_module_settings_api_is_admin_only(app):
    """The generic module settings pair (thirdparties/registry.py).

    Every module's card is read and written through these two routes, and they
    sat at plain login_required: any logged-in account could read a module's
    configuration, and PUT could switch a module on or off and write its extra
    settings -- the same class of decision as installing one, which has been
    admin-only all along. Secrets were masked on the way out, but the enabled
    flag, every non-secret setting and "is a token configured" were not.

    They do not start with "api_settings", so the functional sweep above never
    looked at them. That is worth a named test rather than a wider pattern:
    the sweep's pattern is what made the gap invisible, and widening it would
    only move the blind spot.
    """
    admin_only = app.config["ADMIN_ONLY_ENDPOINTS"]
    assert "api_thirdparty_settings_get" in admin_only
    assert "api_thirdparty_settings_put" in admin_only


def test_module_cards_are_not_rendered_for_a_non_admin(app):
    """The other half of the same fix.

    Monitoring and Notifications are pages a normal user may open, and both
    render module settings cards. Admin-gating the API without gating the
    cards would give that user a page of toggles that 403 on click.
    """
    from mediaforge.web.thirdparties import registry

    with app.test_request_context("/monitoring"):
        from flask import session
        session["user_id"] = 1
        session["user_role"] = "user"
        assert registry.viewer_is_admin() is False
        assert registry.resolve_settings_cards("monitoring", "anything") == []
        assert registry.resolve_dynamic_tabs("monitoring") == []


def test_module_cards_still_render_for_an_admin(app):
    """Counter-test: the gate must not empty the admin's own settings pages."""
    from mediaforge.web.thirdparties import registry

    with app.test_request_context("/monitoring"):
        from flask import session
        session["user_id"] = 1
        session["user_role"] = "admin"
        assert registry.viewer_is_admin() is True
        # Nothing is asserted about the contents -- a test instance may have no
        # modules at all. What matters is that the role is not what emptied it.
        registry.resolve_settings_cards("monitoring", "anything")
        registry.resolve_dynamic_tabs("monitoring")


def test_module_cards_are_unfiltered_outside_a_request(app):
    """A module or a background job asking what is registered has no viewer to
    judge, and must not be handed an empty list as if nothing were installed."""
    from mediaforge.web.thirdparties import registry

    with app.app_context():
        assert registry.viewer_is_admin() is True


def test_secrets_are_not_returned_in_clear_text(app, as_user):
    """A stored secret comes back masked, never as its value."""
    from mediaforge.web import db

    with app.app_context():
        db.set_setting("mediaplayer_apikey", "super-secret-token-value")
    resp = as_user("admin").get("/api/settings/mediaplayer")
    assert resp.status_code == 200
    assert "super-secret-token-value" not in resp.get_data(as_text=True)
    assert resp.get_json()["has_token"] is True


# ==========================================================================
# test_session_less_access.py
#
# What a request with no session may reach.
# 
# Not every request carries a cookie: the external REST API authenticates with
# a key, the calendar feed with a query-string token, and a module can register
# a route with either. Two things followed from that and are asserted here.
# 
# The adult source used to be *allowed* for such a request, because the rule was
# "no ceiling means no limit" and a request with no account has no ceiling to
# read. Opting in to that source is a per-account decision, and an account is
# exactly what these requests do not have.
# 
# The image proxy used to be *refused* for them, because it sat behind the
# blanket login check -- so a key-authenticated client could fetch a listing
# from /api/v1/ but not the posters the listing pointed at. That is what drove
# modules to add image endpoints of their own, each with its own copy of the
# host allowlist and the SSRF check.
# ==========================================================================
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


# ==========================================================================
# test_kids_role.py
#
# The kids ROLE — a restriction that lives on the account, not a mode.
# 
# The point of these tests is the difference between "filtered" and "refused".
# Filtering (the home feed, the library listing) depends on TMDB metadata that
# may simply be absent, so it can never be the whole answer; the refusals
# (downloads, playback, the module store) do not depend on metadata at all and
# are what actually holds. Both are checked here, and so is the one thing that
# would undo either: a kids account changing its own ceiling.
# ==========================================================================
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


def test_a_kids_account_cannot_reach_the_uptime_dashboard(kids):
    """The UpTime page lists every monitored source by label and renders its
    URL as a clickable link -- the adult one included. It had no age gate at
    all, which made /uptime a way straight to the source the rest of the app
    goes to some length to keep away from this role."""
    assert kids.get("/uptime").status_code in (302, 403)
    assert kids.get("/api/uptime/status").status_code == 403
    assert kids.get("/api/uptime/heartbeats?source=hanime").status_code == 403


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


# ==========================================================================
# test_api_keys.py
#
# Scoped API keys and the v1 API's authorisation.
# 
# The property worth guarding is that a scope actually restricts. A scoped key
# that silently behaves like the old all-or-nothing one is the failure mode
# here, and it is invisible: everything keeps working, which is exactly what it
# looks like when nothing is enforced.
# ==========================================================================
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


# ==========================================================================
# test_users_migration.py
#
# The users-table rebuild that widens role's CHECK constraint.
# 
# Written after the first version of it emptied a live instance's users table.
# The failure mode is worth spelling out, because it is not the one you would
# guess: Python's sqlite3 module only opens a transaction before DML, so the
# DDL in a rebuild (ALTER/CREATE/DROP) is committed the instant it runs.
# `conn.rollback()` therefore undoes nothing, and a rebuild that fails halfway
# leaves whatever the DDL already did.
# 
# These tests use a plain sqlite3 database rather than the app, so they run the
# migration against the exact shapes a real installation can have.
# ==========================================================================
OLD_TABLE = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    auth_method TEXT NOT NULL DEFAULT 'local',
    sso_subject TEXT,
    sso_issuer TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "t.db")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _seed(conn, table_sql=OLD_TABLE, rows=3):
    conn.executescript(table_sql)
    for i in range(rows):
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("u%d" % i, "hash%d" % i, "admin" if i == 0 else "user"))
    conn.commit()


def test_the_rebuild_keeps_every_account(conn):
    from mediaforge.web.db import _migrate_role_check

    _seed(conn)
    _migrate_role_check(conn)

    rows = conn.execute("SELECT username, role FROM users ORDER BY id").fetchall()
    assert [r["username"] for r in rows] == ["u0", "u1", "u2"]
    assert rows[0]["role"] == "admin"
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()["sql"]
    assert "'kids'" in sql
    # And the scratch table is gone rather than left lying around.
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_new_kids'").fetchone() is None


def test_a_database_that_predates_a_column_still_migrates(conn):
    """The original break: the old table had `language`, the CREATE statement
    the new table was built from did not, and copying column-for-column threw.
    Now only the columns BOTH tables have are copied."""
    from mediaforge.web.db import _migrate_role_check

    _seed(conn, OLD_TABLE.replace(
        "    language TEXT NOT NULL DEFAULT 'en',\n", ""))
    _migrate_role_check(conn)

    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3


def test_it_does_nothing_when_the_constraint_is_already_wide(conn):
    from mediaforge.web.db import _migrate_role_check

    _seed(conn, OLD_TABLE.replace("'admin', 'user'", "'admin', 'user', 'kids'"))
    ids_before = [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()]
    _migrate_role_check(conn)
    assert [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()] == ids_before


def test_the_migration_leaves_the_original_alone_when_the_copy_fails(conn, monkeypatch):
    """A failed rebuild must not touch the real table at all. The copy is
    verified BEFORE anything is dropped, which is the whole point."""
    from mediaforge.web import db as dbmod

    _seed(conn)
    # A CREATE statement the copy cannot satisfy: NOT NULL on a column the old
    # table has no value for.
    monkeypatch.setattr(dbmod, "_CREATE_TABLE", OLD_TABLE.replace(
        "CREATE TABLE users", "CREATE TABLE IF NOT EXISTS users").replace(
        "sso_subject TEXT", "sso_subject TEXT NOT NULL"))
    dbmod._migrate_role_check(conn)

    # Untouched: same rows, still under the same name.
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_new_kids'").fetchone() is None


# ---------------------------------------------------------------------------
# Recovering the databases the broken version already produced
# ---------------------------------------------------------------------------

def test_an_interrupted_rebuild_is_recovered_on_the_next_start(conn):
    """Exactly the state a live instance was left in: an EMPTY `users` table
    and every real account sitting in `users_old`. The instance asked for
    first-run setup; nothing was actually lost."""
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn)
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.executescript(OLD_TABLE.replace("'admin', 'user'", "'admin', 'user', 'kids'"))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0

    _recover_interrupted_user_rebuild(conn)

    rows = conn.execute("SELECT username FROM users ORDER BY id").fetchall()
    assert [r["username"] for r in rows] == ["u0", "u1", "u2"]
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_old'").fetchone() is None


def test_recovery_refuses_to_delete_the_newer_table_if_it_has_more_rows(conn):
    """A guess is not good enough when the alternative is deleting accounts:
    if the state does not match the known failure, both tables stay."""
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn, rows=1)
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.executescript(OLD_TABLE)
    for i in range(5):
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("newer%d" % i, "h"))
    conn.commit()

    _recover_interrupted_user_rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 5
    assert conn.execute("SELECT COUNT(*) AS c FROM users_old").fetchone()["c"] == 1


def test_recovery_is_a_no_op_on_a_healthy_database(conn):
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn)
    _recover_interrupted_user_rebuild(conn)
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3


# ==========================================================================
# test_v1_scope_registry.py
#
# The registration API third-party modules use to declare /api/v1/ scopes.
# 
# Modules used to reach into ``_V1_ENDPOINT_SCOPES`` and mutate it. Every test
# here guards one of the three things that made that a problem: an unvalidated
# scope becomes an endpoint no key can ever call, an unvalidated endpoint name
# lets a module rewrite someone else's authorisation, and an entry that outlives
# its module hands a login exemption to whatever registers under that name next.
# 
# The fourth property is the ordering one: an entry in this map must never be
# able to switch off ``admin_required``. That is asserted at the bottom.
# ==========================================================================
@pytest.fixture(autouse=True)
def clean_registry():
    """Leave the process-wide registry exactly as it was found."""
    before = {k: dict(v) for k, v in v1_api._V1_MODULE_SCOPES.items()}
    before_bp = dict(v1_api._V1_MODULE_BLUEPRINTS)
    yield
    v1_api._V1_MODULE_SCOPES.clear()
    v1_api._V1_MODULE_SCOPES.update(before)
    v1_api._V1_MODULE_BLUEPRINTS.clear()
    v1_api._V1_MODULE_BLUEPRINTS.update(before_bp)


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------

def test_a_module_can_declare_a_scope_for_its_own_endpoint():
    accepted = v1_api.register_v1_endpoint_scopes(
        "demo", {"demo_bp.api_v1_demo": "library:read"}, blueprint="demo_bp")

    assert accepted == {"demo_bp.api_v1_demo": "library:read"}
    assert v1_api.v1_endpoint_scopes()["demo_bp.api_v1_demo"] == "library:read"


def test_the_blueprint_is_derived_when_not_given():
    v1_api.register_v1_endpoint_scopes("demo", {
        "demo_bp.api_v1_a": "stats:read",
        "demo_bp.api_v1_b": "stats:read",
    })
    assert v1_api._V1_MODULE_BLUEPRINTS["demo"] == "demo_bp"


def test_registering_twice_replaces_rather_than_accumulates():
    """A live reload re-runs register(app). Names must not pile up."""
    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp.api_v1_old": "stats:read"})
    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp.api_v1_new": "stats:read"})

    scopes = v1_api.v1_endpoint_scopes()
    assert "demo_bp.api_v1_new" in scopes
    assert "demo_bp.api_v1_old" not in scopes


def test_the_returned_map_is_a_snapshot():
    """Mutating what a caller was handed must not mutate the registry."""
    scopes = v1_api.v1_endpoint_scopes()
    scopes["api_v1_status"] = "*"
    assert v1_api.v1_endpoint_scopes()["api_v1_status"] == "status:read"


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------

def test_an_unknown_scope_is_refused():
    """has_scope() would never grant it, so the endpoint would 403 forever --
    while the OpenAPI document advertised a scope no key can carry."""
    assert v1_api.register_v1_endpoint_scopes(
        "demo", {"demo_bp.api_v1_demo": "not-a-real-scope"}) == {}


def test_the_wildcard_is_refused():
    """It belongs to the legacy key. Handed to an endpoint it would make that
    endpoint reachable by every scoped key ever issued."""
    from mediaforge.web import api_keys

    assert v1_api.register_v1_endpoint_scopes(
        "demo", {"demo_bp.api_v1_demo": api_keys.WILDCARD}) == {}


def test_a_core_endpoint_cannot_be_redeclared():
    assert v1_api.register_v1_endpoint_scopes(
        "demo", {"api_v1_status": "library:read"}) == {}
    assert v1_api.v1_endpoint_scopes()["api_v1_status"] == "status:read"


def test_a_bare_endpoint_name_is_refused():
    """Bare names belong to routes registered directly on the app, i.e. to the
    core. Allowing one would let a module claim a name the core may take."""
    assert v1_api.register_v1_endpoint_scopes(
        "demo", {"api_v1_something_new": "library:read"}) == {}


def test_a_foreign_blueprint_is_refused():
    assert v1_api.register_v1_endpoint_scopes(
        "demo", {"other_bp.api_v1_demo": "library:read"},
        blueprint="demo_bp") == {}


def test_a_second_module_cannot_take_over_an_endpoint():
    v1_api.register_v1_endpoint_scopes(
        "first", {"shared_bp.api_v1_demo": "library:read"})
    assert v1_api.register_v1_endpoint_scopes(
        "second", {"shared_bp.api_v1_demo": "stats:read"}) == {}
    assert v1_api.v1_endpoint_scopes()["shared_bp.api_v1_demo"] == "library:read"


def test_one_bad_entry_does_not_drop_the_good_ones():
    """A module getting one endpoint wrong must not take the install down."""
    accepted = v1_api.register_v1_endpoint_scopes("demo", {
        "demo_bp.api_v1_good": "library:read",
        "demo_bp.api_v1_bad":  "nonsense:read",
    }, blueprint="demo_bp")
    assert accepted == {"demo_bp.api_v1_good": "library:read"}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def test_deregistering_a_blueprint_drops_its_scopes():
    """The leak this closes: the entry survived the uninstall, so a later
    route registering under the same endpoint name was born login-exempt."""
    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp.api_v1_demo": "library:read"})
    assert v1_api.unregister_v1_endpoint_scopes_for_blueprint("demo_bp") == 1
    assert "demo_bp.api_v1_demo" not in v1_api.v1_endpoint_scopes()


def test_deregistering_a_parent_takes_nested_blueprints_with_it():
    v1_api.register_v1_endpoint_scopes(
        "demo", {"parent.child.api_v1_demo": "library:read"},
        blueprint="parent.child")
    assert v1_api.unregister_v1_endpoint_scopes_for_blueprint("parent") == 1
    assert not v1_api.v1_endpoint_scopes().get("parent.child.api_v1_demo")


def test_unregister_by_item_id():
    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp.api_v1_demo": "library:read"})
    assert v1_api.unregister_v1_endpoint_scopes("demo") == 1
    assert v1_api.unregister_v1_endpoint_scopes("demo") == 0


def test_deregistering_leaves_the_core_map_alone():
    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp.api_v1_demo": "library:read"})
    v1_api.unregister_v1_endpoint_scopes_for_blueprint("demo_bp")
    assert v1_api.v1_endpoint_scopes() == dict(v1_api._V1_ENDPOINT_SCOPES)


# ---------------------------------------------------------------------------
# The ordering property
# ---------------------------------------------------------------------------

def test_a_scope_entry_cannot_un_admin_an_endpoint(app):
    """Regression: the auth pass checked the exemption set *before* the admin
    set, so naming an endpoint here also switched off admin_required for it.

    An authentication exemption must never grant an authorisation it was not
    asked about, so the check is that admin-only endpoints stay admin-only no
    matter what this registry says.
    """
    admin_only = app.config["ADMIN_ONLY_ENDPOINTS"]
    victim = "api_settings_update"
    assert victim in admin_only

    v1_api.register_v1_endpoint_scopes("demo", {"demo_bp." + victim: "library:read"})
    # A module cannot even name it, because the name it would have to use is
    # its own blueprint's -- which is the first half of the defence.
    assert victim not in v1_api._V1_MODULE_SCOPES.get("demo", {})

    # The second half: even if an entry existed, secure_endpoints() decides
    # admin first. Asserted through the running app, which has already been
    # through that pass -- an anonymous caller must not get through (401/403
    # for an API path, 302 to the login page otherwise; the point is that it
    # is not a success).
    resp = app.test_client().post("/api/settings", json={})
    assert resp.status_code >= 300, resp.status_code

    # And structurally: no endpoint may be both admin-only and login-exempt.
    assert not admin_only & set(v1_api.v1_endpoint_scopes())


# ==========================================================================
# test_profile_page.py
#
# /profile — the account's own settings.
# 
# The reason this page exists is the thing worth pinning: /settings is
# admin-only, so before it, a normal account could not reach its own theme,
# language or media-server profile at all. Every test here is about "a NORMAL
# account can do this", not about admins.
# ==========================================================================
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


# ---------------------------------------------------------------------------
# Instance default appearance + the account-backed "advanced" toggles
# ---------------------------------------------------------------------------

def test_the_instance_default_appearance_is_its_own_setting(as_user):
    """Not the per-account theme_mode/accent: those answer "what do I see",
    these answer "what does a new account see". Collapsing the two is what
    made the old Design tab claim to configure the instance while writing to
    one account."""
    from mediaforge.web.db import get_setting

    admin = as_user("admin")
    assert admin.put("/api/settings", json={"default_theme_mode": "light",
                                            "default_accent": "#123456"}).status_code == 200
    try:
        assert get_setting("default_theme_mode") == "light"
        assert get_setting("default_accent") == "#123456"
        # Reading it back is how base.html gets it into the page.
        data = admin.get("/api/settings").get_json()
        assert data["default_theme_mode"] == "light"
        assert data["default_accent"] == "#123456"
    finally:
        admin.put("/api/settings", json={"default_theme_mode": "dark",
                                         "default_accent": ""})


def test_a_junk_accent_default_is_refused(as_user):
    resp = as_user("admin").put("/api/settings", json={"default_accent": "red"})
    assert resp.status_code == 400
    # "" is a real value -- it means "use the built-in colour".
    assert as_user("admin").put("/api/settings",
                                json={"default_accent": ""}).status_code == 200


def test_the_advanced_toggles_are_account_preferences_now(as_user):
    """They were localStorage only, which made them per BROWSER: the same
    account looked different on a laptop and a phone."""
    from mediaforge.web.db import USER_UI_PREF_KEYS

    for key in ("ui_glow_effect", "ui_header_color", "ui_skeleton_loader",
                "ui_choose_border", "ui_active_download_glow", "ui_click_effect",
                "ui_icon_move", "ui_header_color_help"):
        assert key in USER_UI_PREF_KEYS

    resp = as_user("user").post("/api/user/preferences",
                                json={"ui_glow_effect": "1"})
    assert resp.status_code == 200
    assert as_user("user").post("/api/user/preferences",
                                json={"ui_glow_effect": "maybe"}).status_code >= 400
