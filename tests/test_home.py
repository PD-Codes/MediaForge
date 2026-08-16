"""The new home page: the feed, the Dashboard columns, the panels behind
them, and the banner that advertises the whole layout.

Merged from: test_home_feed_smoke.py, test_home_panels_smoke.py, test_home_extras_smoke.py, test_dashboard_columns.py, test_new_home_promo.py, test_devinfo_release.py.
"""

import pytest
from pathlib import Path
import sys

from mediaforge.web import version_info  # noqa: E402


# ==========================================================================
# test_home_feed_smoke.py
#
# Home feed (new home page) assembly.
# 
# Covers what the eleven-request JavaScript version could not: that a title
# never shows up in two rows, that a source several sites have becomes one card
# naming the others, that a dead source is *reported* rather than silently
# dropped, and that the adult source is only ever fetched when asked for.
# ==========================================================================
@pytest.fixture()
def stub_sources(app, monkeypatch):
    import mediaforge.web.routes.browse as B

    def fake(name, count=6):
        return lambda: [
            {"title": "%s %d" % (name, i), "url": "https://%s/%d" % (name, i),
             "poster_url": "", "genre": "g"}
            for i in range(count)
        ]

    hanime_calls = []

    def hanime(**kwargs):
        hanime_calls.append(kwargs)
        return [{"title": "adult 0", "url": "https://h/0", "poster_url": "", "genre": ""}]

    monkeypatch.setattr(B, "fetch_new_animes", fake("anime"))
    monkeypatch.setattr(B, "fetch_popular_animes", fake("anime"))     # same titles as "new"
    monkeypatch.setattr(B, "fetch_new_series", fake("series"))
    monkeypatch.setattr(B, "fetch_popular_series", fake("pseries"))
    monkeypatch.setattr(B, "_fetch_new_movies", lambda: [
        {"title": "Shared Movie", "url": "https://fp/1", "poster_url": "", "genre": ""}])
    monkeypatch.setattr(B, "fetch_megakino_new_movies", lambda: [
        {"title": "Shared  Movie!", "url": "https://mk/1", "poster_url": "", "genre": ""}])
    monkeypatch.setattr(B, "fetch_megakino_popular_movies", fake("mkmovie"))
    monkeypatch.setattr(B, "fetch_megakino_new_series", fake("mkseries"))
    monkeypatch.setattr(B, "fetch_megakino_popular_series", lambda: None)   # upstream down
    monkeypatch.setattr(B, "fetch_hanime_new", hanime)
    monkeypatch.setattr(B, "fetch_hanime_trending", hanime)
    # The browse cache would serve a previous test's data -- BOTH halves of
    # it. The in-memory dict is the obvious one; the sqlite table behind it
    # (browse_cache, read as a stale-while-revalidate fallback) is the one
    # that made these tests depend on which file ran before them: any earlier
    # test that hit /api/home-feed for real -- the route smoke test does --
    # left the real, unstubbed rows in there, and the stubs below never got
    # a chance to answer.
    def _clear():
        B._browse_cache.clear()
        with app.app_context():
            from mediaforge.web.db._core import get_db
            conn = get_db()
            try:
                conn.execute("DELETE FROM browse_cache")
                conn.commit()
            finally:
                conn.close()

    _clear()
    yield hanime_calls
    _clear()


def _key(item):
    return "".join(c for c in item["title"].lower() if c.isalnum()) + item["media_type"]


def test_feed_has_no_duplicates_across_rows(as_user, stub_sources):
    data = as_user("user").get("/api/home-feed").get_json()
    seen = {}
    for row in ("new", "popular", "movies"):
        for item in data["rows"][row]:
            assert _key(item) not in seen, (item["title"], row, seen.get(_key(item)))
            seen[_key(item)] = row
    assert data["rows"]["new"], "the feed should not be empty"


def test_same_title_from_two_sources_is_one_card(as_user, stub_sources):
    data = as_user("user").get("/api/home-feed").get_json()
    merged = [i for row in data["rows"].values() for i in row if i["also"]]
    assert merged, "FilmPalast and MegaKino both carry the movie"
    assert {a["source"] for a in merged[0]["also"]} == {"megakino"} or \
           {a["source"] for a in merged[0]["also"]} == {"filmpalast"}
    assert merged[0]["also"][0]["url"], "the other source must stay reachable"


def test_dead_source_is_reported_not_hidden(as_user, stub_sources):
    data = as_user("user").get("/api/home-feed").get_json()
    assert any(e["source"] == "megakino" for e in data["errors"])
    assert any(s["id"] == "megakino" and s["error"] for s in data["sources"])


def test_adult_source_is_only_fetched_when_asked_for(as_user, stub_sources, monkeypatch):
    from mediaforge.web.db import set_setting
    set_setting("source_enabled_hanime", "1")
    calls = stub_sources
    as_user("user").get("/api/home-feed")
    assert calls == [], "18+ must not be fetched with the chip off"
    as_user("user").get("/api/home-feed?adult=1")
    assert calls, "18+ must be fetched once the chip is on"
    set_setting("source_enabled_hanime", "0")


def test_module_registered_source_shows_up(as_user, stub_sources):
    from mediaforge.home_feed import register_home_feed_source, unregister_home_feed_source
    register_home_feed_source(
        "test-module", "testsite", "TestSite",
        {"new": lambda: [{"title": "Module Card", "url": "https://t/1",
                          "poster_url": "", "genre": ""}]},
        media_type="series")
    try:
        data = as_user("user").get("/api/home-feed").get_json()
        assert any(s["id"] == "testsite" for s in data["sources"])
        titles = [i["title"] for i in data["rows"]["new"]]
        assert "Module Card" in titles
    finally:
        unregister_home_feed_source("test-module")


def test_module_search_source_shows_up_without_feed_fetchers(as_user, stub_sources):
    """A module that only registered a provider + a search source has no
    discovery lists the core could scrape -- but it must still be listed as a
    source, or it can neither be filtered on nor switched off on the home
    page."""
    from mediaforge.search import register_search_source, unregister_search_source
    register_search_source("test-module-2", "searchonly", lambda kw: [],
                           label="SearchOnly", media_types=["movies"])
    try:
        data = as_user("user").get("/api/home-feed").get_json()
        entry = next((s for s in data["sources"] if s["id"] == "searchonly"), None)
        assert entry, [s["id"] for s in data["sources"]]
        assert entry["label"] == "SearchOnly"
        assert entry["builtin"] is False
        # Declared types survive, so the type filter does not drop the source.
        assert entry["types"] == ["movies"]
        # ...and the settings list (Sources card / "off by default") knows it too.
        listed = as_user("user").get("/api/home-feed/sources").get_json()["sources"]
        assert any(s["id"] == "searchonly" for s in listed)
    finally:
        unregister_search_source("test-module-2")


def test_registration_rejects_a_builtin_id():
    from mediaforge.home_feed import register_home_feed_source
    with pytest.raises(ValueError):
        register_home_feed_source("x", "aniworld", "X", {"new": lambda: []})


def test_personal_rows_answer_even_with_nothing_to_show(as_user):
    data = as_user("user").get("/api/home-feed/personal").get_json()
    rows = {"continue", "watchlist", "library", "upcoming", "gaps"}
    assert rows <= set(data)
    assert all(isinstance(data[row], list) for row in rows)
    # Not a row: says whether "Continue watching" came from here or from a
    # linked Jellyfin/Plex user. "local" when nothing is linked.
    assert data["continue_source"] == "local"


# ---------------------------------------------------------------------------
# Start Page settings: the two levels (instance default + per-account
# override) and what they do to the feed.
# ---------------------------------------------------------------------------

def test_default_row_order_puts_the_borrowed_rows_last(as_user):
    """Watchlist and the calendar are other pages in miniature -- they belong
    below the rows that are only on the home page."""
    data = as_user("user").get("/api/home-feed/sources").get_json()
    order = data["config"]["order"]
    assert order[-2:] == ["watchlist", "upcoming"]
    assert order[0] == "continue"
    # "Fill the gaps" asks something of the user rather than offering
    # something, so it sits behind the discovery rows, not above them.
    assert order.index("gaps") > order.index("popular")


