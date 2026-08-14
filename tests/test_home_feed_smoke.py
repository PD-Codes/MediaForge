"""Home feed (new home page) assembly.

Covers what the eleven-request JavaScript version could not: that a title
never shows up in two rows, that a source several sites have becomes one card
naming the others, that a dead source is *reported* rather than silently
dropped, and that the adult source is only ever fetched when asked for.
"""

import pytest


@pytest.fixture()
def stub_sources(monkeypatch):
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
    # The browse cache would serve a previous test's data.
    B._browse_cache.clear()
    yield hanime_calls
    B._browse_cache.clear()


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
from pathlib import Path

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
