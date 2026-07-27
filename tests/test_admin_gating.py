"""Authorisation is a hand-maintained list, so guard it with a test.

MediaForge does not put @admin_required on its routes: web/app.py's
secure_endpoints() wraps every view with login_required and consults one
hand-written set (_admin_only, published as app.config["ADMIN_ONLY_ENDPOINTS"])
to decide which ones additionally need admin. A few endpoints instead check
_get_current_user_info() inline.

Either is fine -- what must never happen is neither, which is the default for a
newly added route. Exactly that gap left the whole integrations settings API
(Jellyfin/Plex/TMDB keys in clear text) and PUT /api/settings/dns open to any
logged-in account. So the test asks the question functionally: call it as a
normal user and see what comes back.
"""

import pytest

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


def test_secrets_are_not_returned_in_clear_text(app, as_user):
    """A stored secret comes back masked, never as its value."""
    from mediaforge.web import db

    with app.app_context():
        db.set_setting("mediaplayer_apikey", "super-secret-token-value")
    resp = as_user("admin").get("/api/settings/mediaplayer")
    assert resp.status_code == 200
    assert "super-secret-token-value" not in resp.get_data(as_text=True)
    assert resp.get_json()["has_token"] is True