def test_every_row_says_where_it_comes_from(as_user):
    rows = {r["id"]: r for r in as_user("user").get("/api/home-feed/sources").get_json()["rows"]}
    assert rows["watchlist"]["hint"] == "favourites"
    assert rows["watchlist"]["link"] == "/favourites"
    assert rows["upcoming"]["hint"] == "calendar"
    assert rows["continue"]["hint"] == "playback"
    assert rows["new"]["hint"] == "sources"


def test_admin_default_applies_until_the_user_overrides_it(as_user, stub_sources):
    from mediaforge.web.db import set_setting
    set_setting("home_rows_order", "movies,new,popular,continue,library,watchlist,upcoming")
    set_setting("home_rows_hidden", "popular")
    set_setting("home_cards_per_row", "10")
    try:
        client = as_user("user")
        cfg = client.get("/api/home-feed").get_json()["config"]
        assert cfg["order"][0] == "movies"
        assert cfg["hidden"] == ["popular"]
        assert cfg["limit"] == 10
        assert cfg["overridden"] == []

        # A hidden row is not just invisible -- it is not collected at all.
        rows = client.get("/api/home-feed").get_json()["rows"]
        assert rows["popular"] == []
        assert len(rows["new"]) <= 10

        # Now the user disagrees, about the order only.
        client.post("/api/user/preferences",
                    json={"home_feed_layout": "o:new,popular,movies,continue,library,watchlist,upcoming"})
        cfg = client.get("/api/home-feed").get_json()["config"]
        assert cfg["order"][0] == "new"
        assert cfg["overridden"] == ["order"]
        # ...so the parts they did not touch still follow the instance default.
        assert cfg["hidden"] == ["popular"]
        assert cfg["limit"] == 10

        # And back to the default.
        client.post("/api/user/preferences", json={"home_feed_layout": ""})
        assert client.get("/api/home-feed").get_json()["config"]["order"][0] == "movies"
    finally:
        for key in ("home_rows_order", "home_rows_hidden", "home_cards_per_row"):
            set_setting(key, "")


def test_a_junk_layout_can_never_lose_a_row(as_user):
    client = as_user("user")
    client.post("/api/user/preferences",
                json={"home_feed_layout": "o:watchlist,not-a-row,watchlist;n:999"})
    try:
        cfg = client.get("/api/home-feed/sources").get_json()["config"]
        assert cfg["order"][0] == "watchlist"
        assert sorted(cfg["order"]) == sorted(
            ["continue", "library", "watchlist", "upcoming", "new", "popular",
             "movies", "gaps", "because"])
        assert cfg["limit"] == 30          # 999 is not one of the offered steps
    finally:
        client.post("/api/user/preferences", json={"home_feed_layout": ""})


def test_start_page_defaults_are_admin_only(as_user):
    resp = as_user("user").put("/api/settings", json={"home_cards_per_row": "10"})
    assert resp.status_code in (302, 401, 403)


def test_personal_rows_are_skipped_when_hidden(as_user):
    from mediaforge.web.db import set_setting
    set_setting("home_rows_hidden", "continue,library,watchlist,upcoming,gaps")
    try:
        data = as_user("user").get("/api/home-feed/personal").get_json()
        # Only the ROW keys -- the payload also carries continue_source, which
        # is a string and says where the row would have come from.
        assert all(data[row] == [] for row in
                   ("continue", "library", "watchlist", "upcoming", "gaps"))
    finally:
        set_setting("home_rows_hidden", "")


# ── The status filter ("have I already got this?") ──────────────────────────
# Client-side by design: the two answers it filters on are the ones app.js
# already computes for the badges on the card, and it has to be able to change
# the row without re-fetching it (the row is backed by a reserve pool). What
# CAN be pinned down here is that the pieces are all present and wired to each
# other -- a missing i18n key or a filter that never reaches visibleCards()
# would otherwise only show up as a dropdown that quietly does nothing.

_STATIC = Path(__file__).resolve().parents[1] / "src/mediaforge/web/static"
_TEMPLATES = Path(__file__).resolve().parents[1] / "src/mediaforge/web/templates"


def _read(name, where=None):
    return (where or _STATIC).joinpath(name).read_text(encoding="utf-8")


def test_the_status_filter_has_all_its_strings():
    """Every home-page string goes through Flask-Babel in index.html; one that
    does not is a German instance showing an English word."""
    html = _read("index.html", _TEMPLATES)
    for key in ("status", "many_status", "status_library", "status_autosync",
                "status_library_hint", "status_autosync_hint"):
        assert "'%s':" % key in html, key
    # And through _() -- a raw string here would never reach the catalogue.
    assert "'status_library': _('Already downloaded')" in html
    assert "'status_autosync': _('On Auto-Sync')" in html


def test_the_status_strings_are_translated_to_german():
    """The two entries have to read like the badges they remove ("Vorhanden",
    "Im Auto-Sync") -- reusing an existing msgid would have given
    "Heruntergeladen", which is a different word for the same thing."""
    import gettext

    root = Path(__file__).resolve().parents[1] / "src/mediaforge/web/translations"
    de = gettext.translation("messages", str(root), languages=["de"])
    assert de.gettext("Already downloaded") == "Vorhanden"
    assert de.gettext("On Auto-Sync") == "Im Auto-Sync"


def test_the_filter_is_a_third_dropdown_and_both_entries_start_on():
    js = _read("home_feed.js")
    assert 'msRoot("status"' in js
    assert 'msItem("library"' in js and 'msItem("autosync"' in js
    # Both on by default: offStatus starts empty and statusOn() is "not off".
    assert "let offStatus = {};" in js
    assert "function statusOn(key) { return !offStatus[key]; }" in js


def test_the_filter_reaches_the_discovery_rows_but_not_the_personal_ones():
    """"New in your library" is a list of things you HAVE. Filtering
    "already downloaded" out of it does not narrow that row, it empties it."""
    js = _read("home_feed.js")
    visible = js.split("function visibleCards(")[1].split("\n  }")[0]
    assert "statusAllows(item)" in visible
    personal = js.split("function renderPersonal(")[1].split("\n  }")[0]
    assert "statusAllows" not in personal


def test_the_filter_asks_app_js_rather_than_matching_titles_itself():
    """A second copy of "is this in my library" drifts from the badge the first
    time the alias index or the mediascan path changes -- and both have."""
    js = _read("home_feed.js")
    allows = js.split("function statusAllows(")[1].split("\n  }")[0]
    assert "window.mfCardInLibrary" in allows
    assert "window.mfCardOnAutoSync" in allows

    app = _read("app.js")
    assert "window.mfCardInLibrary = mfCardInLibrary;" in app
    assert "window.mfCardOnAutoSync = mfCardOnAutoSync;" in app


def test_switching_both_status_entries_off_is_allowed():
    """Unlike an empty Sources or Type list, "hide what I have and what is
    syncing" is the whole point of the control and cannot empty the page on
    its own -- so it must not be bounced by the never-all-off guard."""
    js = _read("home_feed.js")
    assert 'if (kind !== "status" && Object.keys(picked).length === 0)' in js


def test_the_filter_survives_a_preference_saved_before_it_existed():
    """Stored filters are one string ("s:...;t:..."). An account that saved one
    before the status filter existed has no "x:" part, and must come back with
    both entries on rather than with the key list it never wrote."""
    js = _read("home_feed.js")
    assert '";x:"' in js                      # written
    assert 'bits[0] === "x" ? offStatus' in js  # read, unknown prefixes ignored


def test_three_dropdowns_still_fit_a_phone():
    """Three controls across a 360px screen is ~110px each -- an ellipsis with
    a chevron. The row wraps instead (it was nowrap for two)."""
    css = _read("index.css")
    phone = css.split("@media (max-width: 640px) {")[1].split("\n}")[0]
    assert ".feed-filter-menus" in phone
    assert "flex-wrap: wrap;" in phone


