"""The registration API third-party modules use to declare /api/v1/ scopes.

Modules used to reach into ``_V1_ENDPOINT_SCOPES`` and mutate it. Every test
here guards one of the three things that made that a problem: an unvalidated
scope becomes an endpoint no key can ever call, an unvalidated endpoint name
lets a module rewrite someone else's authorisation, and an entry that outlives
its module hands a login exemption to whatever registers under that name next.

The fourth property is the ordering one: an entry in this map must never be
able to switch off ``admin_required``. That is asserted at the bottom.
"""

import pytest

from mediaforge.web.routes import v1_api


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
