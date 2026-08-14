"""The source catalogue: a module's content source must reach the search.

Before this existed, a third-party module could register a provider *and* a
search source and still never be asked a keyword: the WebUI fanned every
search out to five hardcoded site ids. These tests pin the contract that
replaced that -- one server-side list (``web/source_policy.search_sources``),
one endpoint (``GET /api/search/sources``), and no hardcoded five-source list
left in the frontend files that consume it.
"""

import json
import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"


@pytest.fixture()
def module_source():
    """Register a fake module search source for the duration of one test.

    The matching Provider is registered too, because that is the contract:
    a search hit's URL has to be resolvable, and api_search() drops the ones
    that are not (see test_unresolvable_hits_are_dropped).
    """
    from mediaforge import providers, search

    providers.register_provider(
        "test-item",
        providers.Provider(
            name="TestSite",
            episode_pattern=re.compile(r"^https://test\.invalid/[a-z0-9]+$"),
            episode_cls=object,
        ),
    )
    search.register_search_source(
        "test-item", "testsite", lambda kw: [{"title": "Hit " + kw, "url": "https://test.invalid/x"}],
        label="TestSite",
    )
    try:
        yield "testsite"
    finally:
        search.unregister_search_source("test-item")
        providers.unregister_provider("test-item")


# ── The catalogue itself ────────────────────────────────────────────────────
def test_builtins_are_listed_with_labels_and_css(app):
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        ids = [s["id"] for s in search_sources()]
    assert ids[:8] == ["aniworld", "sto", "filmpalast", "megakino",
                       "filmo", "nineanime", "aniwaves", "hanime"]

    with app.app_context():
        by_id = {s["id"]: s for s in search_sources()}
    assert by_id["hanime"]["adult"] is True
    assert by_id["aniworld"]["adult"] is False
    # The class the WebUI puts on the result header must be a real one.
    assert by_id["sto"]["css_class"] == "browse-provider-sto"
    assert all(s["thirdparty"] is False for s in by_id.values())


def test_english_only_sources_are_opt_in_but_not_adult(app):
    """9anime/Aniwaves default to off like the adult source, but must never be
    treated AS one: is_adult_source() drives the age-confirmation modal and the
    kids-mode filtering, and neither may fire for a source that is merely
    English-only."""
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        by_id = {s["id"]: s for s in search_sources()}

    for sid in ("nineanime", "aniwaves"):
        assert by_id[sid]["adult"] is False, sid
        assert by_id[sid]["opt_in"] is True, sid
        assert by_id[sid]["english_only"] is True, sid
        assert by_id[sid]["enabled"] is False, sid
    # filmo.to is a normal opt-out source: German-capable, on by default.
    assert by_id["filmo"]["opt_in"] is False
    assert by_id["filmo"]["english_only"] is False
    assert by_id["filmo"]["enabled"] is True


def test_english_only_sources_survive_an_age_limited_session(app):
    """An age ceiling drops the adult source from the catalogue. The opt-in
    English-only ones must stay -- they are not adult content, and hiding them
    would silently make them unreachable for every kids-mode account."""
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        ids = [s["id"] for s in search_sources(include_adult=False)]
    assert "hanime" not in ids
    assert "nineanime" in ids and "aniwaves" in ids and "filmo" in ids


def test_module_source_appears_and_disappears(app, module_source):
    """The whole point: registering shows up, unregistering removes it."""
    from mediaforge import search
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        entry = [s for s in search_sources() if s["id"] == "testsite"]
    assert entry, "a registered module search source must be in the catalogue"
    assert entry[0]["thirdparty"] is True
    assert entry[0]["label"] == "TestSite"
    # No colour is invented for a source the app has never seen.
    assert entry[0]["css_class"] == "browse-provider-thirdparty"
    # Installed on purpose -> on by default, unlike an adult source.
    assert entry[0]["enabled"] is True

    search.unregister_search_source("test-item")
    with app.app_context():
        assert "testsite" not in [s["id"] for s in search_sources()]


def test_adult_module_source_is_opt_in_and_hideable(app):
    from mediaforge import search
    from mediaforge.web.source_policy import search_sources

    search.register_search_source("adult-item", "adultsite", lambda kw: [],
                                  label="AdultSite", adult=True)
    try:
        with app.app_context():
            entry = [s for s in search_sources() if s["id"] == "adultsite"][0]
            assert entry["adult"] is True
            # An age-limited session must not even be told it exists.
            assert "adultsite" not in [
                s["id"] for s in search_sources(include_adult=False)
            ]
    finally:
        search.unregister_search_source("adult-item")


def test_custom_enabled_key_is_read_and_reported(app):
    """A module reusing its own settings key must not get a dead switch."""
    from mediaforge import search
    from mediaforge.web.db import set_setting
    from mediaforge.web.source_policy import search_sources

    search.register_search_source("keyed-item", "keyedsite", lambda kw: [],
                                  label="Keyed", enabled_key="keyed_search_on")
    try:
        with app.app_context():
            set_setting("keyed_search_on", "0")
            entry = [s for s in search_sources() if s["id"] == "keyedsite"][0]
            assert entry["enabled"] is False
            # Reported, so the settings page writes back the same key.
            assert entry["enabled_key"] == "keyed_search_on"
    finally:
        search.unregister_search_source("keyed-item")