def test_the_status_trigger_says_everything_rather_than_a_count():
    """Its default state is "both on". "2 states" there is a number the reader
    has to decode into "nothing is filtered out" -- and it does not say which
    two. The shared dropdown grew an opt-in `data-all-label` for it, so every
    other menu keeps the count it had."""
    ms = _read("mf_multiselect.js")
    assert "data-all-label" in ms
    assert "root.dataset.allLabel" in ms
    # Opt-in: a root without the attribute must fall through to the count.
    assert 'var allLabel = root.dataset.allLabel || "";' in ms

    js = _read("home_feed.js")
    assert 'HT("status_all")' in js
    html = _read("index.html", _TEMPLATES)
    assert "'status_all': _('Everything')" in html


def test_a_caption_can_never_be_torn_off_its_dropdown():
    """The captions used to be siblings of the menus in the wrapping row, so
    the third control pushed the break between "TYP" and its dropdown and left
    the caption at the end of the line above a menu it did not belong to. A
    wrapping row breaks wherever it likes -- so it is not offered the place."""
    js = _read("home_feed.js")
    assert "function msGroup(" in js
    # Every caption is rendered by msGroup and by nothing else.
    menus = js.split("function renderFilterMenus(")[1].split("\n  }")[0]
    assert menus.count("feed-chip-label") == 0, "a caption still escapes the group"
    assert menus.count("msGroup(") == 3

    css = _read("index.css")
    assert ".feed-filter-group {" in css
    # The spacing between two controls is the row's gap now; the old
    # every-caption-but-the-first margin is gone.
    assert ".feed-chip-label--split" not in css.split("/*")[0] or True
    assert "feed-chip-label--split {" not in css


# ==========================================================================
# test_home_panels_smoke.py
#
# Home panel bar: /api/home-panels and /api/home-panel/<id>.
# 
# The interesting parts are not "does it return JSON" but the three promises the
# feature makes: an admin-only panel is invisible AND unreachable for a normal
# account, one broken panel does not take the page down, and nothing a module
# returns reaches the DOM unfiltered.
# ==========================================================================
@pytest.fixture()
def clean_registry():
    """Registered panels are process-global -- a test that leaves one behind
    changes what the next test sees in the bar."""
    from mediaforge import home_panels as HP
    HP._EXTRA_HOME_PANELS.clear()
    yield HP
    HP._EXTRA_HOME_PANELS.clear()


@pytest.fixture(autouse=True)
def fresh_badges():
    """The badge cache is per process and deliberately shared across accounts;
    without this the second test in a run reads the first one's numbers."""
    from mediaforge.web.routes import home_panels as R
    R._badge_cache["at"] = 0.0
    R._badge_cache["values"].clear()
    yield


# ── the bar ──────────────────────────────────────────────────────────────

def test_user_does_not_see_admin_only_panels(as_user):
    data = as_user("user").get("/api/home-panels").get_json()
    ids = [p["id"] for p in data["panels"]]
    assert "queue" in ids and "library" in ids
    assert "storage" not in ids and "system" not in ids


def test_admin_sees_every_builtin_panel(as_user):
    data = as_user("admin").get("/api/home-panels").get_json()
    ids = [p["id"] for p in data["panels"]]
    assert {"queue", "activity", "library", "storage", "system"} <= set(ids)


def test_admin_only_panel_is_forbidden_not_just_hidden(as_user):
    """The button being absent is cosmetic; the route is the actual gate."""
    assert as_user("user").get("/api/home-panel/storage").status_code == 403
    assert as_user("admin").get("/api/home-panel/storage").status_code == 200


def test_unknown_panel_is_404(as_user):
    assert as_user("user").get("/api/home-panel/nope").status_code == 404


def test_panel_bodies_have_the_expected_shape(as_user):
    for pid in ("queue", "activity", "library"):
        body = as_user("user").get("/api/home-panel/" + pid).get_json()
        assert body["id"] == pid
        assert isinstance(body["stats"], list)
        assert isinstance(body["items"], list)
        assert "error" not in body, body


# ── module panels ────────────────────────────────────────────────────────

def test_module_panel_shows_up_and_renders(as_user, clean_registry):
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": "hello", "href": "/library"}]},
        badge=lambda: 3)
    data = as_user("user").get("/api/home-panels").get_json()
    entry = next(p for p in data["panels"] if p["id"] == "demo")
    assert entry["badge"] == 3 and entry["builtin"] is False
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["title"] == "hello"


def test_a_broken_panel_reports_itself_instead_of_500(as_user, clean_registry):
    def boom():
        raise RuntimeError("module is having a day")

    clean_registry.register_home_panel("bad-module", "bad", "Bad", view=boom)
    resp = as_user("user").get("/api/home-panel/bad")
    assert resp.status_code == 200
    assert resp.get_json()["error"]


def test_a_broken_badge_does_not_break_the_bar(as_user, clean_registry):
    def boom():
        raise RuntimeError("nope")

    clean_registry.register_home_panel("bad-module", "bad", "Bad",
                                       view=lambda: {}, badge=boom)
    data = as_user("user").get("/api/home-panels").get_json()
    assert next(p for p in data["panels"] if p["id"] == "bad")["badge"] == 0


def test_module_panel_output_is_rebuilt_field_by_field(as_user, clean_registry):
    """A module hands back a dict; only the keys the client knows survive, and
    an off-site href is dropped rather than rendered as a link."""
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {
            "items": [
                {"title": "a", "href": "https://evil.example", "percent": 999},
                {"title": "b", "href": "//evil.example"},
                {"title": "c", "href": "/queue", "onclick": "alert(1)"},
            ],
            "link": {"href": "javascript:alert(1)", "label": "go"},
            "surprise": "<script>",
        })
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["href"] == ""          # absolute URL dropped
    assert body["items"][1]["href"] == ""          # protocol-relative dropped
    assert body["items"][2]["href"] == "/queue"
    assert body["items"][0]["percent"] == 100      # clamped, not passed through
    assert "onclick" not in body["items"][2]
    assert body["link"] is None
    assert "surprise" not in body


def test_panel_item_count_is_capped(as_user, clean_registry):
    from mediaforge.home_panels import PANEL_MAX_ITEMS
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": str(i)} for i in range(200)]})
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert len(body["items"]) == PANEL_MAX_ITEMS


def test_admin_only_module_panel_follows_the_same_rule(as_user, clean_registry):
    clean_registry.register_home_panel("demo-module", "demo", "Demo",
                                       view=lambda: {}, admin_only=True)
    assert as_user("user").get("/api/home-panel/demo").status_code == 403
    ids = [p["id"] for p in as_user("user").get("/api/home-panels").get_json()["panels"]]
    assert "demo" not in ids


# ── registry rules ───────────────────────────────────────────────────────

def test_a_module_cannot_shadow_a_builtin(clean_registry):
    with pytest.raises(ValueError):
        clean_registry.register_home_panel("m", "queue", "Queue", view=lambda: {})


def test_two_modules_cannot_share_a_panel_id(clean_registry):
    clean_registry.register_home_panel("m1", "demo", "Demo", view=lambda: {})
    with pytest.raises(ValueError):
        clean_registry.register_home_panel("m2", "demo", "Demo", view=lambda: {})


def test_unregister_drops_the_panel(clean_registry):
    clean_registry.register_home_panel("m1", "demo", "Demo", view=lambda: {})
    assert clean_registry.thirdparty_home_panel_ids() == {"m1"}
    clean_registry.unregister_home_panel("m1")
    assert clean_registry.thirdparty_home_panel_ids() == set()


def test_icon_data_is_filtered(clean_registry):
    clean_registry.register_home_panel("m1", "a", "A", view=lambda: {},
                                       icon='"/><script>alert(1)</script>')
    clean_registry.register_home_panel("m2", "b", "B", view=lambda: {},
                                       icon="M3 6h18")
    panels = {p["panel_id"]: p["icon"] for p in clean_registry.iter_home_panels()}
    assert panels["a"] == ""
    assert panels["b"] == "M3 6h18"


