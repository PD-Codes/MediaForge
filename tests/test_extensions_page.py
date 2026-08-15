"""The Module Manager page is a contract, not just a layout.

The page was reworked into three views (Installed / Store / Settings) with the
store as a split browser. Everything the *rest* of the app reaches into it with
survives that rework only as long as the hooks below keep existing:

  * ``module_store.js`` renders into #extStoreList / #extStoreDetail / #extStoreRail
    and switches views through [data-extview]; it also hangs the "Update" pill on
    an installed card found by ``data-module-id``.
  * ``extension_cards.js`` drives the Modules / Theme Packs tabs through
    [data-exttab] and the state filters through #mmFilters / [data-mm-filter],
    matched against each card's ``data-mm-state``.
  * Settings → Design links here with ``#store``, which only resolves to a view
    because a [data-extview="store"] entry exists.
  * Every module's enable switch is the shared ``.thirdparty-toggle``, and every
    folder — healthy or not — keeps an ``.ext-uninstall-btn``.

A rename or a tidy-up on any of those turns into a dead button in the browser
and nothing else, which is exactly the kind of breakage a page test is for.
"""

import pytest

# id="..." / class markers the page must keep emitting, with the reason each one
# exists in the docstring above.
REQUIRED_MARKERS = [
    b'id="extViewSeg"',
    b'data-extview="installed"',
    b'data-extview="store"',
    b'data-extview="settings"',
    b'id="extInstalledView"',
    b'id="extStoreView"',
    b'id="extSettingsView"',
    b'id="extStoreRail"',
    b'id="extStoreList"',
    b'id="extStoreDetail"',
    b'id="extStoreSearch"',
    b'id="extensionsMenu"',
    b'data-exttab="modules"',
    b'data-exttab="themes"',
    b'id="extPendingBanner"',
]


@pytest.fixture
def admin(client, as_user):
    as_user("admin")
    return client


def test_page_renders_for_an_admin(admin):
    resp = admin.get("/extensions")
    assert resp.status_code == 200


@pytest.mark.parametrize("marker", REQUIRED_MARKERS)
def test_page_keeps_its_javascript_hooks(admin, marker):
    body = admin.get("/extensions").data
    assert marker in body, marker.decode()


def test_store_view_is_reachable_by_hash(admin):
    """Settings → Design links here with #store (templates/settings.html)."""
    body = admin.get("/extensions").data
    assert b'data-extview="store"' in body


def test_no_dead_markup_from_the_previous_layout(admin):
    """The old header toggle and card grid are gone, not merely hidden.

    Both were replaced (the toggle by the segmented control, the grid by the
    split browser); leaving either behind means two things claim to switch the
    same view.
    """
    body = admin.get("/extensions").data
    for gone in (b'id="extStoreToggleBtn"', b'class="mm-bar"', b'mod-card-grid'):
        assert gone not in body, gone.decode()