def test_bad_site_id_is_refused():
    """The id becomes a settings key, a DOM id and part of a CSS class."""
    from mediaforge import search

    for bad in ("", "x", "Has Space", "<script>", "UPPER", "a" * 41):
        with pytest.raises(ValueError):
            search.register_search_source("bad-item", bad, lambda kw: [])


def test_builtin_site_id_still_refused():
    from mediaforge import search

    with pytest.raises(ValueError):
        search.register_search_source("clash", "megakino", lambda kw: [])


def test_thirdparty_listing_carries_no_callable():
    """The listing is JSON-serialised into a response; search_fn must not be
    in it (and would not survive jsonify anyway)."""
    from mediaforge import search

    search.register_search_source("json-item", "jsonsite", lambda kw: [], label="J")
    try:
        entries = search.thirdparty_search_sources()
        assert json.dumps(entries)   # raises if a callable leaked in
        assert all("search_fn" not in e for e in entries)
    finally:
        search.unregister_search_source("json-item")


# ── The endpoint ────────────────────────────────────────────────────────────
def test_endpoint_requires_login(client):
    """Every view is wrapped by app.py's auth pass; this one too."""
    resp = client.get("/api/search/sources")
    assert resp.status_code in (301, 302, 401, 403)


def test_endpoint_lists_module_source(as_user, module_source):
    resp = as_user("admin").get("/api/search/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    ids = [s["id"] for s in data["sources"]]
    assert "aniworld" in ids and "testsite" in ids
    assert "order" in data and "hide_disabled_in_search" in data


def test_search_route_reaches_module_source(as_user, module_source):
    """The other half: POST /api/search with the module's site id works."""
    resp = as_user("admin").post("/api/search",
                                 json={"keyword": "abc", "site": "testsite"})
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.get_json()["results"]]
    assert titles == ["Hit abc"]


def test_unresolvable_hits_are_dropped(as_user):
    """A hit no provider owns is a dead card -- clicking it can only answer
    "Unsupported URL" -- so api_search() must not hand it to the UI."""
    from mediaforge import search

    search.register_search_source(
        "broken-item", "brokensite",
        lambda kw: [{"title": "Dead", "url": "https://nothing.invalid/x"},
                    {"title": "Alive", "url": "https://aniworld.to/anime/stream/naruto"}],
        label="BrokenSite",
    )
    try:
        resp = as_user("admin").post("/api/search",
                                     json={"keyword": "abc", "site": "brokensite"})
        assert resp.status_code == 200
        assert [r["title"] for r in resp.get_json()["results"]] == ["Alive"]
    finally:
        search.unregister_search_source("broken-item")


# ── No hardcoded lists left where they caused the bug ───────────────────────
@pytest.mark.parametrize("filename", ["app.js", "settings.js", "integrations.js"])
def test_frontend_has_no_second_hardcoded_source_list(filename):
    """One hardcoded fallback per file is allowed (and documented); a second
    one means a consumer drifted back to its own list and will silently skip
    module sources again."""
    text = (_STATIC / filename).read_text(encoding="utf-8", errors="replace")
    hits = re.findall(r'"aniworld"\s*,\s*"sto"\s*,\s*"filmpalast"', text)
    assert len(hits) <= 1, (
        f"{filename} has {len(hits)} hardcoded source lists; derive from "
        "loadSearchSources() / sources.available instead"
    )


def test_search_fanout_is_derived_not_positional():
    """doSearch() must fan out over the catalogue, and the renderer must take
    a list -- the old five-positional-argument renderResultsBoth() could not
    represent a sixth source at all."""
    text = (_STATIC / "app.js").read_text(encoding="utf-8", errors="replace")
    assert "buildSourceSection" in text
    assert "function renderResultsBoth" not in text
    assert "loadSearchSources" in text


def test_search_renders_per_source_not_after_all_of_them():
    """Results appear as each source answers.

    Every consumer fans out one request per source and used to await
    Promise.all before painting anything, so the whole list arrived at the
    speed of the slowest site -- up to the 15 s per-request timeout when one
    was dead. Pinned here because "await Promise.all(...)" reads harmless and
    is the natural thing to write back.
    """
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8", errors="replace")
    # doSearch(): one slot per source, filled when THAT source answers.
    assert "buildSourceSection({" in app_js
    # runAniSearch(): one card appended per arriving answer.
    assert "forEach(appendResult)" in app_js
    for dead in ("const _sections = await Promise.all",
                 "const resultsArrays = await Promise.all"):
        assert dead not in app_js, f"{dead!r} is back -- search waits for the slowest source again"

    seerr_js = (_STATIC / "seerr.js").read_text(encoding="utf-8", errors="replace")
    assert "Promise.all(sources.map(" not in seerr_js
    # Re-rendering per answer must not re-scrape the posters.
    assert "_posterCache" in seerr_js