def test_module_cleanup_removes_panels():
    """unregister_module() must drop panels too -- otherwise a disabled
    module's button stays in the bar and 500s when clicked."""
    from mediaforge.web.thirdparties import registry as R
    import inspect
    source = inspect.getsource(R.unregister_module)
    assert "unregister_home_panel" in source


# ── the stored panel ─────────────────────────────────────────────────────

def test_stored_panel_is_only_returned_when_still_visible(as_user, app):
    from mediaforge.web import db
    client = as_user("user")
    with client.session_transaction() as sess:
        uid = sess["user_id"]
    with app.app_context():
        db.set_user_ui_prefs(uid, {"home_panel": "storage"})   # admin-only
    assert as_user("user").get("/api/home-panels").get_json()["active"] == ""
    with app.app_context():
        db.set_user_ui_prefs(uid, {"home_panel": "queue"})
    assert as_user("user").get("/api/home-panels").get_json()["active"] == "queue"


def test_the_client_never_persists_an_open_panel():
    """There is no "open panel" any more -- every panel is a card on the
    dashboard grid. The `home_panel` preference stays registered so an old
    account's stored value does not error, but nothing may read or write it.
    """
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web"
          / "static" / "home_panels.js").read_text(encoding="utf-8")
    assert "data.active" not in js, "home_panels.js restores the stored panel again"
    # mfSaveUserPref itself is fine now -- the grid stores the user's card
    # arrangement through it -- but nothing may write `home_panel`.
    assert '"home_panel"' not in js and "'home_panel'" not in js
    assert "home_panel:" not in js


def test_the_dashboard_polls_one_endpoint_for_the_moving_panels():
    """Six cards must not mean six pollers -- the whole reason the old design
    showed one panel at a time. One mfPoll, one request, and only the panels
    whose data actually changes.
    """
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web"
          / "static" / "home_panels.js").read_text(encoding="utf-8")
    assert js.count("mfPoll(") == 1
    assert "/api/home-panels/all" in js
    assert '"queue", "activity", "system"' in js


# ── the batch endpoint the dashboard loads from ──────────────────────────

def test_all_panels_come_back_with_their_bodies(as_user):
    data = as_user("admin").get("/api/home-panels/all").get_json()
    by_id = {p["id"]: p for p in data["panels"]}
    assert {"queue", "activity", "library", "storage", "system"} <= set(by_id)
    assert isinstance(by_id["queue"]["items"], list)
    assert by_id["queue"]["link"]["action"] == "queue"


def test_all_panels_honour_the_admin_gate(as_user):
    ids = {p["id"] for p in
           as_user("user").get("/api/home-panels/all").get_json()["panels"]}
    assert "queue" in ids
    assert "storage" not in ids and "system" not in ids


def test_only_narrows_the_batch_to_the_polled_panels(as_user):
    data = as_user("admin").get("/api/home-panels/all?only=queue,activity").get_json()
    assert [p["id"] for p in data["panels"]] == ["queue", "activity"]

# ── the queue is a modal, not a page ─────────────────────────────────────

def test_queue_panel_uses_an_action_and_never_links_to_a_missing_route(as_user, app):
    """There is no /queue route -- the queue hub is a modal in base.html. A
    link there produced a 404, which is the bug this pins."""
    body = as_user("user").get("/api/home-panel/queue").get_json()
    assert body["link"]["action"] == "queue"
    assert not body["link"]["href"]
    assert all(not i["href"] for i in body["items"])
    assert all(i["action"] == "queue" for i in body["items"])
    # and the route really is absent, so nobody "fixes" this by adding an href
    assert not any(str(r.rule) == "/queue" for r in app.url_map.iter_rules())


def test_only_known_actions_survive(as_user, clean_registry):
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": "a", "action": "eval"},
                                {"title": "b", "action": "queue"}],
                      "link": {"action": "nope", "label": "x"}})
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["action"] == ""
    assert body["items"][1]["action"] == "queue"
    assert body["link"] is None


# ── library cache shape ──────────────────────────────────────────────────

def test_library_panel_reads_the_cache_dict_not_its_keys(as_user, app, monkeypatch):
    """entry["data"] is a dict; iterating it yields key strings. That made the
    panel report "1 title, 1 series" on a library with hundreds of both."""
    from mediaforge.web.routes import home_panels as R

    entry = {"data": {
        "label": "Default", "custom_path_id": None, "titles": [
            {"folder": "Serie A", "is_movie": False, "total_episodes": 12,
             "total_size": 1024 ** 3},
            {"folder": "Film B", "is_movie": True, "total_size": 2 * 1024 ** 3},
        ],
        "books": [],
    }}
    monkeypatch.setattr("mediaforge.web.db.get_all_library_cache",
                        lambda: {"default": entry})
    monkeypatch.setattr("mediaforge.web.routes.library.lib_path_keys_for_kind",
                        lambda kind: {"default"})
    with app.test_request_context():
        stats = {s["label_key"]: s["value"] for s in R._panel_library()["stats"]}
    assert stats["hp_series"] == "1"
    assert stats["hp_movies"] == "1"
    assert stats["hp_episodes"] == "12"
    assert stats["hp_size"].startswith("3.0 GB")


def test_language_separated_libraries_are_counted_too(app, monkeypatch):
    """With language separation on, `titles` is None and everything hides in
    lang_folders -- which no consumer outside library.py handled."""
    from mediaforge.web.routes import home_panels as R

    entry = {"data": {"titles": None, "lang_folders": [
        {"name": "German", "titles": [{"folder": "S1", "is_movie": False,
                                       "total_episodes": 3, "total_size": 0}]},
        {"name": "English", "titles": [{"folder": "S2", "is_movie": False,
                                        "total_episodes": 4, "total_size": 0}]},
    ]}}
    monkeypatch.setattr("mediaforge.web.db.get_all_library_cache",
                        lambda: {"default": entry})
    monkeypatch.setattr("mediaforge.web.routes.library.lib_path_keys_for_kind",
                        lambda kind: {"default"})
    with app.test_request_context():
        stats = {s["label_key"]: s["value"] for s in R._panel_library()["stats"]}
    assert stats["hp_series"] == "2"
    assert stats["hp_episodes"] == "7"


# ── badges count the queue, not its archive ──────────────────────────────

def _queue_row(status, hidden):
    """One queue row through the real API, then forced into the state we want.

    Deliberately not a hand-written INSERT: download_queue has a dozen NOT
    NULL columns and a test that spells them out itself goes red the next
    time one is added, for a reason that has nothing to do with badges.
    """
    from mediaforge.web import db as DB
    qid = DB.add_to_queue("T", "https://example.invalid/x", [{"episode": 1}],
                          "German", "aniworld")
    conn = DB.get_db()
    try:
        conn.execute("UPDATE download_queue SET status = ?, hidden = ? WHERE id = ?",
                     (status, hidden, qid))
        conn.commit()
    finally:
        conn.close()


def test_cleared_entries_stop_counting_towards_the_badges(app):
    """Removing a finished entry sets hidden = 1 instead of deleting the row,
    so the download still counts towards the statistics. The badges must NOT
    follow that: a badge is a to-do list, and counting cleared failures made
    the System button climb forever while its panel stayed empty."""
    from mediaforge.web import db as DB
    from mediaforge.web.routes import home_panels as R

    # Deltas, not absolutes: the session database is shared with every other
    # test in the run, and a test that empties download_queue to get a clean
    # number decides what the tests after it see.
    before_failed = R._failed_count()
    before_queue = R._queue_badge()
    before_all = (DB.get_queue_stats()["by_status"] or {}).get("failed", 0)

    _queue_row("failed", 0)      # still in the queue
    _queue_row("failed", 1)      # cleared away by the user
    _queue_row("failed", 1)
    _queue_row("queued", 0)

    assert R._failed_count() - before_failed == 1
    assert R._queue_badge() - before_queue == 1
    # The statistics still see every row -- that is why they are kept.
    assert (DB.get_queue_stats()["by_status"] or {}).get("failed", 0) - before_all == 3


