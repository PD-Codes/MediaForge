"""A switched-off module must disappear from the lists it feeds, not just
from the sidebar.

A module's `register(app)` runs whether or not the module is enabled -- see
`thirdparties/registry.py`'s `item_enabled()` for why detaching on the disable
*edge* would not work (a module already off at boot would keep everything it
registered). The gate therefore sits at the point of use, and every consumer
has to apply it.

Two consumers did not: Settings → Sources → "Provider order" listed the module's
hoster, and "Domain fallback (mirrors)" kept an editable card for its site. The
hoster was worse than cosmetic -- it also reached the Hoster dropdown in the
download modal and the provider fallback chain, so a download could be sent to
an extractor belonging to a module that is off.
"""

import pytest

from mediaforge import extractors, mirrors
from mediaforge.web import runtime_state
from mediaforge.web.thirdparties import registry


ITEM_ID = "test_disabled_provider_mod"
HOSTER = "TestModHoster"
SITE_ID = "testmodsite"
ENABLED_KEY = "thirdparty_%s_enabled" % ITEM_ID


def _fake_direct_link(url):
    """Stand-in extractor.

    It has to RAISE for an empty URL: runtime_state._get_working_providers()
    probes every registered extractor with `""` and only counts the ones that
    raise -- a function that returns a link for no input is taken for a stub.
    """
    if not url:
        raise ValueError("no url")
    return "https://example.invalid/stream.m3u8"


@pytest.fixture(scope="module")
def module_registered(app):
    """A module that registered a hoster and a mirror list, switched ON.

    Module-scoped: unregister_hoster() deliberately leaves the name in
    config.SUPPORTED_PROVIDERS (harmless once the extractor is gone, see its
    docstring), so registering the same name a second time is refused. Each
    test re-enables it through the autouse fixture below instead.
    """
    from mediaforge.web.db import set_setting

    registry.register_thirdparty(
        item_id=ITEM_ID, label="Test Mod", enabled_setting_key=ENABLED_KEY,
    )
    extractors.register_hoster(
        item_id=ITEM_ID, name=HOSTER,
        get_direct_link=_fake_direct_link,
    )
    mirrors.register_site_mirrors(
        ITEM_ID, SITE_ID, ["testmod.example", "testmod.example.net"],
        label="Test Mod Site",
    )
    set_setting(ENABLED_KEY, "1")
    registry._invalidate_enabled_cache()

    yield

    extractors.unregister_hoster(ITEM_ID)
    mirrors.unregister_site_mirrors(ITEM_ID)
    registry.unregister_module(ITEM_ID)
    registry._invalidate_enabled_cache()


@pytest.fixture(autouse=True)
def _start_enabled(module_registered):
    """Every test starts from "module is on" -- they disable it themselves."""
    _enable()
    yield


def _disable():
    from mediaforge.web.db import set_setting

    set_setting(ENABLED_KEY, "0")
    registry._invalidate_enabled_cache()


def _enable():
    from mediaforge.web.db import set_setting

    set_setting(ENABLED_KEY, "1")
    registry._invalidate_enabled_cache()


# ── The owner lookups the gate rests on ──────────────────────────────────────

def test_the_owner_of_a_registered_hoster_can_be_found():
    assert extractors.hoster_owner(HOSTER) == ITEM_ID
    assert extractors.hoster_owner("VOE") is None, "a built-in hoster has no owner"


def test_the_owner_of_a_registered_site_can_be_found():
    assert mirrors.site_owner(SITE_ID) == ITEM_ID
    assert mirrors.site_owner("aniworld") is None


# ── Hosters ──────────────────────────────────────────────────────────────────

def test_a_disabled_modules_hoster_leaves_the_usable_list():
    assert HOSTER in runtime_state.enabled_providers()

    _disable()
    assert HOSTER not in runtime_state.enabled_providers()

    _enable()
    assert HOSTER in runtime_state.enabled_providers(), "it did not come back"


def test_the_registry_itself_still_knows_the_hoster():
    """Only the *usable* list shrinks.

    WORKING_PROVIDERS answers "does an extractor for this name exist", which
    stays true while the module is off -- and modules bind that list by
    identity at import time, so filtering it in place would reach into them.
    """
    _disable()
    assert HOSTER in runtime_state.WORKING_PROVIDERS


def test_a_disabled_modules_hoster_is_not_in_the_fallback_chain():
    """Otherwise every episode is handed to a dead extractor in turn."""
    _disable()
    chain = runtime_state.get_provider_fallback_chain("VOE")
    assert HOSTER not in chain


def test_the_provider_order_drops_it_but_keeps_the_saved_setting():
    """Switching the module back on must restore its place, not append it."""
    from mediaforge.web.db import get_setting, set_setting

    set_setting("provider_order", "%s,VOE" % HOSTER)
    assert runtime_state.get_provider_order()[0] == HOSTER

    _disable()
    assert HOSTER not in runtime_state.get_provider_order()
    assert HOSTER in get_setting("provider_order", ""), "the saved order was rewritten"

    _enable()
    assert runtime_state.get_provider_order()[0] == HOSTER


# ── What the Settings page and the download modal are handed ─────────────────

def test_settings_stops_listing_the_hoster_and_the_mirror_card(client, as_user):
    as_user("admin")

    data = client.get("/api/settings").get_json()
    assert HOSTER in data["providers"]["available"]
    assert any(s["id"] == SITE_ID for s in data["mirrors"]["sites"])

    _disable()

    data = client.get("/api/settings").get_json()
    assert HOSTER not in data["providers"]["available"], \
        "Provider order still lists a disabled module's hoster"
    assert not any(s["id"] == SITE_ID for s in data["mirrors"]["sites"]), \
        "Domain fallback still shows a disabled module's site"


def test_custom_paths_stop_offering_the_site(client, as_user):
    """"Default for <site>" must not name a source that cannot be used."""
    as_user("admin")

    options = client.get("/api/custom-paths").get_json()["site_options"]
    assert any(o["key"] == SITE_ID for o in options)

    _disable()
    options = client.get("/api/custom-paths").get_json()["site_options"]
    assert not any(o["key"] == SITE_ID for o in options)


def test_a_builtin_hoster_is_never_hidden():
    """The gate must only ever remove module-owned entries."""
    _disable()
    usable = runtime_state.enabled_providers()
    assert usable, "the hoster list was emptied"
    assert set(usable) == {p for p in runtime_state.WORKING_PROVIDERS if p != HOSTER}
