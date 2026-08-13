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