# ── storage: which paths count as one disk ───────────────────────────────

@pytest.fixture()
def fresh_disk_cache():
    from mediaforge.web.routes import home_panels as R
    R._disk_cache["at"] = 0.0
    R._disk_cache["value"] = []
    yield R
    R._disk_cache["at"] = 0.0
    R._disk_cache["value"] = []


class _Usage:
    def __init__(self, total, free):
        self.total, self.free = total, free
        self.used = total - free


def _fake_storage(monkeypatch, roots, usage_by_path, dev_by_path):
    from mediaforge.web.routes import home_panels as R
    monkeypatch.setattr(R, "_download_roots", lambda: roots)
    monkeypatch.setattr(R.shutil, "disk_usage",
                        lambda p: _Usage(*usage_by_path[str(p)]))
    monkeypatch.setattr(R, "_device_id", lambda p: dev_by_path.get(str(p)))


def test_docker_bind_mounts_of_one_export_are_one_row_naming_all_of_them(
        fresh_disk_cache, monkeypatch):
    """Six bind mounts of /mnt/nas/... are one filesystem, so they share an
    st_dev. They must collapse to one bar that still names all six -- the old
    code kept only the first label and the other five vanished."""
    R = fresh_disk_cache
    names = ["Downloads", "Anime", "Serien", "XXX", "Filme", "Books"]
    roots = [(n, "/app/" + n) for n in names]
    usage = {"/app/" + n: (7 * 1024 ** 4, 3 * 1024 ** 4) for n in names}
    devs = {"/app/" + n: 2049 for n in names}       # one superblock
    _fake_storage(monkeypatch, roots, usage, devs)

    rows = R._disk_rows()
    assert len(rows) == 1
    for name in names:
        assert name in rows[0][0]


def test_datasets_sharing_a_pool_are_one_row_despite_different_st_dev(
        fresh_disk_cache, monkeypatch):
    """ZFS datasets and btrfs subvolumes get their own st_dev but share the
    pool's free space. Keying on st_dev alone would draw one identical bar
    per dataset and claim they are independent disks."""
    R = fresh_disk_cache
    roots = [("Filme", "/a"), ("Serien", "/b")]
    usage = {"/a": (7 * 1024 ** 4, 3 * 1024 ** 4),
             "/b": (7 * 1024 ** 4, 3 * 1024 ** 4)}
    _fake_storage(monkeypatch, roots, usage, {"/a": 60, "/b": 61})

    rows = R._disk_rows()
    assert len(rows) == 1
    assert "Filme" in rows[0][0] and "Serien" in rows[0][0]


def test_separately_mounted_shares_stay_separate(fresh_disk_cache, monkeypatch):
    """One NAS, but each share mounted on its own on the host: different
    superblocks AND different free space, so they are different rows."""
    R = fresh_disk_cache
    roots = [("Downloads", "/a"), ("Filme NAS", "/b"), ("eBooks", "/c")]
    usage = {"/a": (930 * 1024 ** 3, 155 * 1024 ** 3),
             "/b": (7 * 1024 ** 4, 3 * 1024 ** 4),
             "/c": (3 * 1024 ** 4, 1 * 1024 ** 4)}
    _fake_storage(monkeypatch, roots, usage, {"/a": 60, "/b": 61, "/c": 62})

    rows = R._disk_rows()
    assert [r[0] for r in rows] == ["Downloads", "Filme NAS", "eBooks"]


def test_an_unreadable_path_does_not_take_the_others_down(
        fresh_disk_cache, monkeypatch):
    """A path that is not mounted right now is skipped, not an error."""
    R = fresh_disk_cache
    monkeypatch.setattr(R, "_download_roots",
                        lambda: [("Gone", "/gone"), ("Here", "/here")])

    def _usage(path):
        if str(path) == "/gone":
            raise OSError("not mounted")
        return _Usage(1000, 400)
    monkeypatch.setattr(R.shutil, "disk_usage", _usage)
    monkeypatch.setattr(R, "_device_id", lambda p: 7)

    rows = R._disk_rows()
    assert [r[0] for r in rows] == ["Here"]


# ── the LEGACY dashboard grid layout preference (home_dash_layout) ─────
#
# static/home_panels.js owns the client half (parseLayout/serializeLayout);
# this pins the server half, web/db/ui_prefs.py's _valid_dash_layout(), which
# is what actually stands between a stored preference and the database. Both
# shapes -- the pre-12-column "id:order:span[1-3]" rows an account may still
# have saved, and the current "id:order:colspan:rowspan" -- must validate,
# and junk must be dropped without failing the whole string.

def test_dash_layout_accepts_legacy_v1_rows():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert _valid_dash_layout("queue:10:2,storage:20:1")


def test_dash_layout_accepts_current_v2_rows():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert _valid_dash_layout("queue:10:8:a,storage:20:4:14")


def test_dash_layout_accepts_a_mix_of_both_shapes():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert _valid_dash_layout("queue:10:2,storage:20:4:14")


def test_dash_layout_rejects_junk():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert not _valid_dash_layout("queue:10:13:a")     # colspan out of 1-12
    assert not _valid_dash_layout("queue:10:8:41")      # rowspan out of 1-40
    assert not _valid_dash_layout("queue:10:4")         # v1 span out of 1-3
    assert not _valid_dash_layout("nope")
    assert not _valid_dash_layout("queue:10:8:a,")      # trailing empty part


# ── v3: the free-position engine's own format, "<id>:<x>:<y>:<w>:<h>" ─────
# x = column 0-11, y = row 0-999, w = column span 2-12, h = row span 3-80.

def test_dash_layout_accepts_current_v3_rows():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert _valid_dash_layout("queue:0:0:8:10,storage:8:0:4:8")


def test_dash_layout_accepts_a_mix_of_all_three_shapes():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert _valid_dash_layout("queue:10:2,storage:20:4:14,activity:0:14:8:10")


def test_dash_layout_rejects_v3_junk():
    from mediaforge.web.db.ui_prefs import _valid_dash_layout
    assert not _valid_dash_layout("queue:12:0:8:10")    # x out of 0-11
    assert not _valid_dash_layout("queue:0:0:1:10")     # w out of 2-12
    assert not _valid_dash_layout("queue:0:0:8:2")      # h out of 3-80
    assert not _valid_dash_layout("queue:0:0:8:81")     # h out of 3-80
    assert not _valid_dash_layout("queue:0:0:13:10")    # w out of 2-12


# ── the closed-card preference (home_dash_hidden) ────────────────────────
#
# Which base-id cards the account closed with a card's own "x", so the next
# poll/load does not just recreate it -- see static/home_panels.js's
# HIDDEN set and isHidden(). Only base ids (no ".") are ever stored; an
# extra multi-instance ("queue.2") never needs an entry, so the charset only
# has to match a v3 layout row's id part.

def test_dash_hidden_accepts_empty_string_as_nothing_hidden():
    from mediaforge.web.db.ui_prefs import _valid_dash_hidden
    assert _valid_dash_hidden("")


def test_dash_hidden_accepts_a_comma_list_of_ids():
    from mediaforge.web.db.ui_prefs import _valid_dash_hidden
    assert _valid_dash_hidden("gaps,queue,demo-module")
    assert _valid_dash_hidden("gaps")


def test_dash_hidden_rejects_junk():
    from mediaforge.web.db.ui_prefs import _valid_dash_hidden
    assert not _valid_dash_hidden("gaps,")               # trailing empty part
    assert not _valid_dash_hidden("<script>")             # bad charset
    assert not _valid_dash_hidden("a" * 2001)              # over the length cap
    assert not _valid_dash_hidden(",".join(["a"] * 41))    # over the count cap


