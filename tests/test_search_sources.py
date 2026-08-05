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
    """Register a fake module search source for the duration of one test."""
    from mediaforge import search

    search.register_search_source(
        "test-item", "testsite", lambda kw: [{"title": "Hit " + kw, "url": "https://test.invalid/x"}],
        label="TestSite",
    )
    try:
        yield "testsite"
    finally:
        search.unregister_search_source("test-item")


# ── The catalogue itself ────────────────────────────────────────────────────
def test_builtins_are_listed_with_labels_and_css(app):
    from mediaforge.web.source_policy import search_sources

    with app.app_context():
        ids = [s["id"] for s in search_sources()]
    assert ids[:5] == ["aniworld", "sto", "filmpalast", "megakino", "hanime"]

    with app.app_context():
        by_id = {s["id"]: s for s in search_sources()}
    assert by_id["hanime"]["adult"] is True
    assert by_id["aniworld"]["adult"] is False
    # The class the WebUI puts on the result header must be a real one.
    assert by_id["sto"]["css_class"] == "browse-provider-sto"
    assert all(s["thirdparty"] is False for s in by_id.values())


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
    assert "renderResultsBySource" in text
    assert "function renderResultsBoth" not in text
    assert "loadSearchSources" in text