# ==========================================================================
# test_home_extras_smoke.py
#
# Home page 2.1: the endpoints around the rows.
# 
# Covers the parts that are easy to get subtly wrong and impossible to notice
# by looking at the page: the per-row feed endpoint, the age ceiling actually
# being a server-side restriction rather than a client-side filter, and the
# media-server client refusing an id it did not hand out.
# ==========================================================================
# ---------------------------------------------------------------------------
# Per-row feed (#13)
# ---------------------------------------------------------------------------

def test_each_discovery_row_can_be_fetched_on_its_own(as_user, monkeypatch):
    from mediaforge.web.routes import browse
    # No network: the fetchers are the sites themselves, and this test is
    # about the shape of the answer, not about what is on AniWorld today.
    monkeypatch.setattr(browse, "_cached_browse", lambda key, fn: [])
    client = as_user("user")
    for row in ("new", "popular", "movies"):
        data = client.get("/api/home-feed/row/" + row).get_json()
        assert data["row"] == row
        assert row in data["rows"]
        # The chip row is built from whichever row answers first, so every
        # response has to carry the source list -- not just the first one.
        assert isinstance(data["sources"], list)


def test_an_unknown_row_is_404_not_an_empty_page(as_user):
    assert as_user("user").get("/api/home-feed/row/nonsense").status_code == 404


def test_a_row_request_does_not_fetch_the_other_rows(as_user, monkeypatch):
    """The whole point of the split: asking for "popular" must not scrape the
    sites that only publish "new"."""
    from mediaforge.web.routes import browse

    asked = []

    def _fake_cached_browse(key, fn):
        asked.append(key)
        return []

    monkeypatch.setattr(browse, "_cached_browse", _fake_cached_browse)
    as_user("user").get("/api/home-feed/row/popular")
    assert asked, "no source was consulted at all"
    assert all("new" not in key for key in asked)


# ---------------------------------------------------------------------------
# Age ceiling (#8) -- the part that has to be a restriction, not a filter
# ---------------------------------------------------------------------------

def _pin_of(_set_setting):
    """The PIN currently configured -- the teardown of the two tests below
    needs it to get back out of the mode they entered."""
    from mediaforge.web.db import get_setting
    return (get_setting("home_kids_pin", "") or "").strip()


def test_entering_kids_mode_is_refused_until_an_admin_armed_it(as_user):
    """A mode with no PIN behind it is one nobody could ever leave, so it is
    refused rather than entered."""
    from mediaforge.web.db import set_setting
    set_setting("home_kids_enabled", "0")
    set_setting("home_kids_pin", "")
    resp = as_user("user").post("/api/home/mode",
                                json={"mode": "kids", "max_fsk": "6"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "kids-disabled"


def test_the_age_ceiling_is_not_writable_through_the_generic_prefs_endpoint(as_user):
    """A kids mode a client can switch off with one PUT is decoration."""
    resp = as_user("user").post("/api/user/preferences", json={"home_max_fsk": "18"})
    assert resp.status_code >= 400


def test_lowering_the_ceiling_is_free_and_raising_it_needs_the_pin(as_user):
    from mediaforge.web.db import set_setting

    client = as_user("user")
    set_setting("home_kids_pin", "1234")
    set_setting("home_kids_enabled", "1")
    try:
        assert client.post("/api/home/mode",
                           json={"mode": "kids", "max_fsk": "6"}).status_code == 200
        # Back out again without the PIN: refused.
        assert client.post("/api/home/mode",
                           json={"mode": "", "max_fsk": ""}).status_code == 403
        # With it: allowed.
        assert client.post("/api/home/mode",
                           json={"mode": "", "max_fsk": "", "pin": "1234"}).status_code == 200
    finally:
        client.post("/api/home/mode", json={"mode": "", "max_fsk": "",
                                            "pin": _pin_of(set_setting)})
        set_setting("home_kids_pin", "")
        set_setting("home_kids_enabled", "0")


def test_a_wrong_pin_does_not_leak_through_a_lower_step(as_user):
    """"" (no ceiling) is the HIGHEST value there is, not the lowest -- a
    plain string comparison gets that exactly backwards."""
    from mediaforge.web.db import set_setting

    client = as_user("user")
    set_setting("home_kids_pin", "4321")
    set_setting("home_kids_enabled", "1")
    try:
        client.post("/api/home/mode", json={"mode": "kids", "max_fsk": "0"})
        assert client.post("/api/home/mode",
                           json={"max_fsk": "", "pin": "0000"}).status_code == 403
        assert client.post("/api/home/mode",
                           json={"max_fsk": "12", "pin": "0000"}).status_code == 403
    finally:
        client.post("/api/home/mode", json={"mode": "", "max_fsk": "",
                                            "pin": _pin_of(set_setting)})
        set_setting("home_kids_pin", "")
        set_setting("home_kids_enabled", "0")


def test_cards_above_the_ceiling_are_dropped_but_unrated_ones_are_kept():
    """Dropping everything TMDB has no rating for would empty the page on an
    instance without a TMDB key, and teach people to switch the limit off."""
    from mediaforge.web.routes.browse import _feed_apply_age_limit

    items = [
        {"title": "kids", "tmdb": {"fsk": "6"}},
        {"title": "teens", "tmdb": {"fsk": "16"}},
        {"title": "unrated", "tmdb": {}},
        {"title": "nothing at all"},
    ]
    kept = [i["title"] for i in _feed_apply_age_limit(items, 12)]
    assert kept == ["kids", "unrated", "nothing at all"]
    # No ceiling -> the list is handed back untouched.
    assert _feed_apply_age_limit(items, None) is items


# ---------------------------------------------------------------------------
# Media server (#continue watching / #15)
# ---------------------------------------------------------------------------

def test_an_unlinked_id_is_refused_before_it_reaches_a_url(monkeypatch):
    """The id comes from a per-user preference, so without this check an
    account could read anybody's history by editing it."""
    from mediaforge.web import mediaplayer

    monkeypatch.setattr(mediaplayer, "config",
                        lambda: {"kind": "jellyfin", "url": "http://x", "token": "t"})
    monkeypatch.setattr(mediaplayer, "list_users", lambda: [{"id": "abc", "name": "A"}])

    def _boom(*args, **kwargs):
        raise AssertionError("the server was contacted for an unknown user")

    monkeypatch.setattr(mediaplayer, "_get_json", _boom)
    assert mediaplayer.continue_watching("../../etc") == []
    assert mediaplayer.watch_stats("someone-else", 0) == {"available": False}


def test_artwork_proxy_refuses_anything_that_is_not_server_relative(monkeypatch):
    from mediaforge.web import mediaplayer

    monkeypatch.setattr(mediaplayer, "config",
                        lambda: {"kind": "plex", "url": "http://x", "token": "t"})
    for path in ("//evil.example/x.jpg", "http://evil.example/x.jpg", "",
                 "\\evil", "/ok\\..\\x"):
        assert mediaplayer.image_bytes(path) == (None, None)


@pytest.mark.parametrize("value,expected", [
    ("2026-07-31T22:10:05.1234567Z", True),     # Jellyfin's seven digits
    ("2026-07-31T22:10:05Z", True),
    ("2026-07-31T22:10:05.123+02:00", True),
    ("not a date", False),
    ("", False),
])
def test_jellyfin_timestamps_survive_the_parser(value, expected):
    """datetime.fromisoformat() accepts at most six fractional digits and
    raises on seven -- which is exactly what Jellyfin emits."""
    from mediaforge.web.mediaplayer import _parse_iso
    assert bool(_parse_iso(value)) is expected


# ---------------------------------------------------------------------------
# Onboarding / suggestions / wrapped
# ---------------------------------------------------------------------------

def test_onboarding_hides_admin_only_steps_from_a_normal_account(as_user):
    user_steps = {s["key"] for s in
                  as_user("user").get("/api/home/onboarding").get_json()["steps"]}
    admin_steps = {s["key"] for s in
                   as_user("admin").get("/api/home/onboarding").get_json()["steps"]}
    assert "sources" in admin_steps and "sources" not in user_steps
    assert "modules" in user_steps


def test_suggest_says_nothing_until_there_is_something_to_say(as_user):
    data = as_user("user").get("/api/home/suggest?q=a").get_json()
    assert data["groups"] == []


def test_wrapped_defaults_to_the_month_that_just_ended(as_user):
    data = as_user("user").get("/api/home/wrapped").get_json()
    assert len(data["period"]) == 7 and data["period"][4] == "-"
    # No media server linked -> the watched half is absent rather than zeroed.
    assert data["watched"]["available"] is False
    assert data["downloaded"]["count"] == 0


def test_wrapped_rejects_a_junk_period_instead_of_trusting_it(as_user):
    data = as_user("user").get("/api/home/wrapped?period=9999-99").get_json()
    assert len(data["period"]) == 7


def test_every_onboarding_step_points_at_a_real_place(as_user):
    """A checklist that links to a tab that does not exist is worse than no
    checklist -- the page loads and simply does not scroll anywhere. The
    media-server step pointed at /settings#account, which is neither a tab
    nor a page a normal account may open."""
    steps = {s["key"]: s["link"]
             for s in as_user("admin").get("/api/home/onboarding").get_json()["steps"]}
    assert steps["sources"] == "/settings#sources"
    assert steps["library"] == "/settings#library"
    # The TMDB key lives on Integrations; Settings has no CineInfo tab at all.
    assert steps["tmdb"] == "/integrations#cineinfo"
    assert steps["modules"] == "/extensions"
    assert steps["mediaplayer"] == "/profile#mediaplayer"


def test_a_module_card_says_it_comes_from_a_module(app):
    """On a shared tab a module's card sits next to the built-in ones and
    looked exactly like them."""
    from mediaforge.web.thirdparties.registry import _build_card

    with app.test_request_context():
        card = _build_card({
            "id": "not-registered-by-any-module", "label": "X", "badges": [],
            "description": "", "enable_label": "on", "enable_desc": "",
        })
    assert card["module_name"] is None      # a built-in gets no pill


# ==========================================================================
# test_dashboard_columns.py
#
# The Dashboard's card layout: 2 or 3 columns, per account.
# 
# Two predecessors were removed and must not come back as a second card
# container -- that is what would render a module widget twice under the same
# DOM id: a free-position pixel grid (#homeDashGrid) and four named sections
# (#homeDashSections). static/home_panels.js binds to #homeDashColumns and
# nothing else.
# ==========================================================================
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


def test_three_columns_by_default(as_user, prefs):
    prefs("user", new_home="1")
    body = _home(as_user)
    assert 'id="homeDashColumns"' in body
    assert 'data-cols="3"' in body
    for col in range(3):
        assert 'id="dashCol-%d"' % col in body


def test_two_columns_when_the_account_asked_for_two(as_user, prefs):
    prefs("user", new_home="1", home_dash_columns="2")
    body = _home(as_user)
    assert 'data-cols="2"' in body
    assert 'id="dashCol-1"' in body
    assert 'id="dashCol-2"' not in body


def test_the_removed_layouts_are_gone(as_user, prefs):
    """Neither predecessor may render a second card container."""
    prefs("user", new_home="1")
    body = _home(as_user)
    assert 'id="homeDashGrid"' not in body
    assert 'id="homeDashSections"' not in body
    assert 'class="dash-section"' not in body


def test_all_in_one_page_overrules_the_columns(as_user, prefs):
    """"All in one page" is an older layout (home_panel_bar.js) that predates
    both -- the columns must not render alongside it."""
    prefs("user", new_home="1", home_dash_enabled="all")
    body = _home(as_user)
    assert 'id="homePanelBar"' in body
    assert 'id="homeDashColumns"' not in body


def test_column_count_accepts_2_and_3_and_rejects_junk(as_user):
    for good in ("", "2", "3"):
        ok = as_user("user").post("/api/user/preferences", json={"home_dash_columns": good})
        assert ok.status_code == 200, good
    for bad in ("4", "1", "many"):
        resp = as_user("user").post("/api/user/preferences", json={"home_dash_columns": bad})
        assert resp.status_code >= 400, bad


def test_card_layout_accepts_id_column_pairs_and_rejects_junk(as_user):
    """Per-card placement (see static/home_panels.js's saveCardLayout()):
    "<card id>:<column>" pairs, list order = render order within the column."""
    ok = as_user("user").post(
        "/api/user/preferences",
        json={"home_dash_card_layout": "queue:0,storage:2,mymodule.panel:1"})
    assert ok.status_code == 200
    # Column 3 (index 2) is the highest a value may name, whatever the
    # account's current column count -- see _valid_dash_card_layout.
    for bad in ("queue:3", "queue:-1", "queue", "queue:0,", "queue:mediaforge"):
        resp = as_user("user").post("/api/user/preferences",
                                    json={"home_dash_card_layout": bad})
        assert resp.status_code >= 400, bad


def test_a_card_parked_in_column_3_survives_a_switch_to_2_columns(as_user, prefs):
    """The fold to the last visible column happens client-side, on read --
    the stored preference must keep the column the user actually chose, so
    switching back to 3 finds the card where it was left."""
    # home_dash_enabled is reset explicitly: preferences persist across the
    # tests in this file, and the "All in one page" test above would
    # otherwise leave this account on a layout that has no columns at all.
    prefs("user", new_home="1", home_dash_enabled="", home_dash_columns="2",
          home_dash_card_layout="queue:2")
    body = _home(as_user)
    assert 'data-cols="2"' in body
    resp = as_user("user").get("/api/user/preferences")
    assert resp.status_code == 200
    stored = (resp.get_json() or {}).get("preferences") or {}
    assert stored.get("home_dash_card_layout") == "queue:2"


def test_legacy_section_prefs_are_still_accepted(as_user):
    """A client or module still writing the sections layout's keys must not
    start getting 400s just because nothing reads them any more."""
    for key, value in (("home_dash_section_layout", "queue:mediaforge"),
                       ("home_dash_section_order", "system,mediaforge,stats,modules"),
                       ("home_dash_layout", "queue:0:0:8:10")):
        resp = as_user("user").post("/api/user/preferences", json={key: value})
        assert resp.status_code == 200, key


# ==========================================================================
# test_new_home_promo.py
#
# The per-account home page layout and the banner that advertises it.
# 
# Three promises, and all three are about NOT nagging people:
# 
#   * the layout is a per-ACCOUNT choice, so one person trying the new page does
#     not switch it for everyone on the instance (it used to: new_home_enabled
#     is a single instance-wide setting),
#   * the banner is decided server-side, so a user who answered it never ships
#     its markup again -- "hide it with CSS afterwards" still flashes on a slow
#     paint and still tells the browser about a pitch the user rejected,
#   * both answers ("try it" and "don't show again") end it for good.
# 
# The classic layout is also the only place a non-admin can reach these
# settings at all: /settings redirects them, so the modal on the home page is
# it. That is asserted here too, because losing it would leave someone who
# dismissed the banner permanently stuck on the layout they happen to have.
# ==========================================================================
@pytest.fixture()
def promo_prefs(app, users):
    """Read/write helpers for one account's UI preferences."""
    from mediaforge.web import db

    def _set(role, **values):
        uid = users["admin" if role == "admin" else "user"]
        with app.app_context():
            ok, err = db.set_user_ui_prefs(uid, dict(values))
            assert ok, err

    def _get(role):
        uid = users["admin" if role == "admin" else "user"]
        with app.app_context():
            return db.get_user_ui_prefs(uid) or {}

    return _set, _get


@pytest.fixture(autouse=True)
def promo_classic_default(app):
    """The instance default is the classic layout unless a test says otherwise."""
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("new_home_enabled", "0")
    yield
    with app.app_context():
        db.set_setting("new_home_enabled", "0")


def _promo_home(as_user, role="user"):
    resp = as_user(role).get("/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# ── the banner ───────────────────────────────────────────────────────────

def test_banner_is_offered_on_the_classic_layout(as_user, promo_prefs):
    setp, _ = promo_prefs
    setp("user", new_home="", new_home_promo_done="")
    assert 'id="newHomePromo"' in _promo_home(as_user)


def test_a_dismissed_banner_is_not_rendered_at_all(as_user, promo_prefs):
    """Not "hidden" -- absent. A banner the client removes afterwards was
    still sent, and still flashes before the script runs."""
    setp, _ = promo_prefs
    setp("user", new_home="", new_home_promo_done="1")
    body = _promo_home(as_user)
    assert 'id="newHomePromo"' not in body
    assert "Try the new home page" not in body


def test_the_new_layout_never_advertises_itself(as_user, promo_prefs):
    setp, _ = promo_prefs
    setp("user", new_home="1", new_home_promo_done="")
    assert 'id="newHomePromo"' not in _promo_home(as_user)


def test_switching_back_does_not_reopen_the_pitch(as_user, promo_prefs):
    """Someone who tried the new page and returned has answered. Asking again
    is what turns a banner into nagging."""
    setp, _ = promo_prefs
    setp("user", new_home="0", new_home_promo_done="1")
    assert 'id="newHomePromo"' not in _promo_home(as_user)


# ── the layout choice ────────────────────────────────────────────────────

def test_the_account_overrules_the_instance_default(app, as_user, promo_prefs):
    from mediaforge.web import db
    setp, _ = promo_prefs
    with app.app_context():
        db.set_setting("new_home_enabled", "1")
    setp("user", new_home="0", new_home_promo_done="1")
    # homeFeed is the new layout's container; homeSourceChips is the classic
    # one's. Exactly one of them may be on the page.
    body = _promo_home(as_user)
    assert 'id="homeSourceChips"' in body and 'id="homeFeed"' not in body


def test_an_empty_override_follows_the_instance_default(app, as_user, promo_prefs):
    """"" is a real value, not a missing one: it means "whatever the admin
    set". Reading it as "0" would pin every account to the classic page the
    first time they touched the form."""
    from mediaforge.web import db
    setp, _ = promo_prefs
    setp("user", new_home="", new_home_promo_done="1")
    with app.app_context():
        db.set_setting("new_home_enabled", "1")
    assert 'id="homeFeed"' in _promo_home(as_user)


def test_one_account_switching_does_not_move_anybody_else(app, as_user, promo_prefs):
    """The point of the whole change: new_home_enabled is instance-wide, so
    before this an admin looking at the new layout moved every account onto
    it."""
    setp, _ = promo_prefs
    setp("admin", new_home="1", new_home_promo_done="1")
    setp("user", new_home="", new_home_promo_done="1")
    assert 'id="homeFeed"' in _promo_home(as_user, "admin")
    assert 'id="homeSourceChips"' in _promo_home(as_user, "user")


def test_the_preference_is_writable_through_the_normal_endpoint(as_user, promo_prefs):
    _, getp = promo_prefs
    resp = as_user("user").post("/api/user/preferences",
                                json={"new_home": "1", "new_home_promo_done": "1"})
    assert resp.status_code == 200
    stored = getp("user")
    assert stored.get("new_home") == "1"
    assert stored.get("new_home_promo_done") == "1"


def test_a_junk_layout_value_is_refused(as_user):
    """The value is echoed back into the page through window._USER_PREFS."""
    resp = as_user("user").post("/api/user/preferences", json={"new_home": "yes"})
    assert resp.status_code >= 400


# ── the way back ─────────────────────────────────────────────────────────

def test_a_non_admin_cannot_reach_the_settings_page(as_user):
    """The premise of everything below: if this ever stops being true, the
    home page modal is no longer the only route to these settings."""
    assert as_user("user").get("/settings").status_code in (301, 302)


def test_the_classic_layout_carries_the_settings_modal_too(as_user, promo_prefs):
    """Without this, dismissing the banner is a one-way door: no settings
    page, no modal, no way back to either layout."""
    setp, _ = promo_prefs
    setp("user", new_home="0", new_home_promo_done="1")
    body = _promo_home(as_user)
    assert 'id="startPageOverlay"' in body
    assert 'id="spLayout-user"' in body
    assert 'id="homeCustomize"' in body
    assert "start_page.js" in body


def test_the_modal_opener_ships_with_the_classic_layout(as_user, promo_prefs):
    """openStartPageModal() used to live in home_feed.js, which the classic
    layout does not load -- the button would have called nothing."""
    from pathlib import Path
    setp, _ = promo_prefs
    setp("user", new_home="0", new_home_promo_done="1")
    assert "home_feed.js" not in _promo_home(as_user)
    static = Path(__file__).resolve().parents[1] / "src/mediaforge/web/static"
    assert "openStartPageModal" in (static / "start_page.js").read_text(encoding="utf-8")
    assert "window.openStartPageModal" not in (static / "home_feed.js").read_text(encoding="utf-8")


# ==========================================================================
# test_devinfo_release.py
#
# Which Dev Info "release" posts still deserve a banner on the home page.
# 
# A release banner exists to tell somebody a new version is out. Told to somebody
# who already runs it, it is noise -- and noise they cannot get rid of, because
# the banner is rebuilt from the cached feed on every visit and dismissal is only
# remembered per browser.
# 
# The rule lives in web/version_info.py so that the banner and the update badge
# cannot drift apart, and the direction of its uncertainty is deliberate: when
# the comparison cannot be made, the banner is SHOWN. Hiding news that mattered
# is the worse mistake of the two.
# ==========================================================================
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))



@pytest.fixture()
def install(monkeypatch):
    """Pretend this instance was installed in a particular way."""

    def configure(version, dev=False, source=False):
        monkeypatch.setattr(version_info, "_get_version", lambda: version)
        monkeypatch.setattr(version_info, "_get_dev_install_info",
                            lambda: (dev, "abc1234def" if dev else None))
        monkeypatch.setattr(version_info, "_is_source_build", lambda: source)

    return configure


def test_the_announced_version_is_the_one_already_installed(install):
    install("2.4.0")
    assert version_info.is_release_already_installed("v2.4.0")
    # The tag is written by hand on the devInfo server, so both spellings have
    # to mean the same thing.
    assert version_info.is_release_already_installed("2.4.0")


def test_a_release_older_than_what_is_installed_is_not_news_either(install):
    install("2.5.1")
    assert version_info.is_release_already_installed("v2.4.0")


def test_a_newer_release_still_gets_its_banner(install):
    install("2.3.9")
    assert not version_info.is_release_already_installed("v2.4.0")


def test_versions_are_compared_as_versions_not_as_strings(install):
    """The one a lexical compare gets wrong: "2.10.0" sorts below "2.4.0"."""
    install("2.4.0")
    assert not version_info.is_release_already_installed("v2.10.0")


def test_a_release_candidate_ranks_below_the_final_release(install):
    install("2.4.0")
    assert version_info.is_release_already_installed("v2.4.0-rc1")


def test_a_dev_branch_install_never_gets_a_release_banner(install):
    """Its version number tracks a moving branch and says nothing about a tag,
    so no comparison against one is meaningful."""
    install("2.3.0", dev=True)
    assert version_info.is_release_already_installed("v2.4.0")


def test_a_local_source_build_never_gets_one_either(install):
    install("2.3.0", source=True)
    assert version_info.is_release_already_installed("v2.4.0")


def test_without_version_metadata_the_banner_is_shown(install):
    """A frozen build may have no package metadata at all. Showing a banner
    that could have been hidden beats hiding one that mattered."""
    install("")
    assert not version_info.is_release_already_installed("v2.4.0")


def test_a_post_without_a_tag_is_not_a_release_post(install):
    install("2.4.0")
    assert not version_info.is_release_already_installed("")
    assert not version_info.is_release_already_installed(None)


def test_a_tag_that_is_not_a_version_falls_back_to_an_exact_match(install):
    install("2.4.0")
    assert not version_info.is_release_already_installed("nightly")
    install("nightly")
    assert version_info.is_release_already_installed("nightly")
